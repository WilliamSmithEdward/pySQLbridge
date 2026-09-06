import glob
import os
import ssl
import tempfile

import pytest

from pysqlbridge import certificate
from pysqlbridge.tds import (
    HANDSHAKE_PACKET_ID,
    PacketType,
    TlsError,
    TlsTunnel,
    TunnelState,
    parse_header,
    wrap_handshake,
)

from .helpers import TlsClient

# RSA key generation is slow enough to notice, and none of these tests mutate
# the certificate, so it is built once.
CERTIFICATE = certificate.self_signed("pysqlbridge.test")


def complete_handshake(max_flights: int = 12) -> tuple[TlsTunnel, TlsClient]:
    """Run a handshake to completion and return both ends."""
    tunnel = TlsTunnel(certificate.server_context(CERTIFICATE))
    client = TlsClient()

    to_server = client.advance_handshake(b"")
    for _ in range(max_flights):
        if tunnel.handshake_complete and client.handshake_complete:
            return tunnel, client
        to_client = tunnel.advance_handshake(to_server) if to_server else b""
        to_server = client.advance_handshake(to_client)

    raise AssertionError(
        f"handshake did not settle in {max_flights} flights "
        f"(tunnel={tunnel.state.name}, client_done={client.handshake_complete})"
    )


class TestCertificate:
    def test_common_name_is_the_requested_host(self):
        assert CERTIFICATE.common_name == "pysqlbridge.test"

    def test_defaults_to_this_machines_hostname(self):
        import socket

        assert certificate.self_signed().common_name == socket.gethostname()

    def test_is_valid_now(self):
        import datetime as dt

        assert CERTIFICATE.not_valid_after > dt.datetime.now(dt.timezone.utc)

    def test_pems_are_pem(self):
        assert CERTIFICATE.cert_pem.startswith(b"-----BEGIN CERTIFICATE-----")
        assert b"PRIVATE KEY" in CERTIFICATE.key_pem

    def test_loads_into_an_ssl_context(self):
        assert isinstance(certificate.server_context(CERTIFICATE), ssl.SSLContext)

    def test_does_not_leave_the_private_key_on_disk(self):
        pattern = os.path.join(tempfile.gettempdir(), "pysqlbridge-*")
        before = set(glob.glob(pattern))
        certificate.server_context(CERTIFICATE)
        assert set(glob.glob(pattern)) - before == set()

    def test_round_trips_through_files(self, tmp_path):
        cert_path = tmp_path / "server.crt"
        key_path = tmp_path / "server.key"
        cert_path.write_bytes(CERTIFICATE.cert_pem)
        key_path.write_bytes(CERTIFICATE.key_pem)
        assert certificate.load(cert_path, key_path) == CERTIFICATE


class TestHandshake:
    def test_starts_out_handshaking(self):
        tunnel = TlsTunnel(certificate.server_context(CERTIFICATE))
        assert tunnel.state is TunnelState.HANDSHAKING
        assert tunnel.handshake_complete is False

    def test_completes_against_a_real_client(self):
        tunnel, client = complete_handshake()
        assert tunnel.state is TunnelState.ESTABLISHED
        assert client.handshake_complete is True

    def test_rejects_advancing_once_complete(self):
        tunnel, _ = complete_handshake()
        with pytest.raises(TlsError, match="already complete"):
            tunnel.advance_handshake(b"")

    def test_garbage_fails_rather_than_hanging(self):
        tunnel = TlsTunnel(certificate.server_context(CERTIFICATE))
        with pytest.raises(TlsError, match="handshake failed"):
            tunnel.advance_handshake(b"this is not a TLS ClientHello" * 8)


class TestApplicationData:
    def test_decrypts_what_the_client_sent(self):
        tunnel, client = complete_handshake()
        # Stands in for LOGIN7: the tunnel exists to carry exactly this.
        assert tunnel.unwrap(client.send(b"LOGIN7 payload")) == b"LOGIN7 payload"

    def test_encrypts_so_the_client_can_read_it(self):
        tunnel, client = complete_handshake()
        assert client.receive(tunnel.wrap(b"server reply")) == b"server reply"

    def test_returns_empty_on_a_partial_record(self):
        tunnel, client = complete_handshake()
        record = client.send(b"split across two reads")
        assert tunnel.unwrap(record[: len(record) // 2]) == b""
        assert tunnel.unwrap(record[len(record) // 2:]) == b"split across two reads"

    def test_cannot_unwrap_before_the_handshake(self):
        tunnel = TlsTunnel(certificate.server_context(CERTIFICATE))
        with pytest.raises(TlsError, match="cannot unwrap"):
            tunnel.unwrap(b"anything")

    def test_cannot_wrap_before_the_handshake(self):
        tunnel = TlsTunnel(certificate.server_context(CERTIFICATE))
        with pytest.raises(TlsError, match="cannot wrap"):
            tunnel.wrap(b"anything")


class TestWrapHandshake:
    def test_frames_records_as_prelogin_packets(self):
        packets = wrap_handshake(b"\x16\x03\x03\x00\x04abcd")
        assert len(packets) == 1
        header = parse_header(packets[0])
        assert header.type is PacketType.PRELOGIN
        assert header.is_end_of_message

    def test_uses_the_packet_id_the_reference_server_uses(self):
        header = parse_header(wrap_handshake(b"\x16\x03\x03\x00\x01x")[0])
        assert header.packet_id == HANDSHAKE_PACKET_ID == 0

    def test_splits_a_large_flight(self):
        # A certificate chain can outgrow one packet at the default size,
        # which is still in force because packet size is negotiated later.
        packets = wrap_handshake(b"\x00" * 9000)
        assert len(packets) == 3
        assert [parse_header(p).packet_id for p in packets] == [0, 1, 2]
        assert [parse_header(p).is_end_of_message for p in packets] == [
            False, False, True,
        ]

    def test_a_real_server_flight_round_trips_through_framing(self):
        tunnel = TlsTunnel(certificate.server_context(CERTIFICATE))
        client = TlsClient()
        flight = tunnel.advance_handshake(client.advance_handshake(b""))

        packets = wrap_handshake(flight)
        rebuilt = b"".join(p[8:] for p in packets)
        assert rebuilt == flight
        # The server's first flight is a handshake record, not application data.
        assert flight[0] == 0x16
