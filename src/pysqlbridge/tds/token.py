"""Tokens in the server-to-client stream.

A TABULAR_RESULT packet's payload is a sequence of tokens, each opening with a
one-byte type. Every encoder here reproduces a token from the reference
capture byte for byte, and the tests assert exactly that against the captured
slices.

Lengths inside tokens are little-endian, unlike the packet header's big-endian
length. Two string conventions appear and they are not interchangeable:

    B_VARCHAR   one length byte, counting characters, then UTF-16LE
    US_VARCHAR  two length bytes, counting characters, then UTF-16LE

The collation ENVCHANGE breaks the pattern by carrying five raw bytes behind a
length that counts bytes rather than characters, because a collation is not
text.

Measured 2026-09-06 from frame 24, whose 459-byte token stream decodes with
nothing left over.
"""

from __future__ import annotations

import struct
from enum import IntEnum, IntFlag

from .packet import (
    ROW_COUNT_WIDENED_IN,
    TDS_70,
    TDS_71,
    TDS_72,
    TDS_73A,
    TDS_73B,
    TDS_74,
)

_USHORT = struct.Struct("<H")
_ULONG = struct.Struct("<I")

MAX_TOKEN_PAYLOAD = 0xFFFF


class TokenType(IntEnum):
    """Token type bytes. Every value here was observed in the capture."""

    COL_METADATA = 0x81
    ERROR = 0xAA
    INFO = 0xAB
    LOGIN_ACK = 0xAD
    FEATURE_EXT_ACK = 0xAE
    ROW = 0xD1
    ENV_CHANGE = 0xE3
    SSPI = 0xED
    DONE = 0xFD


class EnvChangeType(IntEnum):
    DATABASE = 1
    LANGUAGE = 2
    PACKET_SIZE = 4
    SQL_COLLATION = 7


class DoneStatus(IntFlag):
    FINAL = 0x0000
    MORE = 0x0001
    ERROR = 0x0002
    COUNT = 0x0010
    # What acknowledges a cancellation. A client that sent one waits for it
    # and will not reuse the connection until it arrives.
    ATTENTION = 0x0020


# The versions live with the packet framing because the request parsers need
# them too, and those sit below this module. LOGINACK writes one big-endian
# while LOGIN7 writes the same value little-endian, so a server that echoed
# back the bytes it parsed would send 04 00 00 74 for 74 00 00 04.

# The versions a client may ask for, oldest first. A login names one of these
# and the server answers with the highest it can speak that is no newer than
# what was asked, which is what makes an old client work at all: the legacy
# "SQL Server" ODBC driver asks for 7.1, and told 7.4 it decides it is
# talking to something from before 6.5 and hangs up.
KNOWN_VERSIONS = (TDS_70, TDS_71, TDS_72, TDS_73A, TDS_73B, TDS_74)

# The line number in INFO and ERROR grew from two bytes to four in TDS 7.2,
# the same version that widened the row count in DONE.
LINE_NUMBER_WIDENED_IN = TDS_72


def negotiate(requested: int) -> int:
    """The version to answer a login with.

    Never newer than the client asked for, and never older than the oldest
    this speaks. An unrecognised value is treated as the newest, because a
    client naming a version from the future is expecting to be negotiated
    down rather than refused.
    """
    if requested in KNOWN_VERSIONS:
        return requested
    older = [v for v in KNOWN_VERSIONS if v < requested]
    return max(older) if older else TDS_70

# The reference server's own name for itself, and note the two trailing nulls:
# the B_VARCHAR counts 22 characters, not the 20 the text has. Trimming them
# produces a token two bytes short of the real one.
SERVER_PROGRAM_NAME = "Microsoft SQL Server\x00\x00"

# The collation the reference server announced. Five opaque bytes; nothing
# here interprets them, and sending what the real server sends is the point.
DEFAULT_COLLATION = bytes.fromhex("0904d00034")


def _b_varchar(text: str) -> bytes:
    encoded = text.encode("utf-16-le")
    return bytes([len(text)]) + encoded


def _us_varchar(text: str) -> bytes:
    encoded = text.encode("utf-16-le")
    return _USHORT.pack(len(text)) + encoded


def _token(kind: TokenType, body: bytes) -> bytes:
    if len(body) > MAX_TOKEN_PAYLOAD:
        raise ValueError(
            f"{kind.name} body of {len(body)} bytes exceeds the "
            f"{MAX_TOKEN_PAYLOAD}-byte token length field"
        )
    return bytes([kind]) + _USHORT.pack(len(body)) + body


def sspi_token(blob: bytes) -> bytes:
    """Frame an SSPI blob for the server-to-client stream.

    The client's half is not symmetric: it arrives in a packet typed SSPI
    carrying the blob raw, with no token byte and no length. Only this
    direction is framed, because only this direction shares a stream with
    other tokens.
    """
    return _token(TokenType.SSPI, blob)


def env_change(kind: EnvChangeType, new: str | bytes, old: str | bytes = "") -> bytes:
    """Announce a changed session setting.

    Collation values are bytes and are length-prefixed by byte count.
    Everything else is text, length-prefixed by character count.
    """
    def encode(value: str | bytes) -> bytes:
        if isinstance(value, bytes):
            return bytes([len(value)]) + value
        return _b_varchar(value)

    return _token(TokenType.ENV_CHANGE, bytes([kind]) + encode(new) + encode(old))


def _message_body(
    number: int,
    message: str,
    state: int,
    severity: int,
    server: str,
    procedure: str,
    line: int,
    tds_version: int,
) -> bytes:
    """The shared body of INFO and ERROR.

    The line number is four bytes from TDS 7.2 and two before it. Sending four
    to a 7.1 client puts two bytes it does not expect between this token and
    the next, and everything after is read at the wrong offset.
    """
    counted = _ULONG if tds_version >= LINE_NUMBER_WIDENED_IN else _USHORT
    return (
        _ULONG.pack(number)
        + bytes([state, severity])
        + _us_varchar(message)
        + _b_varchar(server)
        + _b_varchar(procedure)
        + counted.pack(line)
    )


def info(
    number: int,
    message: str,
    *,
    state: int = 1,
    severity: int = 0,
    server: str = "",
    procedure: str = "",
    line: int = 1,
    tds_version: int = TDS_74,
) -> bytes:
    """An informational message the client will surface to its user.

    The reference server sends two of these during login, reporting the
    database and language it selected. Clients display them, so a bare
    LOGINACK is a quieter login than a real one.
    """
    return _token(TokenType.INFO, _message_body(
        number, message, state, severity, server, procedure, line, tds_version
    ))


def error(
    number: int,
    message: str,
    *,
    state: int = 1,
    severity: int = 16,
    server: str = "",
    procedure: str = "",
    line: int = 1,
    tds_version: int = TDS_74,
) -> bytes:
    """Report a failure the client should surface as an error.

    Same body as INFO; the token byte is what makes a client raise rather than
    log. Severity 16 is the conventional level for an error the caller caused
    and can correct, which is the class this project produces.
    """
    return _token(TokenType.ERROR, _message_body(
        number, message, state, severity, server, procedure, line, tds_version
    ))


def error_response(
    number: int,
    message: str,
    *,
    server: str = "",
    severity: int = 16,
    tds_version: int = TDS_74,
) -> bytes:
    """An ERROR token and the DONE that closes the failed batch.

    A client that receives the error without a DONE keeps waiting, because
    nothing has told it the batch finished.
    """
    return error(
        number, message, server=server, severity=severity,
        tds_version=tds_version,
    ) + done(status=DoneStatus.ERROR, tds_version=tds_version)


def login_ack(
    *,
    version: tuple[int, int, int],
    program: str = SERVER_PROGRAM_NAME,
    tds_version: int = TDS_74,
    interface: int = 1,
) -> bytes:
    """Tell the client the login succeeded, and what it is talking to."""
    major, minor, build = version
    return _token(
        TokenType.LOGIN_ACK,
        bytes([interface])
        + struct.pack(">I", tds_version)   # big-endian here, see TDS_74
        + _b_varchar(program)
        + bytes([major, minor, (build >> 8) & 0xFF, build & 0xFF]),
    )


def done(
    *,
    status: DoneStatus = DoneStatus.FINAL,
    current_command: int = 0,
    row_count: int = 0,
    tds_version: int = TDS_74,
) -> bytes:
    """Close off a response.

    The row count is 64-bit from TDS 7.2 onward and 32-bit before it, so a
    client negotiated down to 7.1 reads four bytes of count and then four
    bytes of whatever came next as the start of the following token.
    """
    counted = ("<Q" if tds_version >= ROW_COUNT_WIDENED_IN else "<I")
    return (
        bytes([TokenType.DONE])
        + _USHORT.pack(status)
        + _USHORT.pack(current_command)
        + struct.pack(counted, row_count)
    )


def login_response(
    *,
    version: tuple[int, int, int],
    server_name: str,
    database: str = "master",
    language: str = "us_english",
    packet_size: int = 4096,
    collation: bytes = DEFAULT_COLLATION,
    tds_version: int = TDS_74,
) -> bytes:
    """The whole token stream a client gets when its login succeeds.

    Ordered as the reference server ordered it. FEATUREEXTACK is deliberately
    absent: the real server sent one because the client asked for session
    recovery, data classification and UTF-8 support, none of which this
    project implements, and acknowledging a feature that does not work would
    be worse than staying quiet about it.
    """
    return b"".join([
        env_change(EnvChangeType.DATABASE, database, database),
        info(
            5701,
            f"Changed database context to '{database}'.",
            state=2,
            server=server_name,
            tds_version=tds_version,
        ),
        env_change(EnvChangeType.SQL_COLLATION, collation),
        env_change(EnvChangeType.LANGUAGE, language),
        info(
            5703,
            f"Changed language setting to {language}.",
            server=server_name,
            tds_version=tds_version,
        ),
        login_ack(version=version, tds_version=tds_version),
        env_change(EnvChangeType.PACKET_SIZE, str(packet_size), str(packet_size)),
        done(tds_version=tds_version),
    ])
