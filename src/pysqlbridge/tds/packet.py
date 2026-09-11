"""TDS packet framing.

Every TDS message travels as one or more packets, each carrying an 8-byte
header. This module handles only that outer frame: reading a header, splitting
an oversized payload across packets, and reassembling a message from them. What
the payload means belongs to the modules above.

Header layout, all multi-byte fields big-endian:

    offset 0  type      1 byte
    offset 1  status    1 byte, bit flags
    offset 2  length    2 bytes, whole packet including this header
    offset 4  spid      2 bytes
    offset 6  packet_id 1 byte
    offset 7  window    1 byte

Measured 2026-09-06 against SQL Server 2025 (17.0.1000.7). See
docs/tds-login-handshake.md for the capture these values come from.
"""

from __future__ import annotations

import itertools
import struct
import threading
from dataclasses import dataclass
from enum import IntEnum, IntFlag
from typing import Iterator, NamedTuple

HEADER_SIZE = 8

# The length field is 16 bits, so a packet cannot exceed this no matter what
# packet size the connection negotiates.
MAX_PACKET_SIZE = 0xFFFF

# What a connection uses before ENVCHANGE negotiates something else.
DEFAULT_PACKET_SIZE = 4096

_HEADER = struct.Struct(">BBHHBB")


class PacketType(IntEnum):
    """First byte of the header.

    Values observed in the reference capture are marked. The rest are defined
    by the protocol but this project has not yet seen them on the wire.
    """

    SQL_BATCH = 0x01        # observed: client query
    RPC = 0x03
    TABULAR_RESULT = 0x04   # observed: every server-to-client message
    ATTENTION = 0x06
    BULK_LOAD = 0x07
    TRANSACTION_MANAGER = 0x0E
    LOGIN7 = 0x10
    SSPI = 0x11             # observed: client NTLM authenticate
    PRELOGIN = 0x12         # observed: prelogin, and the TLS handshake records


class PacketStatus(IntFlag):
    """Second byte of the header."""

    NORMAL = 0x00
    END_OF_MESSAGE = 0x01   # observed on every packet in the capture
    IGNORE = 0x02
    RESET_CONNECTION = 0x08
    RESET_CONNECTION_SKIP_TRAN = 0x10


@dataclass(frozen=True)
class PacketHeader:
    type: PacketType
    status: PacketStatus
    length: int
    spid: int = 0
    packet_id: int = 1
    window: int = 0

    @property
    def payload_length(self) -> int:
        return self.length - HEADER_SIZE

    @property
    def is_end_of_message(self) -> bool:
        return bool(self.status & PacketStatus.END_OF_MESSAGE)


class TdsProtocolError(Exception):
    """A packet could not be parsed as TDS."""


def parse_header(data: bytes) -> PacketHeader:
    """Read an 8-byte header from the front of data.

    Raises TdsProtocolError if there are too few bytes, if the type is not one
    this project recognises, or if the length field is shorter than the header
    it sits in.
    """
    if len(data) < HEADER_SIZE:
        raise TdsProtocolError(
            f"need {HEADER_SIZE} bytes for a TDS header, got {len(data)}"
        )

    raw_type, raw_status, length, spid, packet_id, window = _HEADER.unpack_from(data)

    try:
        packet_type = PacketType(raw_type)
    except ValueError:
        raise TdsProtocolError(f"unknown TDS packet type 0x{raw_type:02x}") from None

    if length < HEADER_SIZE:
        raise TdsProtocolError(
            f"TDS length field {length} is smaller than the {HEADER_SIZE}-byte header"
        )

    return PacketHeader(
        type=packet_type,
        status=PacketStatus(raw_status),
        length=length,
        spid=spid,
        packet_id=packet_id,
        window=window,
    )


# TDS versions, as LOGIN7 and LOGINACK spell them. Kept here rather than with
# the tokens because the request parsers need them and sit below that module.
TDS_70 = 0x70000000
TDS_71 = 0x71000001
TDS_72 = 0x72090002
TDS_73A = 0x730A0003
TDS_73B = 0x730B0003
TDS_74 = 0x74000004

# The version that put an ALL_HEADERS block in front of a request. Before it,
# a batch is the query text and an RPC starts at the procedure name.
ALL_HEADERS_ADDED_IN = TDS_72

# The same version widened two response fields: the row count in DONE, from
# four bytes to eight, and the line number in INFO and ERROR, two to four.
ROW_COUNT_WIDENED_IN = TDS_72

# And the user type in COLMETADATA, from two bytes to four.
USER_TYPE_WIDENED_IN = TDS_72


# SQL Server numbers user sessions from 51; everything below that is
# reserved for its own background work. The capture showed 66.
FIRST_SESSION_ID = 51

# A session id is two bytes on the wire, so it wraps rather than growing.
_MAX_SESSION_ID = 0xFFFF

_sessions = itertools.count(FIRST_SESSION_ID)
_sessions_lock = threading.Lock()


def next_session_id() -> int:
    """The id for one more session, unique among those in flight."""
    with _sessions_lock:
        given = next(_sessions)
    if given <= _MAX_SESSION_ID:
        return given
    return FIRST_SESSION_ID + (given - FIRST_SESSION_ID) % (
        _MAX_SESSION_ID - FIRST_SESSION_ID + 1
    )


def build_packet(
    packet_type: PacketType,
    payload: bytes,
    *,
    status: PacketStatus = PacketStatus.END_OF_MESSAGE,
    spid: int = 0,
    packet_id: int = 1,
    window: int = 0,
) -> bytes:
    """Frame a single payload as one TDS packet.

    The payload must fit in one packet. Use build_message for anything that
    might not.
    """
    length = HEADER_SIZE + len(payload)
    if length > MAX_PACKET_SIZE:
        raise ValueError(
            f"payload of {len(payload)} bytes exceeds the maximum packet size; "
            "use build_message to split it"
        )
    header = _HEADER.pack(packet_type, status, length, spid, packet_id, window)
    return header + payload


def build_message(
    packet_type: PacketType,
    payload: bytes,
    *,
    packet_size: int = DEFAULT_PACKET_SIZE,
    spid: int = 0,
    packet_id_start: int = 1,
) -> list[bytes]:
    """Split a payload across as many packets as it needs.

    Only the last packet carries END_OF_MESSAGE. packet_id counts up from
    packet_id_start and wraps at 256, which is what the protocol specifies
    rather than an overflow being ignored. The start is a parameter because
    the reference server numbers its PRELOGIN from 1 but its TLS handshake
    packets from 0.

    An empty payload still produces one packet. A message has to be terminated
    for the peer to act on it, and a zero-packet message would never be.
    """
    if packet_size <= HEADER_SIZE:
        raise ValueError(f"packet_size must exceed the {HEADER_SIZE}-byte header")
    if packet_size > MAX_PACKET_SIZE:
        raise ValueError(f"packet_size cannot exceed {MAX_PACKET_SIZE}")

    body = packet_size - HEADER_SIZE
    chunks = [payload[i:i + body] for i in range(0, len(payload), body)] or [b""]

    packets = []
    for index, chunk in enumerate(chunks):
        last = index == len(chunks) - 1
        packets.append(
            build_packet(
                packet_type,
                chunk,
                status=PacketStatus.END_OF_MESSAGE if last else PacketStatus.NORMAL,
                spid=spid,
                packet_id=(packet_id_start + index) % 256,
            )
        )
    return packets


def iter_packets(stream: bytes) -> Iterator[tuple[PacketHeader, bytes]]:
    """Walk every complete packet in a buffer.

    Stops at the first incomplete packet rather than raising, so this is safe
    to call on a partially filled socket buffer.
    """
    offset = 0
    while offset + HEADER_SIZE <= len(stream):
        header = parse_header(stream[offset:])
        end = offset + header.length
        if end > len(stream):
            return
        yield header, stream[offset + HEADER_SIZE:end]
        offset = end


class Message(NamedTuple):
    """One reassembled TDS message and how much of the buffer it used.

    consumed lets a caller reading from a stream drop exactly this message and
    keep whatever followed it in the same read.

    status is the first packet's, because that is where a request carries the
    bits about the message as a whole: a pooled client asks for the session to
    be reset there, and reassembly used to read the end-of-message bit and
    throw the rest away, so nothing above could see the request.
    """

    type: PacketType
    payload: bytes
    consumed: int
    status: PacketStatus = PacketStatus.NORMAL


def reassemble(stream: bytes) -> Message | None:
    """Join the packets of one message into its payload.

    Returns None when the buffer does not yet hold a packet whose status marks
    the end of the message, which is how a caller knows to read more.
    """
    payload = bytearray()
    message_type: PacketType | None = None
    consumed = 0
    opening = PacketStatus.NORMAL

    for header, chunk in iter_packets(stream):
        if message_type is None:
            message_type = header.type
            # The first packet's status, kept because that is the one that
            # speaks for the whole message.
            opening = header.status
        payload += chunk
        consumed += header.length
        if header.is_end_of_message:
            return Message(message_type, bytes(payload), consumed, opening)
    return None
