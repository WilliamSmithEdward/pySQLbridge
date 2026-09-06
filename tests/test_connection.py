import struct

import pytest

from pysqlbridge import certificate
from pysqlbridge.auth import AuthenticationError
from pysqlbridge.tds import (
    HEADER_SIZE,
    Column,
    Connection,
    ConnectionState,
    Encryption,
    Integer,
    NVarChar,
    PacketType,
    Prelogin,
    QueryError,
    QueryResult,
    TdsProtocolError,
    TokenType,
    build_packet,
    reassemble,
    result_set,
    wrap_handshake,
)

from .captured import CLIENT_LOGIN7, CLIENT_PRELOGIN, SERVER_PRELOGIN
from .helpers import TlsClient, login7_with_sspi

CERTIFICATE = certificate.self_signed("pysqlbridge.test")


def open_connection(**kwargs) -> Connection:
    return Connection(CERTIFICATE, **kwargs)


def unwrap_sspi_token(packet: bytes) -> bytes:
    """Pull the blob out of a server SSPI token packet."""
    payload = reassemble(packet).payload
    assert payload[0] == TokenType.SSPI, f"expected an SSPI token, got 0x{payload[0]:02x}"
    length = struct.unpack_from("<H", payload, 1)[0]
    assert length == len(payload) - 3, "token length disagrees with the payload"
    return payload[3:3 + length]


class Session:
    """Drives a connection through the login, splitting writes like a socket."""

    def __init__(self, connection: Connection, chunk_size: int | None = None) -> None:
        self.connection = connection
        self.chunk_size = chunk_size
        self.tls = TlsClient()

    def feed(self, data: bytes) -> list[bytes]:
        if self.chunk_size is None:
            return self.connection.receive(data)
        out: list[bytes] = []
        for i in range(0, len(data), self.chunk_size):
            out.extend(self.connection.receive(data[i:i + self.chunk_size]))
        return out

    def through_tls(self) -> None:
        self.feed(CLIENT_PRELOGIN)
        to_server = self.tls.advance_handshake(b"")
        for _ in range(12):
            responses = self.feed(b"".join(wrap_handshake(to_server))) if to_server else []
            # The client's final flight must reach the server before either
            # side can call the handshake done.
            if self.tls.handshake_complete and (
                self.connection.state is not ConnectionState.TLS_HANDSHAKE
            ):
                return
            to_client = b"".join(r[HEADER_SIZE:] for r in responses)
            to_server = self.tls.advance_handshake(to_client) if to_client else b""
        raise AssertionError(
            f"handshake did not settle (server={self.connection.state.name})"
        )

    def send_login(self, payload: bytes) -> list[bytes]:
        """Send LOGIN7 through the tunnel, which is the only encrypted message."""
        return self.feed(self.tls.send(build_packet(PacketType.LOGIN7, payload)))


class TestPrelogin:
    def test_starts_expecting_prelogin(self):
        assert open_connection().state is ConnectionState.EXPECT_PRELOGIN

    def test_answers_with_the_reference_response(self):
        assert open_connection().receive(CLIENT_PRELOGIN) == [SERVER_PRELOGIN]

    def test_records_what_the_client_said(self):
        connection = open_connection()
        connection.receive(CLIENT_PRELOGIN)
        assert str(connection.client_prelogin.version) == "18.7.5"
        assert connection.client_prelogin.encryption is Encryption.OFF

    def test_waits_for_a_whole_message(self):
        connection = open_connection()
        assert connection.receive(CLIENT_PRELOGIN[:20]) == []
        assert connection.receive(CLIENT_PRELOGIN[20:]) == [SERVER_PRELOGIN]

    def test_advertised_version_is_configurable(self):
        from pysqlbridge.tds import Version

        connection = Connection(CERTIFICATE, version=Version(16, 0, 4200))
        payload = reassemble(connection.receive(CLIENT_PRELOGIN)[0]).payload
        assert str(Prelogin.parse(payload).version) == "16.0.4200"

    def test_rejects_a_non_prelogin_opener(self):
        connection = open_connection()
        with pytest.raises(TdsProtocolError, match="expected PRELOGIN"):
            connection.receive(build_packet(PacketType.SQL_BATCH, b"SELECT 1"))
        assert connection.state is ConnectionState.FAILED

    def test_refuses_to_continue_after_failing(self):
        connection = open_connection()
        with pytest.raises(TdsProtocolError):
            connection.receive(build_packet(PacketType.SQL_BATCH, b""))
        with pytest.raises(TdsProtocolError, match="already failed"):
            connection.receive(CLIENT_PRELOGIN)


class TestLoginThroughTheTunnel:
    def test_parses_the_captured_login(self):
        session = Session(open_connection())
        session.through_tls()
        session.send_login(CLIENT_LOGIN7)

        login = session.connection.login
        assert login.host_name == "WORKSTATION1"
        assert login.database == "master"
        assert login.uses_integrated_auth is True

    def test_challenges_the_client(self):
        session = Session(open_connection())
        session.through_tls()
        responses = session.send_login(CLIENT_LOGIN7)

        assert len(responses) == 1
        blob = unwrap_sspi_token(responses[0])
        # Windows answers a SPNEGO negotiate with a SPNEGO response carrying
        # the NTLM challenge inside it.
        assert blob[0] == 0xA1
        assert b"NTLMSSP\x00" in blob
        index = blob.find(b"NTLMSSP\x00")
        assert struct.unpack_from("<I", blob, index + 8)[0] == 2  # CHALLENGE

    def test_waits_for_the_clients_answer(self):
        session = Session(open_connection())
        session.through_tls()
        session.send_login(CLIENT_LOGIN7)
        assert session.connection.state is ConnectionState.EXPECT_SSPI

    def test_the_challenge_is_sent_in_the_clear(self):
        # Encryption covered the login and stops there. A response the client
        # had to decrypt would not be read.
        session = Session(open_connection())
        session.through_tls()
        response = session.send_login(CLIENT_LOGIN7)[0]
        assert reassemble(response).type is PacketType.TABULAR_RESULT

    @pytest.mark.parametrize("chunk_size", [1, 13, 512])
    def test_survives_arbitrary_read_boundaries(self, chunk_size):
        # A socket splits wherever it likes, including across the point where
        # TDS framing stops and bare TLS records begin.
        session = Session(open_connection(), chunk_size=chunk_size)
        session.through_tls()
        session.send_login(CLIENT_LOGIN7)
        assert session.connection.state is ConnectionState.EXPECT_SSPI

    def test_rejects_a_non_login_message_through_the_tunnel(self):
        session = Session(open_connection())
        session.through_tls()
        with pytest.raises(TdsProtocolError, match="expected LOGIN7"):
            session.feed(
                session.tls.send(build_packet(PacketType.SQL_BATCH, b"SELECT 1"))
            )

    def test_refuses_a_login_with_no_sspi_token(self):
        # A login with an empty SSPI field is asking for SQL authentication.
        session = Session(open_connection())
        session.through_tls()
        with pytest.raises(AuthenticationError, match="not implemented"):
            session.send_login(login7_with_sspi(CLIENT_LOGIN7, b""))


class TestWindowsAuthentication:
    """Runs a whole exchange against the real Windows acceptor.

    The client half is Windows SSPI too, so both ends are the real thing and a
    pass means Windows actually authenticated the account, not that two mocks
    agreed with each other.
    """

    def test_authenticates_a_real_client(self):
        sspi = pytest.importorskip("sspi", reason="needs pywin32 on Windows")

        client_auth = sspi.ClientAuth("Negotiate")
        status, buffers = client_auth.authorize(None)
        negotiate = bytes(buffers[0].Buffer)

        session = Session(open_connection())
        session.through_tls()
        responses = session.send_login(login7_with_sspi(CLIENT_LOGIN7, negotiate))

        # Answer each challenge until Windows is satisfied, in the clear.
        for _ in range(6):
            if session.connection.state is ConnectionState.READY:
                break
            assert responses, "server stopped talking before authenticating"
            challenge = unwrap_sspi_token(responses[-1])
            status, buffers = client_auth.authorize(challenge)
            reply = bytes(buffers[0].Buffer)
            responses = session.feed(build_packet(PacketType.SSPI, reply))
        else:
            raise AssertionError(
                f"never authenticated (state={session.connection.state.name})"
            )

        assert session.connection.state is ConnectionState.READY

    def test_reports_who_authenticated(self):
        pytest.importorskip("sspi", reason="needs pywin32 on Windows")
        import getpass

        import sspi

        client_auth = sspi.ClientAuth("Negotiate")
        _, buffers = client_auth.authorize(None)

        session = Session(open_connection())
        session.through_tls()
        responses = session.send_login(
            login7_with_sspi(CLIENT_LOGIN7, bytes(buffers[0].Buffer))
        )
        for _ in range(6):
            if session.connection.state is ConnectionState.READY:
                break
            _, buffers = client_auth.authorize(unwrap_sspi_token(responses[-1]))
            responses = session.feed(
                build_packet(PacketType.SSPI, bytes(buffers[0].Buffer))
            )

        username = session.connection.username
        assert username, "authenticated but reported no name"
        # Comes back as DOMAIN\user or MACHINE\user.
        assert username.split("\\")[-1].lower() == getpass.getuser().lower()

    def test_rejects_a_corrupted_token(self):
        pytest.importorskip("sspi", reason="needs pywin32 on Windows")

        session = Session(open_connection())
        session.through_tls()
        with pytest.raises(AuthenticationError):
            session.send_login(login7_with_sspi(CLIENT_LOGIN7, b"\x60\x7f" + b"\x00" * 60))

    def test_rejects_a_non_sspi_reply_to_the_challenge(self):
        session = Session(open_connection())
        session.through_tls()
        session.send_login(CLIENT_LOGIN7)
        with pytest.raises(TdsProtocolError, match="expected an SSPI message"):
            session.feed(build_packet(PacketType.SQL_BATCH, b"SELECT 1"))


class TestQueries:
    """Batches answered after a completed login.

    Reaching READY needs a real SSPI exchange, so these run on Windows only,
    for the same reason the authentication tests do.
    """

    @staticmethod
    def logged_in(**kwargs) -> Session:
        sspi = pytest.importorskip("sspi", reason="needs pywin32 on Windows")

        client_auth = sspi.ClientAuth("Negotiate")
        _, buffers = client_auth.authorize(None)

        session = Session(open_connection(**kwargs))
        session.through_tls()
        responses = session.send_login(
            login7_with_sspi(CLIENT_LOGIN7, bytes(buffers[0].Buffer))
        )
        for _ in range(6):
            if session.connection.state is ConnectionState.READY:
                return session
            _, buffers = client_auth.authorize(unwrap_sspi_token(responses[-1]))
            responses = session.feed(
                build_packet(PacketType.SSPI, bytes(buffers[0].Buffer))
            )
        raise AssertionError(f"never reached READY ({session.connection.state.name})")

    @staticmethod
    def send_query(session: Session, sql: str) -> list[bytes]:
        headers = struct.pack("<I", 22) + b"\x00" * 18
        return session.feed(
            build_packet(PacketType.SQL_BATCH, headers + sql.encode("utf-16-le"))
        )

    def test_the_handler_receives_the_query_text(self):
        seen = []

        def handler(sql):
            seen.append(sql)
            return QueryResult(columns=[Column("n", Integer(4))], rows=[[1]])

        session = self.logged_in(query_handler=handler)
        self.send_query(session, "SELECT 1 FROM t")
        assert seen == ["SELECT 1 FROM t"]
        assert session.connection.last_query == "SELECT 1 FROM t"

    def test_answers_with_a_result_set(self):
        columns = [Column("n", Integer(4)), Column("label", NVarChar(10))]
        rows = [[1, "one"], [2, None]]
        session = self.logged_in(
            query_handler=lambda sql: QueryResult(columns=columns, rows=rows)
        )
        payload = reassemble(b"".join(self.send_query(session, "SELECT 1"))).payload
        assert payload == result_set(columns, rows)

    def test_stays_ready_for_the_next_query(self):
        session = self.logged_in(
            query_handler=lambda sql: QueryResult(
                columns=[Column("n", Integer(4))], rows=[[1]]
            )
        )
        self.send_query(session, "SELECT 1")
        assert session.connection.state is ConnectionState.READY
        assert self.send_query(session, "SELECT 2")
        assert session.connection.state is ConnectionState.READY

    def test_a_failing_query_returns_an_error_and_keeps_the_connection(self):
        def handler(sql):
            raise QueryError("no such table: people", number=208)

        session = self.logged_in(query_handler=handler)
        payload = reassemble(b"".join(self.send_query(session, "SELECT * FROM people"))).payload

        assert payload[0] == TokenType.ERROR
        assert struct.unpack_from("<I", payload, 3)[0] == 208
        assert "no such table: people".encode("utf-16-le") in payload
        # An error is an answer, not a broken connection.
        assert session.connection.state is ConnectionState.READY

    def test_the_default_handler_refuses_rather_than_returning_nothing(self):
        # An empty result set would have the client report success and show no
        # rows, with nothing saying why.
        session = self.logged_in()
        payload = reassemble(b"".join(self.send_query(session, "SELECT 1"))).payload
        assert payload[0] == TokenType.ERROR
        assert "no data source".encode("utf-16-le") in payload

    def test_a_large_result_is_split_across_packets(self):
        columns = [Column("n", Integer(4)), Column("pad", NVarChar(200))]
        rows = [[i, "x" * 200] for i in range(40)]
        session = self.logged_in(
            query_handler=lambda sql: QueryResult(columns=columns, rows=rows)
        )
        packets = self.send_query(session, "SELECT 1")
        assert len(packets) > 1, "expected the answer to outgrow one packet"
        assert reassemble(b"".join(packets)).payload == result_set(columns, rows)

    def test_rejects_a_non_batch_packet_when_ready(self):
        session = self.logged_in()
        with pytest.raises(TdsProtocolError, match="expected a SQL batch"):
            session.feed(build_packet(PacketType.PRELOGIN, b"\xff"))


class TestEncryptionNegotiation:
    """What PRELOGIN agrees decides whether the session stays encrypted.

    Answering OFF to a client that asked for ON does not fail loudly. The
    client completes its handshake, authenticates, then waits for encrypted
    bytes that never arrive and times out in its post-login phase, which is
    what SSMS does under its default Encrypt=Mandatory.
    """

    @staticmethod
    def prelogin_asking(encryption: Encryption) -> bytes:
        from pysqlbridge.tds import PreloginOption, Version, build_packet
        from pysqlbridge.tds.prelogin import Prelogin

        request = Prelogin(options=[
            (PreloginOption.VERSION, Version(18, 7, 5).pack()),
            (PreloginOption.ENCRYPTION, bytes([encryption])),
            (PreloginOption.INSTOPT, b"\x00"),
            (PreloginOption.THREADID, b"\x00\x00\x00\x00"),
            (PreloginOption.MARS, b"\x00"),
        ])
        return build_packet(PacketType.PRELOGIN, request.build())

    def agreed(self, connection: Connection, asked: Encryption) -> Encryption:
        response = connection.receive(self.prelogin_asking(asked))[0]
        return Prelogin.parse(reassemble(response).payload).encryption

    def test_off_is_answered_with_off(self):
        connection = open_connection()
        assert self.agreed(connection, Encryption.OFF) is Encryption.OFF
        assert connection.session_encrypted is False

    def test_on_is_answered_with_on(self):
        connection = open_connection()
        assert self.agreed(connection, Encryption.ON) is Encryption.ON
        assert connection.session_encrypted is True

    def test_a_client_requiring_encryption_gets_it(self):
        connection = open_connection()
        assert self.agreed(connection, Encryption.REQUIRED) is Encryption.ON
        assert connection.session_encrypted is True

    def test_a_server_requiring_encryption_overrides_an_off_request(self):
        connection = open_connection(encryption=Encryption.REQUIRED)
        assert self.agreed(connection, Encryption.OFF) is Encryption.ON
        assert connection.session_encrypted is True

    def test_the_captured_client_asked_for_off(self):
        # The reference capture negotiated login-only encryption, which is why
        # everything after the login dissects as plaintext.
        connection = open_connection()
        connection.receive(CLIENT_PRELOGIN)
        assert connection.session_encrypted is False
