import socket
import struct

import pytest

from pysqlbridge import certificate
from pysqlbridge.auth import AuthenticationError
from pysqlbridge.catalog import Catalog, TRANSACTION
from pysqlbridge.logins import Login, LoginStore
from pysqlbridge.server import BridgeServer
from pysqlbridge.tds import (
    HEADER_SIZE,
    Column,
    Connection,
    ConnectionState,
    Encryption,
    EnvChangeType,
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
from pysqlbridge.tds.packet import TDS_71, PacketStatus
from pysqlbridge.tds.token import DoneStatus, done, error

from .captured import CLIENT_LOGIN7, CLIENT_PRELOGIN, SERVER_PRELOGIN
from .helpers import TlsClient, login7_with_password, login7_with_sspi

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

    def through_tls(self, prelogin: bytes = CLIENT_PRELOGIN) -> None:
        self.feed(prelogin)
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
        """Send LOGIN7 through the tunnel.

        On a session that agreed OFF this is the only encrypted message and
        everything after it is cleartext. On one that agreed ON, everything
        is encrypted, and then send() and read() are how a client speaks.
        """
        return self.feed(self.tls.send(build_packet(PacketType.LOGIN7, payload)))

    def send(self, packet: bytes) -> list[bytes]:
        """One packet as an encrypted client sends it."""
        return self.feed(self.tls.send(packet))

    def read(self, responses: list) -> bytes:
        """What an encrypted client makes of what came back.

        Anything that is not a TLS record it cannot read at all, which is the
        failure this exists to catch: a client that is sent a bare TDS packet
        on an encrypted session waits for a record that never arrives.
        """
        return self.tls.receive(b"".join(responses))


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
        pytest.importorskip("sspi", reason="needs pywin32 on Windows")
        # A login asking for integrated authentication builds the
        # Windows acceptor, whatever the test goes on to check.
        session = Session(open_connection())
        session.through_tls()
        session.send_login(CLIENT_LOGIN7)

        login = session.connection.login
        assert login.host_name == "WORKSTATION1"
        assert login.database == "master"
        assert login.uses_integrated_auth is True

    def test_challenges_the_client(self):
        pytest.importorskip("sspi", reason="needs pywin32 on Windows")
        # A login asking for integrated authentication builds the
        # Windows acceptor, whatever the test goes on to check.
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
        pytest.importorskip("sspi", reason="needs pywin32 on Windows")
        # A login asking for integrated authentication builds the
        # Windows acceptor, whatever the test goes on to check.
        session = Session(open_connection())
        session.through_tls()
        session.send_login(CLIENT_LOGIN7)
        assert session.connection.state is ConnectionState.EXPECT_SSPI

    def test_the_challenge_is_sent_in_the_clear(self):
        pytest.importorskip("sspi", reason="needs pywin32 on Windows")
        # A login asking for integrated authentication builds the
        # Windows acceptor, whatever the test goes on to check.
        # Encryption covered the login and stops there. A response the client
        # had to decrypt would not be read.
        session = Session(open_connection())
        session.through_tls()
        response = session.send_login(CLIENT_LOGIN7)[0]
        assert reassemble(response).type is PacketType.TABULAR_RESULT

    @pytest.mark.parametrize("chunk_size", [1, 13, 512])
    def test_survives_arbitrary_read_boundaries(self, chunk_size):
        pytest.importorskip("sspi", reason="needs pywin32 on Windows")
        # A login asking for integrated authentication builds the
        # Windows acceptor, whatever the test goes on to check.
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
        # A login with an empty SSPI field is asking for SQL authentication,
        # and a bridge told about no logins admits nobody. It says so the way
        # a real server does rather than dropping the connection, which a
        # client reports as a server that is down instead of a login refused.
        session = Session(open_connection())
        session.through_tls()
        responses = session.send_login(login7_with_sspi(CLIENT_LOGIN7, b""))
        payload = reassemble(b"".join(responses)).payload
        assert payload[0] == TokenType.ERROR
        assert struct.unpack_from("<I", payload, 3)[0] == 18456
        assert session.connection.state is ConnectionState.FAILED


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
        pytest.importorskip("sspi", reason="needs pywin32 on Windows")
        # A login asking for integrated authentication builds the
        # Windows acceptor, whatever the test goes on to check.
        session = Session(open_connection())
        session.through_tls()
        session.send_login(CLIENT_LOGIN7)
        with pytest.raises(TdsProtocolError, match="expected an SSPI message"):
            session.feed(build_packet(PacketType.SQL_BATCH, b"SELECT 1"))


class TestASqlLogin:
    """A username and a password, rather than a Windows token.

    These need no SSPI, so unlike the Windows tests they run anywhere. The
    refusal is measured: SQL Server 2025 answered a login it did not know
    with number 18456 at severity 14, and answers an unknown name and a wrong
    password with the same one.
    """

    LOGIN_FAILED = 18456

    @staticmethod
    def store(password: str = "hunter2") -> LoginStore:
        return LoginStore([Login(user="reader", secret=password, hashed=False)])

    def attempt(self, user: str, password: str, logins=None) -> tuple:
        session = Session(open_connection(logins=logins))
        session.through_tls()
        responses = session.send_login(
            login7_with_password(CLIENT_LOGIN7, user, password)
        )
        return session, reassemble(b"".join(responses)).payload

    def test_a_configured_login_is_admitted(self):
        session, payload = self.attempt("reader", "hunter2", self.store())
        assert session.connection.state is ConnectionState.READY
        assert TokenType.LOGIN_ACK in payload

    def test_the_name_is_matched_without_regard_to_case(self):
        session, _ = self.attempt("READER", "hunter2", self.store())
        assert session.connection.state is ConnectionState.READY

    def test_a_wrong_password_is_refused(self):
        session, payload = self.attempt("reader", "wrong", self.store())
        assert payload[0] == TokenType.ERROR
        assert struct.unpack_from("<I", payload, 3)[0] == self.LOGIN_FAILED
        assert session.connection.state is ConnectionState.FAILED

    def test_an_unknown_user_is_refused_the_same_way(self):
        # The same number and the same sentence, so that a refusal does not
        # say whether the name exists and is worth guessing a password for.
        # The name inside it differs because it is the one the client just
        # sent, which the client already knows.
        _, unknown = self.attempt("nobody", "hunter2", self.store())
        _, wrong = self.attempt("reader", "wrong", self.store())
        assert struct.unpack_from("<I", unknown, 3)[0] == self.LOGIN_FAILED
        assert struct.unpack_from("<I", wrong, 3)[0] == self.LOGIN_FAILED
        assert "Login failed for user 'nobody'.".encode("utf-16-le") in unknown
        assert "Login failed for user 'reader'.".encode("utf-16-le") in wrong

    def test_a_refusal_does_not_carry_the_password(self):
        _, payload = self.attempt("reader", "swordfish", self.store())
        assert "swordfish".encode("utf-16-le") not in payload
        assert b"swordfish" not in payload

    def test_nothing_is_admitted_when_no_login_is_configured(self):
        session, payload = self.attempt("reader", "hunter2")
        assert struct.unpack_from("<I", payload, 3)[0] == self.LOGIN_FAILED
        assert session.connection.state is ConnectionState.FAILED

    def test_a_hashed_password_is_admitted(self):
        from pysqlbridge.logins import hash_password

        store = LoginStore([
            Login(user="reader", secret=hash_password("hunter2"), hashed=True)
        ])
        session, _ = self.attempt("reader", "hunter2", store)
        assert session.connection.state is ConnectionState.READY

    def test_windows_authentication_still_works_beside_it(self):
        # Configuring SQL logins adds a way in; it does not close the other.
        pytest.importorskip("sspi", reason="needs pywin32 on Windows")
        session = TestQueries.logged_in(logins=self.store())
        assert session.connection.state is ConnectionState.READY


class TestAPooledConnectionBeingReused:
    """The reset a client asks for on a connection out of its pool.

    Measured on SQL Server 2025 by closing a pooled connection and opening
    another with the pool held to one, so the same physical connection
    comes back: the temp table is gone, an open transaction is rolled
    back, and SET XACT_ABORT is off again. This server read the bit and
    did nothing with it, so one user's scratch tables, transaction and
    settings were still sitting there for whoever had the connection
    next, which is the wrong answer and the wrong person's data.

    Driven directly here rather than through a pool, because the pool is
    the client's side of it; the batch tests below send the bit on a
    packet the way a pooled client does.
    """

    def test_it_throws_away_what_the_last_user_left(self):
        connection = open_connection()
        connection._session["#scratch"] = "a table"
        connection._session["@@__rowcount"] = 7
        connection._reset_the_session(keep_transaction=False)
        assert connection._session == {}

    def test_what_identifies_the_connection_comes_back_by_itself(self):
        # Clearing everything is safe because _about() writes these back
        # before each query, which is why the reset needs no list of what
        # to spare.
        connection = open_connection()
        connection._session["login"] = "someone"
        connection._reset_the_session(keep_transaction=False)
        assert connection._about()["login"] == connection.username

    def test_the_transaction_it_keeps_is_the_one_the_catalog_writes(self):
        # The protocol layer writes that key out rather than importing the
        # catalog, which would be the circular import the other way round.
        # This holds the two to the same string: rename either and it
        # fails here, where the reset would otherwise keep nothing and say
        # nothing about it.
        connection = open_connection()
        connection._session[TRANSACTION] = "still open"
        connection._session["#scratch"] = "a table"
        connection._reset_the_session(keep_transaction=True)
        assert connection._session == {TRANSACTION: "still open"}

    def test_a_reset_that_keeps_nothing_forgets_the_descriptor(self):
        connection = open_connection()
        connection._xact_descriptor = b"\x01\x02\x03\x04\x05\x06\x07\x08"
        connection._reset_the_session(keep_transaction=False)
        assert connection._xact_descriptor is None


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

    def test_a_reused_connection_starts_clean(self):
        # The bit a pooled client sets on the first message it sends after
        # taking the connection back out of the pool.
        session = self.logged_in(
            query_handler=lambda request: QueryResult(columns=[], rows=[])
        )
        session.connection._session["#scratch"] = "a table"
        headers = struct.pack("<I", 22) + b"\x00" * 18
        session.feed(build_packet(
            PacketType.SQL_BATCH,
            headers + "SELECT 1".encode("utf-16-le"),
            status=PacketStatus.END_OF_MESSAGE | PacketStatus.RESET_CONNECTION,
        ))
        assert "#scratch" not in session.connection._session

    def test_a_batch_without_the_bit_keeps_the_session(self):
        # The other half. A temp table has to survive the batch after the
        # one that made it, or no session could use one at all.
        session = self.logged_in(
            query_handler=lambda request: QueryResult(columns=[], rows=[])
        )
        session.connection._session["#scratch"] = "a table"
        self.send_query(session, "SELECT 1")
        assert session.connection._session["#scratch"] == "a table"

    def test_the_same_query_twice_is_two_queries(self):
        # What a listener counts to know it was reached. Watching the text
        # for a change said nothing the second time, and pressing F5 in SSMS
        # re-sends the same text: the server answered and looked idle.
        session = self.logged_in(
            query_handler=lambda request: QueryResult(columns=[], rows=[])
        )
        before = session.connection.asked
        for _ in range(3):
            self.send_query(session, "SELECT id FROM people ORDER BY id")
        assert session.connection.asked == before + 3
        assert session.connection.last_query == "SELECT id FROM people ORDER BY id"

    def test_and_a_different_query_counts_the_same_way(self):
        session = self.logged_in(
            query_handler=lambda request: QueryResult(columns=[], rows=[])
        )
        before = session.connection.asked
        self.send_query(session, "SELECT 1")
        self.send_query(session, "SELECT 2")
        assert session.connection.asked == before + 2

    def test_a_cancellation_is_acknowledged_rather_than_fatal(self):
        # A client that cancels waits to be told the cancellation happened,
        # and will not use the connection again until it is. This used to be
        # a protocol error that ended the connection.
        session = self.logged_in(
            query_handler=lambda request: QueryResult(columns=[], rows=[])
        )
        responses = session.feed(build_packet(PacketType.ATTENTION, b""))
        payload = reassemble(b"".join(responses)).payload
        assert payload[0] == TokenType.DONE
        assert int.from_bytes(payload[1:3], "little") & 0x0020
        assert session.connection.state is ConnectionState.READY

    def test_and_a_query_after_it_still_answers(self):
        session = self.logged_in(
            query_handler=lambda request: QueryResult(
                columns=[Column("n", Integer(4))], rows=[[7]]
            )
        )
        session.feed(build_packet(PacketType.ATTENTION, b""))
        payload = reassemble(b"".join(self.send_query(session, "SELECT 7"))).payload
        assert payload[0] == TokenType.COL_METADATA

    def test_the_handler_receives_the_query_text(self):
        seen = []

        def handler(request):
            seen.append(request.sql)
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

    def test_a_batch_that_failed_twice_sends_both_errors_before_one_done(self):
        # Measured: a quote left open is 105 and then 102, two ERROR tokens
        # and a single DONE, which is how a client gets both messages and
        # still raises the first number.
        def handler(sql):
            raise QueryError(
                "Unclosed quotation mark after the character string 'open'.",
                number=105, severity=15,
                following=(QueryError("Incorrect syntax near 'open'.",
                                      number=102, severity=15),),
            )

        session = self.logged_in(query_handler=handler)
        payload = reassemble(b"".join(self.send_query(session, "SELECT 'open"))).payload
        server = socket.gethostname()
        assert payload == (
            error(105, "Unclosed quotation mark after the character string "
                       "'open'.", severity=15, server=server)
            + error(102, "Incorrect syntax near 'open'.", severity=15,
                    server=server)
            + done(status=DoneStatus.ERROR)
        )
        assert session.connection.state is ConnectionState.READY

    @staticmethod
    def logged_in_asking_for_71(**kwargs) -> Session:
        # The legacy "SQL Server" ODBC driver asks for 7.1, which moves the
        # line number in ERROR and the row count in DONE to their narrower
        # widths. The version sits at offset 4 of a LOGIN7.
        sspi = pytest.importorskip("sspi", reason="needs pywin32 on Windows")
        client_auth = sspi.ClientAuth("Negotiate")
        _, buffers = client_auth.authorize(None)
        session = Session(open_connection(**kwargs))
        session.through_tls()
        login = bytearray(login7_with_sspi(CLIENT_LOGIN7, bytes(buffers[0].Buffer)))
        login[4:8] = TDS_71.to_bytes(4, "little")
        responses = session.send_login(bytes(login))
        for _ in range(6):
            if session.connection.state is ConnectionState.READY:
                return session
            _, buffers = client_auth.authorize(unwrap_sspi_token(responses[-1]))
            responses = session.feed(
                build_packet(PacketType.SSPI, bytes(buffers[0].Buffer)))
        raise AssertionError(f"never reached READY ({session.connection.state.name})")

    @pytest.mark.parametrize("failure", [
        QueryError("invalid object name 'nope'", number=208),
        ValueError("a fault in answering"),
    ])
    def test_an_old_client_reads_a_failed_query_at_its_own_widths(self, failure):
        # Found by reading the error path against the version it was sent
        # at: it wrote the 7.4 shape to everyone, so a 7.1 client read the
        # two spare bytes of the line number and the four of the row count
        # as the start of the next token.
        def handler(sql):
            raise failure

        session = self.logged_in_asking_for_71(query_handler=handler)
        # A 7.1 batch is the text alone, with no header block in front.
        payload = reassemble(b"".join(session.feed(build_packet(
            PacketType.SQL_BATCH, "SELECT * FROM nope".encode("utf-16-le")
        )))).payload
        number = failure.number if isinstance(failure, QueryError) else 50000
        severity = failure.severity if isinstance(failure, QueryError) else 16
        message = (str(failure) if isinstance(failure, QueryError) else
                   f"pysqlbridge could not answer that: ValueError: {failure}")
        assert payload == (
            error(number, message, severity=severity,
                  server=socket.gethostname(), tds_version=TDS_71)
            + done(status=DoneStatus.ERROR, tds_version=TDS_71)
        )
        assert session.connection.state is ConnectionState.READY

    def test_a_bug_in_the_handler_is_an_error_and_keeps_the_connection(self):
        # Not a query this cannot answer but a fault in answering it, which
        # used to reach the read loop and close the socket. A client that
        # loses its session over one query reports every query after it as a
        # connection failure, and the real fault is nowhere in that report.
        def handler(sql):
            raise ValueError("invalid literal for int() with base 10: 'ada'")

        session = self.logged_in(query_handler=handler)
        payload = reassemble(b"".join(self.send_query(session, "SELECT 1"))).payload

        assert payload[0] == TokenType.ERROR
        assert "ValueError".encode("utf-16-le") in payload
        assert session.connection.state is ConnectionState.READY
        # And the next query is answered normally.
        assert self.send_query(session, "SELECT 2")
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


class TestATransactionThroughTheApi:
    """A client's BeginTransaction/Commit/Rollback, which arrive as packet 0x0E.

    Reuses TestQueries' login helpers, so these run on Windows only for the
    same reason: reaching READY needs a real SSPI exchange. Before this, the
    transaction-manager packet dropped the connection, which is what made a
    .NET client's BeginTransaction() fail. The count is answered by a real
    catalog so a client reads back what its calls set.
    """

    @staticmethod
    def send_transaction(session: Session, request_type: int,
                         rest: bytes = b"") -> list[bytes]:
        # A 22-byte ALL_HEADERS block, as the reference client sends, then the
        # request type and its body.
        headers = struct.pack("<I", 22) + b"\x00" * 18
        payload = headers + struct.pack("<H", request_type) + rest
        return session.feed(
            build_packet(PacketType.TRANSACTION_MANAGER, payload)
        )

    @staticmethod
    def _first_int(payload: bytes) -> int:
        # The one integer value of a single-column, single-row answer: find the
        # ROW token, whose INTN value is a length byte then that many LE bytes.
        at = payload.index(TokenType.ROW)
        width = payload[at + 1]
        return int.from_bytes(payload[at + 2:at + 2 + width], "little")

    def logged_in_with_catalog(self) -> Session:
        return TestQueries.logged_in(query_handler=Catalog().answer)

    def count(self, session: Session) -> int:
        payload = reassemble(
            b"".join(TestQueries.send_query(session, "SELECT @@TRANCOUNT AS n"))
        ).payload
        return self._first_int(payload)

    def test_begin_is_answered_rather_than_dropping_the_connection(self):
        session = self.logged_in_with_catalog()
        payload = reassemble(
            b"".join(self.send_transaction(session, 5, b"\x00\x00"))
        ).payload
        assert payload[0] == TokenType.ENV_CHANGE
        assert payload[3] == EnvChangeType.BEGIN_TRAN
        assert session.connection.state is ConnectionState.READY

    def test_the_client_reads_the_count_its_calls_set(self):
        session = self.logged_in_with_catalog()
        assert self.count(session) == 0
        self.send_transaction(session, 5, b"\x00\x00")     # begin
        assert self.count(session) == 1
        self.send_transaction(session, 7, b"\x00\x00")     # commit
        assert self.count(session) == 0

    def test_commit_announces_the_transaction_ended(self):
        session = self.logged_in_with_catalog()
        self.send_transaction(session, 5, b"\x00\x00")
        payload = reassemble(
            b"".join(self.send_transaction(session, 7, b"\x00\x00"))
        ).payload
        assert payload[0] == TokenType.ENV_CHANGE
        assert payload[3] == EnvChangeType.COMMIT_TRAN

    def test_a_commit_with_nothing_open_is_the_servers_error(self):
        session = self.logged_in_with_catalog()
        payload = reassemble(
            b"".join(self.send_transaction(session, 7, b"\x00\x00"))
        ).payload
        assert payload[0] == TokenType.ERROR
        assert struct.unpack_from("<I", payload, 3)[0] == 3902
        assert session.connection.state is ConnectionState.READY

    def test_a_distributed_request_is_refused_but_keeps_the_connection(self):
        session = self.logged_in_with_catalog()
        payload = reassemble(
            b"".join(self.send_transaction(session, 0, b"\x00\x00"))
        ).payload
        assert payload[0] == TokenType.ERROR
        assert "distributed".encode("utf-16-le") in payload
        assert session.connection.state is ConnectionState.READY


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

    @staticmethod
    def prelogin_wanting_mars() -> bytes:
        """A client asking for multiple active result sets, not declining."""
        from pysqlbridge.tds import PreloginOption, Version, build_packet
        from pysqlbridge.tds.prelogin import Prelogin

        request = Prelogin(options=[
            (PreloginOption.VERSION, Version(18, 7, 5).pack()),
            (PreloginOption.ENCRYPTION, bytes([Encryption.OFF])),
            (PreloginOption.INSTOPT, b"\x00"),
            (PreloginOption.THREADID, b"\x00\x00\x00\x00"),
            (PreloginOption.MARS, b"\x01"),
        ])
        return build_packet(PacketType.PRELOGIN, request.build())

    def test_mars_is_declined_even_when_it_is_asked_for(self):
        # Measured against both servers: a client that asks for multiple
        # active result sets and is told no connects and runs, and its own
        # driver refuses it a second open reader before anything reaches
        # the server. That is what a real server with MARS off gives it,
        # so declining is a complete answer rather than a gap.
        #
        # This guards the answer rather than the feature. Saying yes
        # without carrying the session multiplexing that goes with it
        # would be worse than saying no: the client would wrap every
        # packet in a header this server does not read, and the
        # connection would break instead of quietly doing without.
        connection = open_connection()
        response = connection.receive(self.prelogin_wanting_mars())[0]
        assert Prelogin.parse(reassemble(response).payload).mars is False

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


class TestTheDatabaseItAnnounces:
    """What a client is told it reached, which everything else must match.

    A client told it was in master, and then handed a list of databases with
    no master in it, showed no databases at all. One database is served
    whatever a login names, so the name announced is that one.
    """

    def test_the_login_names_the_database_served(self):
        assert open_connection(database="pysqlbridge")._database == "pysqlbridge"

    def test_the_default_is_what_a_client_expects_of_a_server(self):
        assert open_connection()._database == "master"


class TestOnePortOneServer:
    """A second bridge on a port already held is refused.

    On Windows SO_REUSEADDR does not mean what it means on Unix: it lets a
    second process bind a port another is already listening on, and the two
    then split the clients between them. That is silent, and it looks like a
    server losing its mind rather than like two servers, because a connection
    that made a temp table lands next on the other process and the table it
    just made is not there.
    """

    def test_the_second_one_does_not_bind(self):
        first = BridgeServer("127.0.0.1", 0)
        try:
            with pytest.raises(OSError):
                BridgeServer("127.0.0.1", first.address[1]).server_close()
        finally:
            first.server_close()

    def test_and_the_port_is_free_again_afterwards(self):
        first = BridgeServer("127.0.0.1", 0)
        port = first.address[1]
        first.server_close()
        second = BridgeServer("127.0.0.1", port)
        second.server_close()


class TestTheNameItAnswersTo:
    """What a client is told to call this server, which it may connect to.

    A real server answers the machine name and that is enough, because it
    listens on every address the name resolves to. This one listens on
    whatever it was told to, and a client handed the machine name goes to
    the addresses that name resolves to, finds nothing on any of them, and
    spends its whole connect timeout finding that out. Fifteen seconds of
    silence, and then it carries on as though nothing happened.
    """

    def test_the_name_is_the_address_it_listens_on(self):
        server = BridgeServer("127.0.0.1", 0)
        try:
            assert server.reached_at == f"127.0.0.1,{server.address[1]}"
        finally:
            server.server_close()

    def test_bound_to_every_address_the_machine_name_is_right_again(self):
        server = BridgeServer("0.0.0.0", 0)
        try:
            assert server.reached_at == socket.gethostname()
        finally:
            server.server_close()

    def test_a_connection_is_told_where_it_reached(self):
        connection = open_connection(reached_at="127.0.0.1,1337")
        assert connection._about()["server"] == "127.0.0.1,1337"

    def test_and_nothing_is_claimed_when_nothing_says(self):
        assert open_connection()._about()["server"] is None


class TestAFullyEncryptedSession:
    """Every answer wrapped, which is what a client that asked for ON reads.

    SSMS connects with encryption mandatory, so the session stays encrypted
    after the login rather than reverting to cleartext. A packet written
    straight to the socket then is not a TLS record: the client cannot read
    it, keeps waiting for one it can, and sits there holding the connection
    open. That is what a cancellation acknowledgement did, and it is what
    made Object Explorer spin after connecting.
    """

    @staticmethod
    def logged_in() -> Session:
        sspi = pytest.importorskip("sspi", reason="needs pywin32 on Windows")

        session = Session(open_connection(
            encryption=Encryption.ON,
            query_handler=lambda request: QueryResult(
                columns=[Column("n", Integer(4))], rows=[[7]]
            ),
        ))
        session.through_tls(
            TestEncryptionNegotiation.prelogin_asking(Encryption.ON))
        assert session.connection.session_encrypted is True

        client_auth = sspi.ClientAuth("Negotiate")
        _, buffers = client_auth.authorize(None)
        responses = session.send_login(
            login7_with_sspi(CLIENT_LOGIN7, bytes(buffers[0].Buffer))
        )
        for _ in range(6):
            # Read every record, always. TLS numbers them implicitly, so a
            # client that skips one cannot decrypt the next: leaving the
            # login response unread breaks the stream rather than ignoring it.
            plain = session.read(responses)
            if session.connection.state is ConnectionState.READY:
                return session
            _, buffers = client_auth.authorize(unwrap_sspi_token(plain))
            responses = session.send(
                build_packet(PacketType.SSPI, bytes(buffers[0].Buffer))
            )
        raise AssertionError(
            f"never reached READY ({session.connection.state.name})")

    @staticmethod
    def batch(sql: str) -> bytes:
        headers = struct.pack("<I", 22) + bytes(18)
        return build_packet(PacketType.SQL_BATCH,
                            headers + sql.encode("utf-16-le"))

    def test_a_query_comes_back_through_the_tunnel(self):
        session = self.logged_in()
        responses = session.send(self.batch("SELECT 7"))
        payload = reassemble(session.read(responses)).payload
        assert payload[0] == TokenType.COL_METADATA

    def test_and_so_does_a_cancellation(self):
        # The one that did not. It went out as a bare TDS packet, which a
        # client on an encrypted session cannot read at all.
        session = self.logged_in()
        responses = session.send(build_packet(PacketType.ATTENTION, b""))
        payload = reassemble(session.read(responses)).payload
        assert payload[0] == TokenType.DONE
        assert int.from_bytes(payload[1:3], "little") & 0x0020

    def test_nothing_goes_out_that_is_not_a_record(self):
        # Whatever the answer, a client must be able to read it. Bytes that
        # are not TLS are bytes it will wait on for as long as it is willing.
        session = self.logged_in()
        for packet in (build_packet(PacketType.ATTENTION, b""),
                       self.batch("SELECT 7")):
            responses = session.send(packet)
            assert responses, "the server answered nothing at all"
            for one in responses:
                assert one[0] in (0x14, 0x15, 0x16, 0x17), (
                    f"not a TLS record: {one[:8].hex(' ')}"
                )
