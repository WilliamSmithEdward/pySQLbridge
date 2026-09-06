"""The TLS tunnel that carries the login packet.

TDS puts TLS in an unusual position. The handshake records travel as the
payload of TDS packets typed PRELOGIN, so they are framed; the application
records that follow do not, so they are bare on the wire. Measured against SQL
Server 2025 on 2026-09-06:

    frame 8   12 01 009b ...  16 03 03 ...   handshake record inside TDS
    frame 16  17 03 03 0187 ...              application record, no TDS header
    frame 18  04 01 00fa ...  ed ...         plaintext TDS, tunnel abandoned

With encryption negotiated off, which is the default both sides chose, the
tunnel exists only to carry LOGIN7. Everything after it is cleartext. A server
that keeps encrypting past the login will lose the client.

This module deals in byte strings and knows nothing about what it is
encrypting. wrap_handshake is the one exception, because where the TDS frame
stops is a fact about this tunnel rather than about TDS generally.
"""

from __future__ import annotations

import ssl
from enum import Enum, auto

from .packet import DEFAULT_PACKET_SIZE, PacketType, build_message

# The reference server sends its handshake packets with packet_id 0, where
# PRELOGIN itself used 1. Clients are not known to inspect the field, but this
# project's whole approach is to send what the real server sends.
HANDSHAKE_PACKET_ID = 0


class TunnelState(Enum):
    HANDSHAKING = auto()
    ESTABLISHED = auto()


class TlsError(Exception):
    """The TLS layer failed in a way the connection cannot continue past."""


class TlsTunnel:
    """A server-side TLS engine driven by buffers instead of a socket.

    The caller owns the transport: it feeds received bytes in and sends
    whatever comes back out. That is what lets the same engine read framed
    handshake records and unframed application records.
    """

    def __init__(self, context: ssl.SSLContext) -> None:
        self._incoming = ssl.MemoryBIO()
        self._outgoing = ssl.MemoryBIO()
        self._ssl = context.wrap_bio(
            self._incoming, self._outgoing, server_side=True
        )
        self._state = TunnelState.HANDSHAKING

    @property
    def state(self) -> TunnelState:
        return self._state

    @property
    def handshake_complete(self) -> bool:
        return self._state is TunnelState.ESTABLISHED

    def advance_handshake(self, data: bytes) -> bytes:
        """Feed received handshake bytes, return the bytes to send back.

        Call until handshake_complete. The return value is the payload to put
        inside TDS packets, and it can be empty when the engine needs more
        input before it can say anything.
        """
        if self._state is TunnelState.ESTABLISHED:
            raise TlsError("handshake already complete")

        if data:
            self._incoming.write(data)

        try:
            self._ssl.do_handshake()
        except ssl.SSLWantReadError:
            pass  # Normal: more input needed before the next flight.
        except ssl.SSLError as exc:
            raise TlsError(f"TLS handshake failed: {exc}") from exc
        else:
            self._state = TunnelState.ESTABLISHED

        return self._outgoing.read()

    def unwrap(self, data: bytes) -> bytes:
        """Decrypt application records into plaintext.

        Returns every whole plaintext byte currently available, which may be
        empty if the records received so far do not complete one.
        """
        self._require_established("unwrap")
        if data:
            self._incoming.write(data)

        plaintext = bytearray()
        while True:
            try:
                chunk = self._ssl.read()
            except ssl.SSLWantReadError:
                break
            except ssl.SSLError as exc:
                raise TlsError(f"TLS decryption failed: {exc}") from exc
            if not chunk:
                break
            plaintext += chunk
        return bytes(plaintext)

    def wrap(self, plaintext: bytes) -> bytes:
        """Encrypt plaintext into application records ready for the wire."""
        self._require_established("wrap")
        if plaintext:
            try:
                self._ssl.write(plaintext)
            except ssl.SSLError as exc:
                raise TlsError(f"TLS encryption failed: {exc}") from exc
        return self._outgoing.read()

    def _require_established(self, operation: str) -> None:
        if self._state is not TunnelState.ESTABLISHED:
            raise TlsError(
                f"cannot {operation} before the handshake completes; "
                f"tunnel is {self._state.name.lower()}"
            )


def wrap_handshake(
    records: bytes, *, packet_size: int = DEFAULT_PACKET_SIZE
) -> list[bytes]:
    """Frame handshake records as TDS packets.

    Only the handshake is framed this way. Once the tunnel is established its
    records go on the wire unwrapped, so there is deliberately no matching
    helper for them.
    """
    return build_message(
        PacketType.PRELOGIN,
        records,
        packet_size=packet_size,
        packet_id_start=HANDSHAKE_PACKET_ID,
    )
