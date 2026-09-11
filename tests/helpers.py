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


def login7_with_password(base: bytes, user: str, password: str,
                         host_name: str = "testhost",
                         app_name: str = "pysqlbridge tests") -> bytes:
    """A LOGIN7 that asks for SQL authentication, built around a real header.

    The captured login carries an SSPI blob and no password, which is the
    other kind entirely, so the variable data is rebuilt rather than patched:
    every string is written fresh and every offset recomputed. The fixed
    header is kept, minus the bit that says the client is using Windows
    authentication, so the flags and version stay the ones a real client sent.

    Layout from tds/login.py: a 36-byte header, nine (offset, character-count)
    pairs, a 6-byte ClientID, three more pairs, cbSSPILong, then the data at 94.
    """
    import struct

    from pysqlbridge.tds.login import (
        FIXED_HEADER_SIZE,
        INTEGRATED_SECURITY,
        VARIABLE_DATA_START,
        obfuscate_password,
    )

    header = bytearray(base[:FIXED_HEADER_SIZE])
    header[25] &= ~INTEGRATED_SECURITY & 0xFF    # OptionFlags2, fIntSecurity

    # In the order the parser reads them. The password is the one field whose
    # bytes are not plain UTF-16, and its count is still in characters.
    strings = [
        host_name, user, password, app_name,
        "", "", "", "", "",   # server, extension, interface, language, database
    ]

    table = bytearray()
    data = bytearray()
    for position, value in enumerate(strings):
        encoded = (obfuscate_password(value) if position == 2
                   else value.encode("utf-16-le"))
        # ibHostName must point at the start of the data even when empty.
        table += struct.pack("<HH", VARIABLE_DATA_START + len(data),
                             len(encoded) // 2)
        data += encoded

    table += base[FIXED_HEADER_SIZE + 36:FIXED_HEADER_SIZE + 42]   # ClientID
    end = VARIABLE_DATA_START + len(data)
    table += struct.pack("<HH", end, 0)     # SSPI: none, this is not Windows auth
    table += struct.pack("<HH", end, 0)     # AtchDBFile
    table += struct.pack("<HH", end, 0)     # ChangePassword
    table += struct.pack("<I", 0)           # cbSSPILong

    built = bytearray(header + table + data)
    struct.pack_into("<I", built, 0, len(built))
    return bytes(built)


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
