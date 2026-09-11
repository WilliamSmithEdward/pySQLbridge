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

import datetime
import struct
import uuid
from dataclasses import dataclass, field
from enum import IntEnum

from .packet import TDS_74, USER_TYPE_WIDENED_IN

_USHORT = struct.Struct("<H")
_ULONG = struct.Struct("<I")
_ULONGLONG = struct.Struct("<Q")

# A MAX column says NULL with an all-ones total length rather than the
# 0xffff a sized one uses.
PLP_NULL = b"\xff" * 8

# The collation the reference server declared on its nvarchar column. Opaque
# here; sending what the real server sends is the point.
DEFAULT_COLLATION = bytes.fromhex("0904d00034")

# Observed on every column of the reference result set, which selected CAST
# expressions: bit 0 marks the column nullable and bit 5 marks it computed,
# which a CAST is.
DEFAULT_COLUMN_FLAGS = 0x0021

# What SQL Server sends on the columns of its own catalog rowsets: bit 3 of
# the updateable field rather than the computed bit, since these come from a
# procedure rather than an expression. Measured from sp_columns_100_rowset2,
# where the two NOT NULL columns are the only ones without bit 0.
CATALOG_COLUMN_FLAGS = 0x0009

# The nullable bit. A fixed-width type has no way to say NULL, so declaring
# one nullable is a promise the encoding cannot keep, and a provider that
# believes it goes looking for an indicator that was never sent.
NULLABLE_FLAG = 0x0001

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

    # Measured from a real SQL Server 2025 answering
    #
    #     SELECT CAST(1 AS bit), CAST(NULL AS bit),
    #            CAST('6F9619FF-...' AS uniqueidentifier),
    #            CAST('2026-09-06T12:34:56' AS datetime),
    #            CAST(0x0102030405 AS binary(5)), CAST(NULL AS varbinary(10))
    #
    # captured through a proxy with encryption negotiated off. The OLE DB
    # catalog rowsets declare all four, and a provider reading nvarchar where
    # it expects uniqueidentifier does not report a mismatch.
    BITN = 0x68       # observed, length 1
    GUIDN = 0x24      # observed, length 16
    DATETIMN = 0x6F   # observed, length 8
    BIGVARBINARY = 0xA5
    BIGBINARY = 0xAD  # observed, size 5, for a fixed binary(n)
    INT2 = 0x34       # observed, the fixed two-byte int, never null


class ColumnType:
    """How one column declares itself and encodes its values."""

    # Whether the encoding can carry a NULL. True for every nullable form,
    # which is nearly all of them: the N in INTN and FLTN is exactly this.
    nullable = True

    def type_info(self) -> bytes:
        raise NotImplementedError

    def encode(self, value: object) -> bytes:
        raise NotImplementedError


@dataclass(frozen=True)
class Integer(ColumnType):
    """INTN. Width 4 was measured; 8 is the same encoding, widened.

    One byte is tinyint, and tinyint is unsigned: SQL Server hands 200 back
    as 200 and a client reads it as a byte. Every wider one is signed.
    """

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
                self.width, "little", signed=self.width > 1
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


def as_utf16(text: str) -> bytes:
    """Text as the code units the wire carries.

    surrogatepass, because nvarchar is a string of UTF-16 code units and not
    a string of characters, and one of them can be half of a pair with the
    other half missing. A JSON document is allowed to spell one: "\\ud800"
    decodes to a lone surrogate and an API that builds its JSON out of UTF-16
    by hand serves them. Refusing to encode it took down the answer to a
    query over a row that held one.

    Measured: SQL Server stores NCHAR(0xD800) as the code unit D800 and hands
    it back unchanged, and UNICODE() reads it back as 55296. It neither
    refuses the value nor replaces it, so neither does this.
    """
    return text.encode("utf-16-le", "surrogatepass")


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
        encoded = as_utf16(str(value))
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
        encoded = as_utf16(str(value))
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
class Bit(ColumnType):
    """BITN. One length byte, then one byte holding 0 or 1."""

    def type_info(self) -> bytes:
        return bytes([TdsType.BITN, 1])

    def encode(self, value: object) -> bytes:
        if value is None:
            return b"\x00"
        return b"\x01" + (b"\x01" if value else b"\x00")


@dataclass(frozen=True)
class UniqueIdentifier(ColumnType):
    """GUIDN. Sixteen bytes in the mixed-endian layout Windows uses.

    Accepts a uuid, a string, or raw bytes. The first three fields are little
    endian and the last two big endian, which is what uuid.bytes_le produces
    and what the captured row carried for
    6F9619FF-8B86-D011-B42D-00C04FC964FF.
    """

    def type_info(self) -> bytes:
        return bytes([TdsType.GUIDN, 16])

    def encode(self, value: object) -> bytes:
        if value is None:
            return b"\x00"
        if isinstance(value, bytes):
            raw = value[:16].ljust(16, b"\x00")
        else:
            raw = uuid.UUID(str(value)).bytes_le
        return b"\x10" + raw


@dataclass(frozen=True)
class DateTime(ColumnType):
    """DATETIMN, the eight-byte form.

    Four bytes of days since 1900-01-01, then four of three-hundredths of a
    second since midnight. Confirmed against the capture: 0x0000b4bd days is
    2026-09-06, and 0x00cf5940 is 13,588,800 ticks, which is 45,296 seconds,
    which is 12:34:56 to the second.
    """

    EPOCH = datetime.date(1900, 1, 1)

    def type_info(self) -> bytes:
        return bytes([TdsType.DATETIMN, 8])

    def encode(self, value: object) -> bytes:
        if value is None:
            return b"\x00"
        if isinstance(value, datetime.datetime):
            moment = value
        elif isinstance(value, datetime.date):
            moment = datetime.datetime(value.year, value.month, value.day)
        else:
            moment = datetime.datetime.fromisoformat(str(value))
        days = (moment.date() - self.EPOCH).days
        seconds = (moment.hour * 3600 + moment.minute * 60 + moment.second
                   + moment.microsecond / 1_000_000)
        return b"\x08" + struct.pack("<iI", days, round(seconds * 300))


@dataclass(frozen=True)
class VarBinary(ColumnType):
    """BIGVARBINARY. A two-byte size, and 0xffff for NULL."""

    size: int = 8000

    def type_info(self) -> bytes:
        return bytes([TdsType.BIGVARBINARY]) + _USHORT.pack(self.size)

    def encode(self, value: object) -> bytes:
        if value is None:
            return _USHORT.pack(MAX_MARKER)
        raw = bytes(value)[:self.size]
        return _USHORT.pack(len(raw)) + raw


@dataclass(frozen=True)
class SmallInt(ColumnType):
    """INT2, the fixed form: two bytes, no length prefix, never NULL.

    Distinct from Integer(2), which is INTN and can say NULL by spending its
    length byte. The OLE DB columns rowset declares DATA_TYPE this way because
    every row has one, and a provider told the column might be NULL where the
    real server promised it never is refuses the rowset.
    """

    nullable = False

    def type_info(self) -> bytes:
        return bytes([TdsType.INT2])

    def encode(self, value: object) -> bytes:
        return struct.pack("<h", int(value or 0))


@dataclass(frozen=True)
class Binary(ColumnType):
    """BIGBINARY, the fixed-width form.

    Declared separately from VarBinary because the capture showed binary(5)
    and varbinary(10) using different type bytes, and a client reads the
    declaration rather than inferring from the value.
    """

    size: int = 8000

    def type_info(self) -> bytes:
        return bytes([TdsType.BIGBINARY]) + _USHORT.pack(self.size)

    def encode(self, value: object) -> bytes:
        if value is None:
            return _USHORT.pack(MAX_MARKER)
        raw = bytes(value)[:self.size].ljust(self.size, bytes(1))
        return _USHORT.pack(len(raw)) + raw


def _name_length(name: str) -> int:
    """How long a column name is, refused if it will not fit the field.

    One byte holds it, so 255 is the ceiling. Reached once by a select list
    written without commas, which parsed as one column named after the rest
    of the statement and dropped the connection with an error about a byte
    being out of range.
    """
    if len(name) > 255:
        raise ValueError(
            f"a column name of {len(name)} characters cannot be sent; the "
            f"length field holds 255"
        )
    return len(name)


@dataclass(frozen=True)
class Column:
    name: str
    type: ColumnType
    flags: int = DEFAULT_COLUMN_FLAGS
    user_type: int = 0

    def metadata(self, tds_version: int = TDS_74) -> bytes:
        """This column's entry in COLMETADATA.

        The user type is four bytes from TDS 7.2 and two before it. Two extra
        bytes per column is enough to make the whole declaration unreadable,
        which a client reports as a protocol error in the stream rather than
        as anything naming a column.
        """
        encoded_name = as_utf16(self.name)
        # A fixed-width column cannot say NULL, so the flag is cleared here
        # rather than trusted from the caller: the two have to agree, and a
        # client reading a nullable declaration on a fixed type looks for an
        # indicator byte that is not in the row.
        flags = self.flags if self.type.nullable else self.flags & ~NULLABLE_FLAG
        user_type = _ULONG if tds_version >= USER_TYPE_WIDENED_IN else _USHORT
        return (
            user_type.pack(self.user_type)
            + _USHORT.pack(flags)
            + self.type.type_info()
            + bytes([_name_length(self.name)])
            + encoded_name
        )


def col_metadata(columns: list[Column], tds_version: int = TDS_74) -> bytes:
    """Declare the shape of the rows that follow."""
    from .token import TokenType

    return (
        bytes([TokenType.COL_METADATA])
        + _USHORT.pack(len(columns))
        + b"".join(column.metadata(tds_version) for column in columns)
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


def result_set(columns: list[Column], rows: list[list[object]],
               tds_version: int = TDS_74, following: tuple = (),
               error: QueryError | None = None, server: str = "") -> bytes:
    """A whole answer: metadata, the rows, and a DONE carrying the count.

    No columns means no result set, and the answer is a bare DONE. That is not
    the same as a query returning no rows, which still declares its shape.
    Statements like SET and USE produce nothing, and a client sent COLMETADATA
    for one of those reports an invalid cursor state when it later tries to
    read the results it was actually waiting for.

    A batch that read several times answers with each of them in turn. Every
    DONE but the last says more is coming, which is how a client knows to keep
    reading: SSMS opens a connection with three reads in one batch and takes
    the third, so a server that sent only the first is a server it will not
    connect to.
    """
    from .token import DoneStatus, done
    from .token import error as error_token

    def three(one: tuple) -> tuple:
        """An entry as (columns, rows, error), however it was written."""
        return one if len(one) == 3 else (*one, None)

    answers = [(columns, rows, error), *(three(one) for one in following)]
    written = []
    for at, (its_columns, its_rows, its_error) in enumerate(answers):
        last = at == len(answers) - 1
        more = DoneStatus.FINAL if last else DoneStatus.MORE
        if its_error is not None:
            # A statement that failed, with the batch still running. The
            # DONE says both: this one ended in an error, and there is more
            # to read. Without the second a client stops here and reads the
            # rest as the answer to its next request.
            if its_columns:
                # A read that failed while evaluating a row, not while
                # binding: a real server had already sent the shape by
                # then, so the empty result set goes out ahead of the
                # error. Measured, and only for the errors that keep it.
                written.append(col_metadata(its_columns, tds_version))
            written.append(error_token(
                its_error.number, str(its_error),
                severity=its_error.severity, server=server,
                tds_version=tds_version,
            ))
            written.append(done(status=DoneStatus.ERROR | more,
                                tds_version=tds_version))
            continue
        if not its_columns:
            written.append(done(status=more, tds_version=tds_version))
            continue
        written.append(col_metadata(its_columns, tds_version))
        written.append(_rows(its_columns, its_rows))
        written.append(done(status=DoneStatus.COUNT | more,
                            current_command=SELECT_COMMAND,
                            row_count=len(its_rows), tds_version=tds_version))
    return b"".join(written)


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

    def __init__(self, message: str, *, number: int = 50000,
                 severity: int = 16, state: int = 1,
                 carries_on: bool | None = None,
                 columns: list | None = None,
                 following: tuple = ()) -> None:
        super().__init__(message)
        self.number = number
        self.severity = severity
        # The errors the same failure sends after this one, before the DONE
        # that ends the batch. A batch that will not compile can fail in more
        # than one way, and a real server reports each: measured, a quote
        # left open is msg 105 and then msg 102 near the text it left open,
        # and a misplaced keyword with an unclosed comment after it is 156
        # and then 113. This one is still the error: its number is the one a
        # client raises.
        self.following = following
        # The shape a failed read had already declared before the row that
        # failed to evaluate. A real server sends an empty result set with
        # these columns in front of such an error, having bound the query
        # before it ran; this carries them so the error is rendered the
        # same way. None where the shape is not known, which is a bind or
        # syntax error, or a read whose columns cannot be typed without
        # running it: there a real server sends no metadata either.
        self.columns = columns
        # What ERROR_STATE() answers. One unless something said otherwise,
        # which is what a real server reports for everything this raises of
        # its own; a RAISERROR carries the state it was given.
        self.state = state
        # Whether the batch around this error carries on past it. Left None,
        # the number decides, which is how every error a statement runs into
        # is settled. An error a client asked for is the exception: measured,
        # a batch goes on past a RAISERROR and stops at a THROW, and the
        # number says neither, because both carry one the client chose.
        #
        # False carries two more measured facts, both of them THROW's, and
        # THROW is the only thing that sets it: what the statements before it
        # answered is still the client's, and the error reaches out of an
        # EXEC of text to end the batch that ran it, where a RAISERROR
        # inside one does not.
        self.carries_on = carries_on


@dataclass(frozen=True)
class Query:
    """One batch to answer, with whatever parameters came with it.

    A dataclass rather than two arguments because RPC supplies parameters and a
    SQL batch does not, and a handler should not have to care which arrived.
    """

    sql: str
    parameters: dict[str, object] = field(default_factory=dict)
    # Set when the client called a procedure by name rather than sending SQL.
    # Which procedures exist is the handler's business, not the protocol's.
    procedure: str | None = None
    arguments: list[object] = field(default_factory=list)
    # Where a connection keeps what belongs to it alone, which so far is the
    # tables it made for itself. One dict per connection, handed in rather
    # than looked up, so nothing here has to know what a connection is.
    session: dict = field(default_factory=dict)


@dataclass(frozen=True)
class QueryResult:
    """What a query handler returns.

    Columns and rows rather than encoded bytes, so a data source never needs to
    know how TDS puts values on the wire.
    """

    columns: list[Column]
    rows: list[list[object]]
    # The result sets after this one, when a batch read more than once.
    following: tuple = ()
    # Set where this answer is a statement that failed rather than one that
    # read: a batch goes on past some errors, so a failure can arrive among
    # the answers instead of in place of them. Columns and rows are empty,
    # because nothing came back from it.
    error: QueryError | None = None

    def encode(self, tds_version: int = TDS_74, server: str = "") -> bytes:
        return result_set(
            self.columns, self.rows, tds_version,
            following=tuple((one.columns, one.rows, one.error)
                            for one in self.following),
            error=self.error, server=server,
        )
