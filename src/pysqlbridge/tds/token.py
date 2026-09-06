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


# TDS 7.4. LOGINACK writes this big-endian while LOGIN7 writes the same value
# little-endian, so a server that echoed back the bytes it parsed would send
# 04 00 00 74 where the client expects 74 00 00 04.
TDS_74 = 0x74000004

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


def info(
    number: int,
    message: str,
    *,
    state: int = 1,
    severity: int = 0,
    server: str = "",
    procedure: str = "",
    line: int = 1,
) -> bytes:
    """An informational message the client will surface to its user.

    The reference server sends two of these during login, reporting the
    database and language it selected. Clients display them, so a bare
    LOGINACK is a quieter login than a real one.
    """
    body = (
        _ULONG.pack(number)
        + bytes([state, severity])
        + _us_varchar(message)
        + _b_varchar(server)
        + _b_varchar(procedure)
        + _ULONG.pack(line)
    )
    return _token(TokenType.INFO, body)


def error(
    number: int,
    message: str,
    *,
    state: int = 1,
    severity: int = 16,
    server: str = "",
    procedure: str = "",
    line: int = 1,
) -> bytes:
    """Report a failure the client should surface as an error.

    Same body as INFO; the token byte is what makes a client raise rather than
    log. Severity 16 is the conventional level for an error the caller caused
    and can correct, which is the class this project produces.
    """
    body = (
        _ULONG.pack(number)
        + bytes([state, severity])
        + _us_varchar(message)
        + _b_varchar(server)
        + _b_varchar(procedure)
        + _ULONG.pack(line)
    )
    return _token(TokenType.ERROR, body)


def error_response(
    number: int,
    message: str,
    *,
    server: str = "",
    severity: int = 16,
) -> bytes:
    """An ERROR token and the DONE that closes the failed batch.

    A client that receives the error without a DONE keeps waiting, because
    nothing has told it the batch finished.
    """
    return error(number, message, server=server, severity=severity) + done(
        status=DoneStatus.ERROR
    )


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
) -> bytes:
    """Close off a response. The row count is 64-bit in TDS 7.2 and later."""
    return (
        bytes([TokenType.DONE])
        + _USHORT.pack(status)
        + _USHORT.pack(current_command)
        + struct.pack("<Q", row_count)
    )


def login_response(
    *,
    version: tuple[int, int, int],
    server_name: str,
    database: str = "master",
    language: str = "us_english",
    packet_size: int = 4096,
    collation: bytes = DEFAULT_COLLATION,
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
        ),
        env_change(EnvChangeType.SQL_COLLATION, collation),
        env_change(EnvChangeType.LANGUAGE, language),
        info(
            5703,
            f"Changed language setting to {language}.",
            server=server_name,
        ),
        login_ack(version=version),
        env_change(EnvChangeType.PACKET_SIZE, str(packet_size), str(packet_size)),
        done(),
    ])
