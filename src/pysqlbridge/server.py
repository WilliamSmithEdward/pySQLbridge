"""A socket listener that runs the login sequence.

Thin on purpose. Everything about the protocol lives in tds.connection, which
knows nothing about sockets; this module only moves bytes between a socket and
that state machine, one thread per client.

It carries a client through login and then answers batches through whatever
query handler it was given. With no handler the connection still completes and
every query returns an error saying no data source is configured, which is the
honest answer while the source layer does not exist.
"""

from __future__ import annotations

import logging
import socket
import socketserver
import threading

from . import DEFAULT_PORT
from .certificate import Certificate, self_signed
from .tds.connection import Connection, ConnectionState
from .tds.result import Column, Float, Integer, NVarChar, QueryResult

log = logging.getLogger(__name__)

RECV_SIZE = 65536

# A client that connects and says nothing should not hold a thread forever.
CLIENT_TIMEOUT_SECONDS = 30.0


DEMO_COLUMNS = [
    Column("id", Integer(4)),
    Column("name", NVarChar(40)),
    Column("score", Float(8)),
    Column("retired", Integer(4)),
]

DEMO_ROWS = [
    [1, "ada", 99.5, None],
    [2, "grace", 87.25, None],
    [3, "edsger", 78.0, 1],
]


def demo_handler(sql: str) -> QueryResult:
    """Answer every batch with the same small table.

    A demonstration, not a feature. It ignores the SQL entirely, which is why
    it is behind a flag rather than being the default: a server that silently
    returns the same rows whatever it was asked is worse than one that says it
    cannot answer.
    """
    return QueryResult(columns=DEMO_COLUMNS, rows=DEMO_ROWS)


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        peer = self.client_address
        self.request.settimeout(CLIENT_TIMEOUT_SECONDS)
        connection = Connection(
            self.server.certificate,
            query_handler=self.server.query_handler,
        )
        announced = False
        last_seen: str | None = None
        log.info("connection from %s:%s", *peer[:2])

        try:
            while True:
                data = self.request.recv(RECV_SIZE)
                if not data:
                    log.info("%s:%s closed the connection", *peer[:2])
                    return

                for response in connection.receive(data):
                    self.request.sendall(response)

                if connection.last_query and connection.last_query != last_seen:
                    last_seen = connection.last_query
                    log.info("%s:%s query: %s", *peer[:2],
                             " ".join(last_seen.split())[:120])

                if connection.state is ConnectionState.READY and not announced:
                    announced = True
                    login = connection.login
                    log.info(
                        "%s:%s logged in as %s (app %r, database %r)",
                        *peer[:2],
                        connection.username or "an unnamed principal",
                        login.app_name if login else "",
                        login.database if login else "",
                    )
        except socket.timeout:
            log.warning("%s:%s went quiet in state %s",
                        *peer[:2], connection.state.name)
        except Exception as exc:
            log.warning("%s:%s failed in state %s: %s",
                        *peer[:2], connection.state.name, exc)


class BridgeServer(socketserver.ThreadingTCPServer):
    """Listens for clients and hands each one a Connection."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        certificate: Certificate | None = None,
        query_handler=None,
    ) -> None:
        # Generated once and shared, not per connection: RSA key generation is
        # slow enough that doing it per client would be a denial of service
        # anyone could trigger by connecting repeatedly.
        self.certificate = certificate or self_signed()
        self.query_handler = query_handler
        super().__init__((host, port), _Handler)

    @property
    def address(self) -> tuple[str, int]:
        return self.server_address[:2]


def serve(
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    certificate: Certificate | None = None,
    query_handler=None,
) -> None:
    """Run until interrupted."""
    with BridgeServer(host, port, certificate, query_handler) as server:
        log.info("listening on %s:%s", *server.address)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            log.info("shutting down")


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run the pySQLbridge listener")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--debug", action="store_true", help="log every state transition"
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="answer every query with a fixed sample table, ignoring the SQL",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    serve(args.host, args.port,
          query_handler=demo_handler if args.demo else None)


def start_background(
    host: str = "127.0.0.1",
    port: int = 0,
    certificate: Certificate | None = None,
    query_handler=None,
) -> tuple[BridgeServer, threading.Thread]:
    """Start a server on its own thread and return it with that thread.

    Port 0 asks the OS for a free one, which keeps tests off a fixed port.
    Call shutdown() then server_close() on the server when finished.
    """
    server = BridgeServer(host, port, certificate, query_handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


if __name__ == "__main__":
    _main()
