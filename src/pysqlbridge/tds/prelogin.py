"""PRELOGIN, the first message either side sends.

The payload is a table of fixed-size entries followed by a blob the entries
point into:

    token   1 byte      which option
    offset  2 bytes     where its data starts, from the payload's first byte
    length  2 bytes     how many bytes of data
    ...
    0xFF                terminator, no offset or length

Order and presence both matter on the wire. A server that answers with the
options it feels like sending, in its own order, is not answering the way SQL
Server does, so parsing keeps the entries as they arrived and building emits
them as given. Options carrying zero bytes are still present: the reference
server sends THREADID and TRACEID with length 0 rather than omitting them.

Measured 2026-09-06 against SQL Server 2025 (17.0.1000.7). See
docs/tds-login-handshake.md.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

from .packet import TdsProtocolError

TERMINATOR = 0xFF
_ENTRY = struct.Struct(">BHH")
_ENTRY_SIZE = _ENTRY.size          # 5
_VERSION = struct.Struct(">BBHH")


class PreloginOption(IntEnum):
    VERSION = 0x00
    ENCRYPTION = 0x01
    INSTOPT = 0x02
    THREADID = 0x03
    MARS = 0x04
    TRACEID = 0x05
    FEDAUTHREQUIRED = 0x06
    NONCEOPT = 0x07


class Encryption(IntEnum):
    """The ENCRYPTION option's single byte.

    OFF is the only value seen in the reference capture, from both sides. It
    does not mean no encryption: the login packet is still carried through a
    TLS handshake, after which the connection reverts to cleartext. The other
    three are protocol-defined and unexercised here.
    """

    OFF = 0x00
    ON = 0x01
    NOT_SUPPORTED = 0x02
    REQUIRED = 0x03


@dataclass(frozen=True)
class Version:
    """The VERSION option's six bytes."""

    major: int
    minor: int
    build: int
    subbuild: int = 0

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.build}"

    def pack(self) -> bytes:
        return _VERSION.pack(self.major, self.minor, self.build, self.subbuild)

    @classmethod
    def unpack(cls, data: bytes) -> Version:
        if len(data) != _VERSION.size:
            raise TdsProtocolError(
                f"VERSION option needs {_VERSION.size} bytes, got {len(data)}"
            )
        return cls(*_VERSION.unpack(data))


# What this server reports as itself. Taken from the reference capture so a
# client sees the same version it would see from SQL Server 2025.
SQL_SERVER_2025 = Version(major=17, minor=0, build=1000, subbuild=0)


@dataclass
class Prelogin:
    """One PRELOGIN message, entries in wire order."""

    options: list[tuple[PreloginOption, bytes]]

    def get(self, option: PreloginOption) -> bytes | None:
        for token, data in self.options:
            if token == option:
                return data
        return None

    @property
    def version(self) -> Version | None:
        data = self.get(PreloginOption.VERSION)
        return Version.unpack(data) if data else None

    @property
    def encryption(self) -> Encryption | None:
        data = self.get(PreloginOption.ENCRYPTION)
        return Encryption(data[0]) if data else None

    @property
    def mars(self) -> bool | None:
        data = self.get(PreloginOption.MARS)
        return bool(data[0]) if data else None

    @property
    def instance(self) -> str | None:
        """The requested instance name, without its terminating null."""
        data = self.get(PreloginOption.INSTOPT)
        if data is None:
            return None
        return data.rstrip(b"\x00").decode("ascii")

    @classmethod
    def parse(cls, payload: bytes) -> Prelogin:
        options: list[tuple[PreloginOption, bytes]] = []
        cursor = 0

        while True:
            if cursor >= len(payload):
                raise TdsProtocolError("PRELOGIN option table has no terminator")
            if payload[cursor] == TERMINATOR:
                break
            if cursor + _ENTRY_SIZE > len(payload):
                raise TdsProtocolError(
                    f"PRELOGIN option entry at {cursor} runs past the payload"
                )

            raw_token, offset, length = _ENTRY.unpack_from(payload, cursor)
            if offset + length > len(payload):
                raise TdsProtocolError(
                    f"PRELOGIN option 0x{raw_token:02x} points to bytes "
                    f"{offset}..{offset + length}, past the {len(payload)}-byte payload"
                )

            try:
                token = PreloginOption(raw_token)
            except ValueError:
                raise TdsProtocolError(
                    f"unknown PRELOGIN option 0x{raw_token:02x}"
                ) from None

            options.append((token, payload[offset:offset + length]))
            cursor += _ENTRY_SIZE

        return cls(options=options)

    def build(self) -> bytes:
        """Serialise back to a payload.

        Offsets are computed from the entry count, so a parsed message rebuilds
        to the same bytes as long as the original packed its data contiguously
        in table order. The reference server does.
        """
        table_size = len(self.options) * _ENTRY_SIZE + 1  # + terminator

        table = bytearray()
        blob = bytearray()
        for token, data in self.options:
            table += _ENTRY.pack(token, table_size + len(blob), len(data))
            blob += data
        table.append(TERMINATOR)

        return bytes(table + blob)


# What the server answers for each option it supports, in wire order. VERSION
# and ENCRYPTION are always sent: a client cannot proceed without either.
ALWAYS_ANSWERED = (PreloginOption.VERSION, PreloginOption.ENCRYPTION)


def server_response(
    *,
    version: Version = SQL_SERVER_2025,
    encryption: Encryption = Encryption.OFF,
    mars: bool = False,
    asked: Prelogin | None = None,
) -> Prelogin:
    """Build the PRELOGIN response, mirroring what the client asked about.

    A real server answers the options it was sent and no others. Measured
    against SQL Server 2025 through a proxy: a modern driver is answered with
    six options, and the legacy "SQL Server" ODBC driver, which asks about
    four, is answered with four. Sending it the extra two makes it read the
    response as a version older than SQL Server 6.5 and hang up.

    With nothing to mirror, all six go out, which is the shape the reference
    capture recorded and what every modern driver asks for.
    """
    answers = [
        (PreloginOption.VERSION, version.pack()),
        (PreloginOption.ENCRYPTION, bytes([encryption])),
        (PreloginOption.INSTOPT, b"\x00"),
        (PreloginOption.THREADID, b""),
        (PreloginOption.MARS, bytes([1 if mars else 0])),
        (PreloginOption.TRACEID, b""),
    ]
    if asked is None:
        return Prelogin(options=answers)

    wanted = {token for token, _ in asked.options}
    return Prelogin(options=[
        (token, value) for token, value in answers
        if token in wanted or token in ALWAYS_ANSWERED
    ])
