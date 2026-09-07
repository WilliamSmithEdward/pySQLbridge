"""The login sequence as a state machine over byte buffers.

Feed it what arrived, send back what it returns. It owns no socket, which is
what makes the whole login path testable without one and keeps the transport
free to be threads, asyncio, or a test harness.

The states follow the measured handshake in docs/tds-login-handshake.md:

    EXPECT_PRELOGIN  a TDS message arrives, a PRELOGIN response goes back
    TLS_HANDSHAKE    TLS records arrive framed in TDS, replies go back framed
    EXPECT_LOGIN     TLS records arrive bare, LOGIN7 comes out of them
    EXPECT_SSPI      the client answers the challenge, in the clear
    READY            logged in; SQL batches are answered from here on

Two boundaries in there are easy to get wrong and both are measured. The shift
from TDS-framed TLS to bare TLS happens the moment the handshake completes, and
the same socket read can hold the end of one and the start of the other, so the
buffer is drained by exactly the bytes each message used rather than cleared.

What happens after the login depends on what PRELOGIN agreed. A client asking
for ENCRYPT_OFF gets the reference server's behaviour: the tunnel covers the
login packet and the session reverts to cleartext. A client asking for
ENCRYPT_ON, which is what SSMS sends under its default Encrypt=Mandatory, keeps
the tunnel for everything. Answering OFF to a client that asked for ON does not
fail loudly; the client simply waits for encrypted bytes that never come, and
times out in its post-login phase.
"""

from __future__ import annotations

import socket
from enum import Enum, auto
from typing import Callable

from ..auth import AuthenticationError, SspiAcceptor
from ..certificate import Certificate, server_context
from .batch import parse_sql_batch
from .login import Login7
from .packet import (
    next_session_id,
    DEFAULT_PACKET_SIZE,
    PacketType,
    TdsProtocolError,
    build_message,
    build_packet,
    reassemble,
)
from .prelogin import SQL_SERVER_2025, Encryption, Prelogin, Version, server_response
from .result import Query, QueryError, QueryResult
from .rpc import parse_rpc
from .token import TDS_74, error_response, login_response, negotiate, sspi_token
from .tls import TlsTunnel, wrap_handshake


# Where SQL Server's user-defined error numbers begin. A procedure this
# project has not implemented is its own complaint, not one of the server's.
UNSUPPORTED_PROCEDURE = 50000


class ConnectionState(Enum):
    EXPECT_PRELOGIN = auto()
    TLS_HANDSHAKE = auto()
    EXPECT_LOGIN = auto()
    EXPECT_SSPI = auto()
    READY = auto()
    FAILED = auto()


class Connection:
    """One client connection, driven by bytes in and bytes out."""

    def __init__(
        self,
        certificate: Certificate,
        *,
        version: Version = SQL_SERVER_2025,
        encryption: Encryption = Encryption.OFF,
        server_name: str | None = None,
        query_handler: Callable[[str], QueryResult] | None = None,
        acceptor_factory=SspiAcceptor,
    ) -> None:
        self._certificate = certificate
        self._version = version
        self._encryption = encryption
        # Clients display this in the messages the login response carries, so
        # it should be the host's real name rather than a placeholder.
        self._server_name = server_name or socket.gethostname()
        self._query_handler = query_handler or _no_queries
        self._acceptor_factory = acceptor_factory

        self._state = ConnectionState.EXPECT_PRELOGIN
        self._tunnel: TlsTunnel | None = None
        self._acceptor = None
        self._buffer = bytearray()      # bytes as they arrived
        self._plaintext = bytearray()   # bytes after the tunnel decrypted them
        self._client_prelogin: Prelogin | None = None
        self._login: Login7 | None = None
        self._last_query: str | None = None
        # What this connection alone can see: the temp tables it created.
        self._session: dict = {}
        self._tds_version = TDS_74
        # Zero until the login is answered, which is what a real server sends
        # through the handshake.
        self._spid = 0
        self._session_encrypted = False

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def client_prelogin(self) -> Prelogin | None:
        """What the client said about itself, once it has said it."""
        return self._client_prelogin

    @property
    def login(self) -> Login7 | None:
        """The parsed LOGIN7, once one has arrived."""
        return self._login

    @property
    def session_encrypted(self) -> bool:
        """Whether the tunnel covers the whole session rather than just the login."""
        return self._session_encrypted

    def _about(self) -> dict:
        """What this connection is, for the functions that answer about it.

        Filled in each time rather than at login because the login is what
        supplies it and a query can arrive before anything else has looked.
        """
        self._session["login"] = self.username
        self._session["app"] = self._login.app_name if self._login else None
        self._session["host"] = self._login.host_name if self._login else None
        return self._session

    @property
    def last_query(self) -> str | None:
        """The most recent batch this connection was asked to run."""
        return self._last_query

    @property
    def username(self) -> str | None:
        """Who authenticated, once Windows has said so."""
        return self._acceptor.username if self._acceptor is not None else None

    def receive(self, data: bytes) -> list[bytes]:
        """Take received bytes, return whatever should go back.

        Each returned item is one complete thing to write. Callers should not
        assume one response per call: a single read can drive several steps,
        and some steps produce nothing.
        """
        if self._state is ConnectionState.FAILED:
            raise TdsProtocolError("connection already failed")

        self._buffer += data
        responses: list[bytes] = []
        try:
            while self._step(responses):
                pass
        except Exception:
            self._state = ConnectionState.FAILED
            raise
        return responses

    def _step(self, responses: list[bytes]) -> bool:
        """Advance one step. Returns whether anything moved."""
        if self._state is ConnectionState.EXPECT_PRELOGIN:
            return self._step_prelogin(responses)
        if self._state is ConnectionState.TLS_HANDSHAKE:
            return self._step_handshake(responses)
        if self._state is ConnectionState.EXPECT_LOGIN:
            return self._step_login(responses)
        if self._state is ConnectionState.EXPECT_SSPI:
            return self._step_sspi(responses)
        if self._state is ConnectionState.READY:
            return self._step_query(responses)
        return False

    def _step_prelogin(self, responses: list[bytes]) -> bool:
        message = reassemble(self._buffer)
        if message is None:
            return False
        del self._buffer[:message.consumed]

        if message.type is not PacketType.PRELOGIN:
            raise TdsProtocolError(
                f"expected PRELOGIN to open the connection, got {message.type.name}"
            )

        self._client_prelogin = Prelogin.parse(message.payload)
        agreed = self._negotiate_encryption(self._client_prelogin.encryption)
        self._session_encrypted = agreed is Encryption.ON
        response = server_response(
            version=self._version, encryption=agreed, asked=self._client_prelogin
        )
        # The reference server answers PRELOGIN with a TABULAR_RESULT packet
        # rather than echoing the PRELOGIN type.
        responses.append(
            build_packet(PacketType.TABULAR_RESULT, response.build())
        )

        self._tunnel = TlsTunnel(server_context(self._certificate))
        self._state = ConnectionState.TLS_HANDSHAKE
        return True

    def _negotiate_encryption(self, requested: Encryption | None) -> Encryption:
        """Decide what to answer the client's ENCRYPTION option with.

        A client that asked for ON is telling us it will not read cleartext
        afterwards, so agreeing to OFF strands it. The reference server's OFF
        is the answer to an OFF request, not a fixed policy.
        """
        if self._encryption is Encryption.REQUIRED:
            return Encryption.ON
        if requested in (Encryption.ON, Encryption.REQUIRED):
            return Encryption.ON
        return Encryption.OFF

    def _take_message(self):
        """The next whole message, decrypted first when the session is encrypted.

        Before the tunnel exists, and in login-only mode after it has served
        its purpose, the bytes on the wire are already plaintext.
        """
        if self._session_encrypted and self._tunnel is not None:
            if self._buffer:
                self._plaintext += self._tunnel.unwrap(bytes(self._buffer))
                self._buffer.clear()
            source = self._plaintext
        else:
            source = self._buffer

        message = reassemble(source)
        if message is None:
            return None
        del source[:message.consumed]
        return message

    def _send(self, responses: list[bytes], payload: bytes,
              packet_id: int = 1) -> None:
        """Queue a response, encrypting it when the session is encrypted.

        Stamped with the session id from the login response onward. SQL Server
        sends zero through the handshake and the real id afterwards, and a
        client reading zero on an established session has nothing to identify
        it by.
        """
        packets = build_message(
            PacketType.TABULAR_RESULT, payload, packet_size=DEFAULT_PACKET_SIZE,
            spid=self._spid, packet_id_start=packet_id,
        )
        if self._session_encrypted and self._tunnel is not None:
            responses.append(self._tunnel.wrap(b"".join(packets)))
        else:
            responses.extend(packets)

    def _step_handshake(self, responses: list[bytes]) -> bool:
        assert self._tunnel is not None
        message = reassemble(self._buffer)
        if message is None:
            return False
        del self._buffer[:message.consumed]

        records = self._tunnel.advance_handshake(message.payload)
        if records:
            responses.extend(wrap_handshake(records))

        if self._tunnel.handshake_complete:
            # Anything still buffered is application data, not framed.
            self._state = ConnectionState.EXPECT_LOGIN
        return True

    def _step_login(self, responses: list[bytes]) -> bool:
        assert self._tunnel is not None
        moved = False

        if self._buffer:
            self._plaintext += self._tunnel.unwrap(bytes(self._buffer))
            self._buffer.clear()
            moved = True

        message = reassemble(self._plaintext)
        if message is None:
            return moved

        del self._plaintext[:message.consumed]
        if message.type is not PacketType.LOGIN7:
            raise TdsProtocolError(
                f"expected LOGIN7 through the tunnel, got {message.type.name}"
            )

        self._login = Login7.parse(message.payload)
        # Answered with a version the client offered rather than the one
        # this would have chosen. A client told about a newer protocol
        # than it asked for cannot read the reply: the legacy ODBC driver
        # asks for 7.1, and given 7.4 concludes it is talking to something
        # older than SQL Server 6.5 and hangs up.
        self._tds_version = negotiate(self._login.tds_version)
        if not self._login.uses_integrated_auth:
            raise AuthenticationError(
                "this login carries no SSPI token, so it is asking for SQL "
                "authentication, which is not implemented"
            )

        # The tunnel has done its job. Everything from here is cleartext,
        # which is what the reference server does once the login is through.
        self._acceptor = self._acceptor_factory()
        result = self._acceptor.step(self._login.sspi)
        if result.token and not result.complete:
            self._send(responses, sspi_token(result.token), packet_id=0)

        if result.complete:
            self._finish_login(responses)
        else:
            self._state = ConnectionState.EXPECT_SSPI
        return True

    def _step_sspi(self, responses: list[bytes]) -> bool:
        assert self._acceptor is not None
        message = self._take_message()
        if message is None:
            return False

        if message.type is not PacketType.SSPI:
            raise TdsProtocolError(
                f"expected an SSPI message, got {message.type.name}"
            )

        # The client's half carries the blob raw: no token byte, no length.
        # Only the server's direction is framed as a token.
        result = self._acceptor.step(message.payload)
        # The token that comes back on the completing step is not sent. SSPI
        # produces one, and SQL Server does not pass it on: measured through a
        # proxy, a real server answers the last client token with the login
        # response alone. Sending it as well leaves the legacy ODBC driver
        # trying to continue a handshake that is already finished.
        if result.token and not result.complete:
            self._send(responses, sspi_token(result.token), packet_id=0)
        if result.complete:
            self._finish_login(responses)
        return True

    def _finish_login(self, responses: list[bytes]) -> None:
        """Tell the client it is in.

        Until this goes out the client has authenticated but heard nothing
        back, so it sits waiting on a reply that decides whether it connected.
        """
        self._spid = next_session_id()
        self._send(
            responses,
            login_response(
                version=(
                    self._version.major,
                    self._version.minor,
                    self._version.build,
                ),
                server_name=self._server_name,
                database=(self._login.database or "master") if self._login else "master",
                packet_size=DEFAULT_PACKET_SIZE,
                tds_version=self._tds_version,
            ),
        )
        self._state = ConnectionState.READY

    def _step_query(self, responses: list[bytes]) -> bool:
        message = self._take_message()
        if message is None:
            return False

        # Clients send anything parameterised, and every catalog query .NET
        # issues, as an RPC call to sp_executesql rather than as a batch.
        if message.type is PacketType.SSPI:
            # A trailing authentication token, arriving after the login was
            # already answered. SPNEGO lets the client send one last leg once
            # the server has accepted, and Windows has: SSPI returned
            # SEC_E_OK, which is what made the login complete. The legacy ODBC
            # driver always sends it, and refusing it there ends the
            # connection with a protocol error rather than a query.
            return True

        parameters: dict[str, object] = {}
        procedure: str | None = None
        arguments: list[object] = []
        if message.type is PacketType.SQL_BATCH:
            self._last_query = parse_sql_batch(message.payload,
                                               self._tds_version)
        elif message.type is PacketType.RPC:
            call = parse_rpc(message.payload, self._tds_version)
            if call.sql is None:
                # Not sp_executesql, so the client called something by name.
                # Passed on rather than refused here: which procedures exist
                # is a question about the catalog, and answering it in the
                # protocol layer is what made every client show an empty
                # table picker.
                # Named so an operator watching the log sees the call. It
                # used to read as silence, which is how a provider asking for
                # a procedure looked identical to a provider asking nothing.
                self._last_query = f"EXEC {call.procedure}"
                procedure = call.procedure
                arguments = [p.value for p in call.parameters if not p.name]
                parameters = {p.name: p.value for p in call.parameters if p.name}
            else:
                self._last_query = call.sql
                # The first two parameters are the statement and its
                # declarations; only the named ones after them are values.
                parameters = {p.name: p.value for p in call.parameters if p.name}
        else:
            raise TdsProtocolError(
                f"expected a SQL batch or an RPC, got {message.type.name}"
            )
        try:
            payload = self._query_handler(
                Query(sql=self._last_query, parameters=parameters,
                      procedure=procedure, arguments=arguments,
                      session=self._about())
            ).encode(self._tds_version)
        except QueryError as exc:
            # A failed query is a normal answer, not a broken connection. The
            # client reports it and stays connected to ask something else.
            payload = error_response(
                exc.number, str(exc), server=self._server_name, severity=exc.severity
            )

        # A result set can outgrow one packet, and the size the client was
        # told to expect is the one it will read.
        self._send(responses, payload)
        return True


def _no_queries(sql: str) -> QueryResult:
    """The default handler, which answers nothing.

    Returning an empty result set would be worse than an error: the client
    would report success and show no rows, and nothing would say why.
    """
    raise QueryError(
        "pysqlbridge has no data source configured, so it cannot answer queries"
    )
