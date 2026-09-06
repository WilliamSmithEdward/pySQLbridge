"""Shared test doubles.

TlsClient is a real OpenSSL client rather than a mock. A mock would only prove
the tunnel agrees with the mock, and the thing worth proving is that it agrees
with an actual TLS implementation.
"""

import ssl


class TlsClient:
    """A TLS client over memory buffers, used to drive the server side."""

    def __init__(self) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        self.incoming = ssl.MemoryBIO()
        self.outgoing = ssl.MemoryBIO()
        self._ssl = context.wrap_bio(self.incoming, self.outgoing, server_side=False)
        self.handshake_complete = False

    def advance_handshake(self, data: bytes) -> bytes:
        if data:
            self.incoming.write(data)
        try:
            self._ssl.do_handshake()
        except ssl.SSLWantReadError:
            pass
        else:
            self.handshake_complete = True
        return self.outgoing.read()

    def send(self, plaintext: bytes) -> bytes:
        self._ssl.write(plaintext)
        return self.outgoing.read()

    def receive(self, data: bytes) -> bytes:
        self.incoming.write(data)
        return self._ssl.read()


def login7_with_sspi(base: bytes, blob: bytes) -> bytes:
    """Rebuild a captured LOGIN7 around a different SSPI blob.

    An SSPI exchange is one-shot: the blob in the captured login belongs to a
    finished conversation, so a test that wants to run the exchange through has
    to supply a fresh token from a live client. The blob sits last in the
    variable data, so replacing it means fixing the two fields that describe it
    and the three offsets that point past it.
    """
    import struct

    # Field positions, from the layout in tds/login.py: nine offset/length
    # pairs after the 36-byte header, then the 6-byte ClientID.
    ib_sspi_at = 36 + 9 * 4 + 6      # 78
    ib_sspi = struct.unpack_from("<H", base, ib_sspi_at)[0]

    rebuilt = bytearray(base[:ib_sspi] + blob)
    end = len(rebuilt)

    struct.pack_into("<HH", rebuilt, ib_sspi_at, ib_sspi, len(blob))
    struct.pack_into("<H", rebuilt, ib_sspi_at + 4, end)   # AtchDBFile
    struct.pack_into("<H", rebuilt, ib_sspi_at + 8, end)   # ChangePassword
    struct.pack_into("<I", rebuilt, 0, end)                # Length
    return bytes(rebuilt)
