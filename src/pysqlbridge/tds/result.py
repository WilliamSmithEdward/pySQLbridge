"""Describing and encoding a result set.

Two tokens carry a SELECT's answer. COLMETADATA declares the shape once, then
one ROW token follows per row, and the values inside a row are encoded
according to the metadata rather than being self-describing. That coupling is
why the column types live here with the encoders rather than in token.py: a
row cannot be encoded without the columns it belongs to.

Measured from a capture of

    SELECT CAST(42 AS int) AS answer, CAST('hello' AS nvarchar(20)) AS greeting,
           CAST(1.5 AS float) AS ratio, CAST(NULL AS int) AS missing

whose 138-byte token stream decodes with nothing left over. Every type here
appeared in it. Nullability is not a column property in the encoding: the
nullable forms carry a length prefix and spend it to say NULL, which is why
INTN and FLTN are used rather than the fixed-width INT4 and FLT8.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum

_USHORT = struct.Struct("<H")
_ULONG = struct.Struct("<I")
_ULONGLONG = struct.Struct("<Q")

# A MAX column says NULL with an all-ones total length rather than the
# 0xffff a sized one uses.
PLP_NULL = b"\xff" * 8

# The collation the reference server declared on its nvarchar column. Opaque
# here; sending what the real server sends is the point.
DEFAULT_COLLATION = bytes.fromhex("0904d00034")

# Observed on every column of the reference result set. Bit 0 marks the column
# nullable; the rest are not interpreted, only reproduced.
DEFAULT_COLUMN_FLAGS = 0x0021

# Two bytes per UTF-16 code unit, and the wire field counts bytes.
NVARCHAR_MAX_BYTES = 0xFFFF - 1

# A declared size of 0xFFFF is not a size, it is the MAX form. Measured from a
# real server answering CAST(REPLICATE('ab', 3000) AS nvarchar(max)): the
# column declares 0xffff, and the value arrives as an 8-byte total length
# followed by length-prefixed chunks and a zero-length terminator.
MAX_MARKER = 0xFFFF

# The size beyond which a sized nvarchar cannot be declared, so MAX is the only
# way to carry the value.
NVARCHAR_SIZED_LIMIT = 4000


class TdsType(IntEnum):
    INTN = 0x26      # observed, length 4
    FLTN = 0x6D      # observed, length 8
    NVARCHAR = 0xE7  # observed, max 40 bytes with a 5-byte collation


class ColumnType:
    """How one column declares itself and encodes its values."""

    def type_info(self) -> bytes:
        raise NotImplementedError

    def encode(self, value: object) -> bytes:
        raise NotImplementedError


@dataclass(frozen=True)
class Integer(ColumnType):
    """INTN. Width 4 was measured; 8 is the same encoding, widened."""

    width: int = 4

    def __post_init__(self) -> None:
        if self.width not in (1, 2, 4, 8):
            raise ValueError(f"INTN width must be 1, 2, 4 or 8, got {self.width}")

    def type_info(self) -> bytes:
        return bytes([TdsType.INTN, self.width])

    def encode(self, value: object) -> bytes:
        if value is None:
            return b"\x00"  # a zero length is how a nullable type says NULL
        number = int(value)
        try:
            return bytes([self.width]) + number.to_bytes(
                self.width, "little", signed=True
            )
        except OverflowError:
            raise ValueError(
                f"{number} does not fit in a {self.width}-byte integer column"
            ) from None


@dataclass(frozen=True)
class Float(ColumnType):
    """FLTN. Width 8 is a double, which is what SQL Server's float returns."""

    width: int = 8

    def __post_init__(self) -> None:
        if self.width not in (4, 8):
            raise ValueError(f"FLTN width must be 4 or 8, got {self.width}")

    def type_info(self) -> bytes:
        return bytes([TdsType.FLTN, self.width])

    def encode(self, value: object) -> bytes:
        if value is None:
            return b"\x00"
        packed = struct.pack("<d" if self.width == 8 else "<f", float(value))
        return bytes([self.width]) + packed


@dataclass(frozen=True)
class NVarChar(ColumnType):
    """NVARCHAR, sized or MAX.

    max_chars of None is the MAX form, which a sized column cannot express:
    past 4000 characters there is no size to declare, so the value has to
    arrive in chunks instead.
    """

    max_chars: int | None = 4000
    collation: bytes = DEFAULT_COLLATION

    @property
    def is_max(self) -> bool:
        return self.max_chars is None

    def type_info(self) -> bytes:
        size = MAX_MARKER if self.is_max else self.max_chars * 2
        return bytes([TdsType.NVARCHAR]) + _USHORT.pack(size) + self.collation

    def encode(self, value: object) -> bytes:
        if self.is_max:
            return self._encode_max(value)

        if value is None:
            # NVARCHAR spends its whole two-byte length on the null marker,
            # where the one-byte types use zero. Sending zero here would be a
            # legitimate empty string instead.
            return b"\xff\xff"
        encoded = str(value).encode("utf-16-le")
        if len(encoded) > NVARCHAR_MAX_BYTES:
            raise ValueError(
                f"value of {len(encoded)} bytes exceeds what a sized nvarchar "
                f"length field can describe; the column needs the MAX form"
            )
        return _USHORT.pack(len(encoded)) + encoded

    def _encode_max(self, value: object) -> bytes:
        """The chunked form: total length, then chunks, then a zero terminator."""
        if value is None:
            return PLP_NULL
        encoded = str(value).encode("utf-16-le")
        if not encoded:
            # An empty MAX value is its own marker; a zero total length followed
            # by a zero chunk would be read as one chunk of nothing.
            return _ULONGLONG.pack(0)
        return (
            _ULONGLONG.pack(len(encoded))
            + _ULONG.pack(len(encoded))
            + encoded
            + _ULONG.pack(0)
        )


@dataclass(frozen=True)
class Column:
    name: str
    type: ColumnType
    flags: int = DEFAULT_COLUMN_FLAGS
    user_type: int = 0

    def metadata(self) -> bytes:
        encoded_name = self.name.encode("utf-16-le")
        return (
            _ULONG.pack(self.user_type)
            + _USHORT.pack(self.flags)
            + self.type.type_info()
            + bytes([len(self.name)])
            + encoded_name
        )


def col_metadata(columns: list[Column]) -> bytes:
    """Declare the shape of the rows that follow."""
    from .token import TokenType

    return (
        bytes([TokenType.COL_METADATA])
        + _USHORT.pack(len(columns))
        + b"".join(column.metadata() for column in columns)
    )


def row(columns: list[Column], values: list[object]) -> bytes:
    """Encode one row against the columns that were declared for it."""
    from .token import TokenType

    if len(values) != len(columns):
        raise ValueError(
            f"row has {len(values)} values but {len(columns)} columns were declared"
        )
    return bytes([TokenType.ROW]) + b"".join(
        column.type.encode(value) for column, value in zip(columns, values)
    )


def _rows(columns: list[Column], rows: list[list[object]]) -> bytes:
    """Encode many rows, resolving each column's encoder once.

    The per-row form looks up column.type.encode for every value, which is a
    pair of attribute lookups per cell. Hoisting them out is worth about a
    fifth of the encoding time on a large result set, and encoding is most of
    what a big SELECT costs once the data is cached.
    """
    marker = bytes([0xD1])       # TokenType.ROW, without the import per row
    encoders = [column.type.encode for column in columns]
    width = len(encoders)

    out = bytearray()
    for values in rows:
        if len(values) != width:
            raise ValueError(
                f"row has {len(values)} values but {width} columns were declared"
            )
        out += marker
        for encode, value in zip(encoders, values):
            out += encode(value)
    return bytes(out)


def result_set(columns: list[Column], rows: list[list[object]]) -> bytes:
    """A whole answer: metadata, the rows, and a DONE carrying the count.

    No columns means no result set, and the answer is a bare DONE. That is not
    the same as a query returning no rows, which still declares its shape.
    Statements like SET and USE produce nothing, and a client sent COLMETADATA
    for one of those reports an invalid cursor state when it later tries to
    read the results it was actually waiting for.
    """
    from .token import DoneStatus, done

    if not columns:
        return done(status=DoneStatus.FINAL)

    return b"".join([
        col_metadata(columns),
        _rows(columns, rows),
        done(status=DoneStatus.COUNT, current_command=SELECT_COMMAND,
             row_count=len(rows)),
    ])


# The reference server reported this in DONE's current-command field after a
# SELECT, where the login response used zero.
SELECT_COMMAND = 0x00C1


class QueryError(Exception):
    """A query could not be answered.

    Carries the number a client will see. The default sits at 50000, where SQL
    Server's user-defined range begins, because an error raised here is this
    bridge's own rather than one of the server's. A handler reporting a missing
    table should pass 208, "invalid object name", which clients already know
    how to present.
    """

    def __init__(self, message: str, *, number: int = 50000, severity: int = 16) -> None:
        super().__init__(message)
        self.number = number
        self.severity = severity


@dataclass(frozen=True)
class Query:
    """One batch to answer, with whatever parameters came with it.

    A dataclass rather than two arguments because RPC supplies parameters and a
    SQL batch does not, and a handler should not have to care which arrived.
    """

    sql: str
    parameters: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class QueryResult:
    """What a query handler returns.

    Columns and rows rather than encoded bytes, so a data source never needs to
    know how TDS puts values on the wire.
    """

    columns: list[Column]
    rows: list[list[object]]

    def encode(self) -> bytes:
        return result_set(self.columns, self.rows)
