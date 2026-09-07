"""RPC, the packet that carries a parameterised query.

Clients do not send everything as a SQL batch. Anything with a parameter, and
every catalog query .NET issues, arrives as an RPC call to sp_executesql:

    proc: well-known id 10 (ExecuteSql)
    param 1 ''      nvarchar  the statement
    param 2 ''      nvarchar  '@id int'
    param 3 '@id'   intn      1

That is measured, not assumed: a client asked for its table list and sent 922
bytes shaped exactly like that.

The layout after the same 22-byte ALL_HEADERS block a SQL batch uses:

    NameLenProcID  2 bytes   0xFFFF means a well-known id follows, otherwise
                             this is a character count for the procedure name
    OptionFlags    2 bytes
    parameters     name, status, TYPE_INFO, then the value

Reading a parameter's value requires knowing its type, because the value is not
self-describing: the width and the length prefix both come from TYPE_INFO. An
unsupported type therefore stops parsing rather than being skipped, since there
is no way to know how many bytes it occupied.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

from .packet import ALL_HEADERS_ADDED_IN, TDS_74, TdsProtocolError

_USHORT = struct.Struct("<H")
_ULONG = struct.Struct("<I")

# A well-known procedure is identified by number instead of by name.
PROC_ID_SENTINEL = 0xFFFF

# nvarchar(max) and friends declare this instead of a size, and their values
# arrive in length-prefixed chunks rather than as one run of bytes.
PLP_MARKER = 0xFFFF
PLP_NULL = 0xFFFFFFFFFFFFFFFF
PLP_UNKNOWN_LENGTH = 0xFFFFFFFFFFFFFFFE

NULL_VARIABLE_LENGTH = 0xFFFF

# What a text, ntext or image parameter writes where its length goes when
# it has no value.
NULL_LONG = 0xFFFFFFFF


class ProcId(IntEnum):
    """The well-known procedures, by the number that stands in for the name."""

    CURSOR = 1
    CURSOR_OPEN = 2
    CURSOR_PREPARE = 3
    CURSOR_EXECUTE = 4
    CURSOR_PREP_EXEC = 5
    CURSOR_UNPREPARE = 6
    CURSOR_FETCH = 7
    CURSOR_OPTION = 8
    CURSOR_CLOSE = 9
    EXECUTE_SQL = 10
    PREPARE = 11
    EXECUTE = 12
    PREP_EXEC = 13
    PREP_EXEC_RPC = 14
    UNPREPARE = 15


# Fixed-width types carry no length anywhere: the type byte implies the size.
_FIXED_WIDTH = {
    0x1F: 0,   # NULL
    0x30: 1,   # INT1
    0x32: 1,   # BIT
    0x34: 2,   # INT2
    0x38: 4,   # INT4
    0x3A: 4,   # DATETIME4
    0x3B: 4,   # FLT4
    0x3C: 8,   # MONEY
    0x3D: 8,   # DATETIME
    0x3E: 8,   # FLT8
    0x7A: 4,   # MONEY4
    0x7F: 8,   # INT8
}

# Types whose TYPE_INFO is a single maximum-length byte, and whose value is
# preceded by a single actual-length byte. A zero length is NULL.
_BYTE_LEN = frozenset({
    0x24,  # GUID
    0x26,  # INTN
    0x68,  # BITN
    0x6D,  # FLTN
    0x6E,  # MONEYN
    0x6F,  # DATETIMN
})

# Decimal and numeric add precision and scale after the maximum length.
_DECIMAL = frozenset({0x6A, 0x6C})

# Character types: two-byte maximum, five-byte collation, two-byte value length.
_CHAR = frozenset({0xA7, 0xAF, 0xE7, 0xEF})

# Binary types: two-byte maximum, two-byte value length, no collation.
_BINARY = frozenset({0xA5, 0xAD})

# text, ntext and image. Deprecated for twenty years and still sent: SSMS
# passes a filter to a catalog query as ntext. Their value is not written
# where the others are, so a parameter of one of these used to end the
# connection rather than the call.
_LONG = frozenset({0x23, 0x63, 0x22})


@dataclass(frozen=True)
class Parameter:
    name: str
    value: object
    status: int = 0

    @property
    def is_output(self) -> bool:
        return bool(self.status & 0x01)


@dataclass(frozen=True)
class RpcRequest:
    """One remote procedure call."""

    procedure: str
    parameters: list[Parameter]
    proc_id: ProcId | None = None
    option_flags: int = 0

    @property
    def is_execute_sql(self) -> bool:
        return self.proc_id is ProcId.EXECUTE_SQL or (
            self.procedure.lower() == "sp_executesql"
        )

    @property
    def sql(self) -> str | None:
        """The statement, when this call is one that carries one.

        sp_executesql puts it in the first parameter, which is unnamed.
        """
        if not self.is_execute_sql or not self.parameters:
            return None
        statement = self.parameters[0].value
        return statement if isinstance(statement, str) else None


def _read_plp(payload: bytes, at: int) -> tuple[bytes, int]:
    """Read a chunked value, the form the MAX types use."""
    total, = struct.unpack_from("<Q", payload, at)
    at += 8
    if total == PLP_NULL:
        return b"", at
    chunks = bytearray()
    while True:
        size, = _ULONG.unpack_from(payload, at)
        at += 4
        if size == 0:
            break
        chunks += payload[at:at + size]
        at += size
    return bytes(chunks), at


def _read_value(payload: bytes, at: int, type_id: int) -> tuple[object, int]:
    """Read one parameter's TYPE_INFO and its value."""
    if type_id in _FIXED_WIDTH:
        width = _FIXED_WIDTH[type_id]
        raw = payload[at:at + width]
        return _decode_number(raw, type_id), at + width

    if type_id in _BYTE_LEN or type_id in _DECIMAL:
        at += 1                       # declared maximum
        if type_id in _DECIMAL:
            at += 2                   # precision, scale
        length = payload[at]
        at += 1
        if length == 0:
            return None, at
        raw = payload[at:at + length]
        return _decode_number(raw, type_id), at + length

    if type_id in _CHAR or type_id in _BINARY:
        declared, = _USHORT.unpack_from(payload, at)
        at += 2
        if type_id in _CHAR:
            at += 5                   # collation
        if declared == PLP_MARKER:
            raw, at = _read_plp(payload, at)
        else:
            length, = _USHORT.unpack_from(payload, at)
            at += 2
            if length == NULL_VARIABLE_LENGTH:
                return None, at
            raw = payload[at:at + length]
            at += length
        if type_id in _BINARY:
            return raw, at
        if type_id in (0xE7, 0xEF):
            return raw.decode("utf-16-le"), at
        return raw.decode("latin-1"), at

    if type_id in _LONG:
        return _read_long(payload, at, type_id)

    raise TdsProtocolError(
        f"RPC parameter has type 0x{type_id:02x}, which this project cannot "
        f"read; its value's length is unknown so the rest of the call cannot "
        f"be parsed"
    )


def _read_long(payload: bytes, at: int, type_id: int) -> tuple[object, int]:
    """One text, ntext or image value, as a parameter carries it.

    A four-byte declared maximum, a collation for the two text ones, then a
    four-byte length and the bytes. Nothing else: a row sent the other way
    puts a pointer and a timestamp in front of the length, and a parameter,
    which is a value and not a place, does not. Read from a capture of what
    a client sends rather than from what the row direction looks like.
    """
    at += 4                                   # the declared maximum length
    if type_id in (0x23, 0x63):
        at += 5                               # collation
    length, = _ULONG.unpack_from(payload, at)
    at += 4
    if length == NULL_LONG:
        return None, at
    raw = payload[at:at + length]
    at += length
    if type_id == 0x22:
        return raw, at
    return raw.decode("utf-16-le" if type_id == 0x63 else "latin-1"), at


def _decode_number(raw: bytes, type_id: int) -> object:
    if not raw:
        return None
    if type_id in (0x3B, 0x3E, 0x6D):
        return struct.unpack("<f" if len(raw) == 4 else "<d", raw)[0]
    if type_id in (0x32, 0x68):
        return bool(raw[0])
    if type_id == 0x24:
        return raw
    return int.from_bytes(raw, "little", signed=True)


def parse_rpc(payload: bytes, tds_version: int = TDS_74) -> RpcRequest:
    """Parse an RPC payload, after its ALL_HEADERS block if it has one.

    The block arrived in TDS 7.2. A 7.1 client starts at the procedure name,
    and reading its name length and option flags as a header length produced a
    seven-megabyte block inside a hundred-byte packet.
    """
    if tds_version < ALL_HEADERS_ADDED_IN:
        at = 0
    else:
        if len(payload) < 4:
            raise TdsProtocolError(
                f"RPC payload is {len(payload)} bytes, too short for an "
                f"ALL_HEADERS length"
            )

        headers_length, = _ULONG.unpack_from(payload, 0)
        if headers_length < 4 or headers_length > len(payload):
            raise TdsProtocolError(
                f"RPC declares a {headers_length}-byte ALL_HEADERS block, "
                f"which does not fit in {len(payload)} bytes"
            )
        at = headers_length

    name_length, = _USHORT.unpack_from(payload, at)
    at += 2
    proc_id: ProcId | None = None
    if name_length == PROC_ID_SENTINEL:
        raw_id, = _USHORT.unpack_from(payload, at)
        at += 2
        try:
            proc_id = ProcId(raw_id)
        except ValueError:
            raise TdsProtocolError(f"unknown well-known procedure id {raw_id}") from None
        procedure = f"sp_{proc_id.name.lower()}" if proc_id else str(raw_id)
        if proc_id is ProcId.EXECUTE_SQL:
            procedure = "sp_executesql"
    else:
        procedure = payload[at:at + name_length * 2].decode("utf-16-le")
        at += name_length * 2

    option_flags, = _USHORT.unpack_from(payload, at)
    at += 2

    parameters: list[Parameter] = []
    while at < len(payload):
        name_chars = payload[at]
        at += 1
        name = payload[at:at + name_chars * 2].decode("utf-16-le")
        at += name_chars * 2
        status = payload[at]
        at += 1
        type_id = payload[at]
        at += 1
        value, at = _read_value(payload, at, type_id)
        parameters.append(Parameter(name=name, value=value, status=status))

    return RpcRequest(
        procedure=procedure,
        parameters=parameters,
        proc_id=proc_id,
        option_flags=option_flags,
    )
