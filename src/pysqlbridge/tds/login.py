"""LOGIN7, the packet a client sends once the tunnel is up.

Layout, decoded from a real 428-byte login sent by .Net SqlClient on
2026-09-06 rather than transcribed from the specification. Everything is
little-endian here, which is worth stating because the packet header wrapping
it is big-endian.

    0   Length          4   whole structure, matched the buffer exactly
    4   TDSVersion      4   0x74000004, the same value the server's LOGINACK carries
    8   PacketSize      4   8000 in the observed login
    12  ClientProgVer   4
    16  ClientPID       4
    20  ConnectionID    4
    24  OptionFlags1    1
    25  OptionFlags2    1
    26  TypeFlags       1
    27  OptionFlags3    1
    28  ClientTimeZone  4   signed
    32  ClientLCID      4
    36  offset/length table
    94  variable data

The table holds nine (offset, character-count) pairs, a 6-byte ClientID, then
(offset, byte-count) pairs for SSPI, AtchDBFile and ChangePassword, then
cbSSPILong. That comes to 58 bytes, so the variable data starts at 94, which is
exactly where the observed login put its first string.

String lengths count UTF-16 characters, so the byte length is twice the field.
The SSPI length counts bytes, because it is not a string.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .packet import TdsProtocolError

FIXED_HEADER_SIZE = 36
VARIABLE_DATA_START = 94

# What the password field is scrambled with. This is obfuscation and not
# encryption: the constant is published, so anyone who can read the bytes can
# read the password. What actually protects it is the tunnel, which covers
# LOGIN7 even on a connection that agreed to encrypt nothing else.
#
# The two directions are not the same order, which is easy to get backwards.
# [MS-TDS] 2.2.6.4: the client swaps the four high bits with the four low bits
# and then XORs with 0xA5; the server XORs with 0xA5 and then swaps.
PASSWORD_MASK = 0xA5

# The bit of OptionFlags2 that says the client is using Windows authentication.
# Least significant bit order, after the three bits of fUserType, so it is the
# top bit of the byte.
INTEGRATED_SECURITY = 0x80

# cbSSPI is 16 bits. When a blob will not fit, the client writes this sentinel
# and puts the real length in the 32-bit cbSSPILong instead.
SSPI_LENGTH_ESCAPE = 0xFFFF

_FIXED = struct.Struct("<6I4BiI")
_PAIR = struct.Struct("<HH")

_STRING_FIELDS = (
    "host_name",
    "user_name",
    "password",
    "app_name",
    "server_name",
    "extension",
    "client_interface_name",
    "language",
    "database",
)


def _swap_nibbles(byte: int) -> int:
    return ((byte & 0x0F) << 4) | ((byte & 0xF0) >> 4)


def deobfuscate_password(raw: bytes) -> str:
    """The password a client put in LOGIN7, unscrambled.

    XOR then swap, which is the reverse of the order the client applied. Doing
    it in the client's order instead returns plausible-looking rubbish rather
    than failing, so the direction is asserted by a test against a byte worked
    out by hand.

    Raises TdsProtocolError rather than letting a decode error escape, because
    a UnicodeDecodeError carries the offending bytes and those bytes are a
    password.
    """
    plain = bytes(_swap_nibbles(byte ^ PASSWORD_MASK) for byte in raw)
    if len(plain) % 2:
        raise TdsProtocolError(
            f"the LOGIN7 password is {len(plain)} bytes, which is not a whole "
            f"number of UTF-16 characters"
        )
    try:
        return plain.decode("utf-16-le")
    except UnicodeDecodeError:
        raise TdsProtocolError(
            "the LOGIN7 password is not valid UTF-16"
        ) from None


def obfuscate_password(password: str) -> bytes:
    """The inverse, for building a login. Only a client ever needs this.

    Here so that the two directions sit together and a test can show they are
    inverses; nothing in the server calls it.
    """
    return bytes(
        _swap_nibbles(byte) ^ PASSWORD_MASK
        for byte in password.encode("utf-16-le")
    )


@dataclass
class Login7:
    """A parsed LOGIN7.

    password holds the raw obfuscated bytes and is kept out of the repr. It is
    empty under Windows authentication, which is the case this project handles,
    but a SQL authentication login would carry a real credential here and it
    should not reach a log or a traceback by accident.
    """

    tds_version: int
    packet_size: int
    client_prog_ver: int
    client_pid: int
    connection_id: int
    option_flags1: int
    option_flags2: int
    type_flags: int
    option_flags3: int
    client_timezone: int
    client_lcid: int
    client_id: bytes

    host_name: str = ""
    user_name: str = ""
    app_name: str = ""
    server_name: str = ""
    extension: str = ""
    client_interface_name: str = ""
    language: str = ""
    database: str = ""

    sspi: bytes = b""
    atch_db_file: str = ""
    change_password: str = ""

    password: bytes = field(default=b"", repr=False)

    @property
    def uses_integrated_auth(self) -> bool:
        """Whether the client is authenticating through SSPI rather than a password."""
        return bool(self.sspi)

    @classmethod
    def parse(cls, payload: bytes) -> Login7:
        if len(payload) < VARIABLE_DATA_START:
            raise TdsProtocolError(
                f"LOGIN7 needs at least {VARIABLE_DATA_START} bytes, got {len(payload)}"
            )

        (
            length, tds_version, packet_size, client_prog_ver, client_pid,
            connection_id, flags1, flags2, type_flags, flags3,
            timezone, lcid,
        ) = _FIXED.unpack_from(payload, 0)

        if length != len(payload):
            raise TdsProtocolError(
                f"LOGIN7 says it is {length} bytes but the message is {len(payload)}"
            )

        def read_string(cursor: int) -> tuple[str, int]:
            offset, char_count = _PAIR.unpack_from(payload, cursor)
            blob = _slice(payload, offset, char_count * 2, "a LOGIN7 string")
            try:
                return blob.decode("utf-16-le"), cursor + _PAIR.size
            except UnicodeDecodeError as exc:
                raise TdsProtocolError(f"LOGIN7 string is not UTF-16: {exc}") from exc

        cursor = FIXED_HEADER_SIZE
        strings: dict[str, str] = {}
        raw_password = b""
        for name in _STRING_FIELDS:
            if name == "password":
                # Read without decoding. The bytes are obfuscated, so decoding
                # them as text produces noise, and they are not ours to expand.
                offset, char_count = _PAIR.unpack_from(payload, cursor)
                raw_password = _slice(
                    payload, offset, char_count * 2, "the LOGIN7 password"
                )
                cursor += _PAIR.size
                continue
            strings[name], cursor = read_string(cursor)

        client_id = bytes(payload[cursor:cursor + 6])
        cursor += 6

        sspi_offset, sspi_length = _PAIR.unpack_from(payload, cursor)
        cursor += _PAIR.size
        atch_db_file, cursor = read_string(cursor)
        change_password, cursor = read_string(cursor)
        (sspi_long,) = struct.unpack_from("<I", payload, cursor)

        if sspi_length == SSPI_LENGTH_ESCAPE:
            sspi_length = sspi_long
        sspi = _slice(payload, sspi_offset, sspi_length, "the LOGIN7 SSPI blob")

        return cls(
            tds_version=tds_version,
            packet_size=packet_size,
            client_prog_ver=client_prog_ver,
            client_pid=client_pid,
            connection_id=connection_id,
            option_flags1=flags1,
            option_flags2=flags2,
            type_flags=type_flags,
            option_flags3=flags3,
            client_timezone=timezone,
            client_lcid=lcid,
            client_id=client_id,
            sspi=sspi,
            atch_db_file=atch_db_file,
            change_password=change_password,
            password=raw_password,
            **strings,
        )


def _slice(payload: bytes, offset: int, length: int, what: str) -> bytes:
    """Read a range the table points at, refusing to run off the end.

    Offsets come from the client, so they are checked rather than trusted.
    """
    if length == 0:
        return b""
    end = offset + length
    if offset > len(payload) or end > len(payload):
        raise TdsProtocolError(
            f"LOGIN7 points {what} at bytes {offset}..{end}, "
            f"past the {len(payload)}-byte message"
        )
    return bytes(payload[offset:end])
