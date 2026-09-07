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
import os
import socket
import socketserver
import threading
import time

from . import DEFAULT_PORT
from .catalog import load as load_catalog
from .certificate import Certificate, self_signed
from .procedures import CATALOG
from .tds.connection import Connection, ConnectionState
from .tds.result import Column, Float, Integer, NVarChar, Query, QueryResult

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


def demo_handler(request: Query | str) -> QueryResult:
    """Answer SELECTs with the same small table, and everything else with nothing.

    A demonstration, not a feature: it does not read the SELECT, so every one
    gets the same rows. The one thing it does look at is whether the batch is a
    SELECT at all, because clients open a session with setup batches. SSMS and
    sqlcmd both send SET statements before anything the user typed, and
    answering one of those with a result set makes the client report an invalid
    cursor state on the query it was really waiting for.
    """
    sql = request if isinstance(request, str) else request.sql
    if not sql.lstrip().upper().startswith("SELECT"):
        return QueryResult(columns=[], rows=[])
    return QueryResult(columns=DEMO_COLUMNS, rows=DEMO_ROWS)


# How much of a query the console shows. The rest goes to the log file, if
# there is one, because a console line that wraps four times is worse than a
# short one.
CONSOLE_QUERY_CHARS = 120


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        peer = self.client_address
        self.request.settimeout(CLIENT_TIMEOUT_SECONDS)
        connection = Connection(
            self.server.certificate,
            query_handler=self.server.query_handler,
            database=self.server.database,
            reached_at=self.server.reached_at,
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
                    written = " ".join(last_seen.split())
                    # Short at the console, whole in a log file. A query cut
                    # off at a hundred characters is unreadable exactly when
                    # it matters: a client that will not connect sends a long
                    # batch and the interesting part is never the beginning.
                    log.info("%s:%s query: %s", *peer[:2],
                             written[:CONSOLE_QUERY_CHARS])
                    if len(written) > CONSOLE_QUERY_CHARS:
                        log.debug("%s:%s query in full: %s", *peer[:2], written)

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
            # The line says what happened; the traceback says where, and goes
            # to the log file rather than the console. Without it an internal
            # failure reads as a client that hung up, and the two need
            # different fixes.
            log.warning("%s:%s failed in state %s: %s",
                        *peer[:2], connection.state.name, exc)
            log.debug("%s:%s failed in state %s",
                      *peer[:2], connection.state.name, exc_info=True)


class BridgeServer(socketserver.ThreadingTCPServer):
    """Listens for clients and hands each one a Connection."""

    # Not on Windows, where the flag means something else entirely. On Unix
    # it lets a restarted server take a port still in TIME_WAIT; on Windows
    # it lets a second process bind a port another one is already listening
    # on, and the two then split the clients between them arbitrarily. That
    # is silent and it looks like a server losing its mind: a connection
    # makes a temp table, the next lands on the other process, and the table
    # it just made is not there.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self) -> None:
        if os.name == "nt":
            # Say it outright rather than relying on the default: this port
            # is ours alone while we hold it.
            self.socket.setsockopt(
                socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1
            )
        super().server_bind()

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        certificate: Certificate | None = None,
        query_handler=None,
        database: str = CATALOG,
    ) -> None:
        self.database = database
        # Generated once and shared, not per connection: RSA key generation is
        # slow enough that doing it per client would be a denial of service
        # anyone could trigger by connecting repeatedly.
        self.certificate = certificate or self_signed()
        self.query_handler = query_handler
        super().__init__((host, port), _Handler)

    @property
    def address(self) -> tuple[str, int]:
        return self.server_address[:2]

    @property
    def reached_at(self) -> str:
        """The name to give a client that will bring it back here.

        A real server answers the machine name and that is enough, because it
        listens on every address the name resolves to. This one listens on
        whatever it was told to, usually loopback, and a client handed the
        machine name goes off to the addresses that name resolves to, finds
        nothing listening on any of them, and spends its whole connect
        timeout doing it. Fifteen seconds of silence, and then it carries on
        as though nothing happened.

        So the answer is the address and port it is listening on, written the
        way a client writes one. Bound to every address, the machine name is
        right again, and it is what a client would rather see.
        """
        host, port = self.address
        if host in ("0.0.0.0", "::", ""):
            return socket.gethostname()
        return f"{host},{port}"


def serve(
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    certificate: Certificate | None = None,
    query_handler=None,
    database: str = CATALOG,
) -> None:
    """Run until interrupted."""
    try:
        server = BridgeServer(host, port, certificate, query_handler, database)
    except OSError as exc:
        # Almost always another bridge on the same port. Two of them serving
        # the same clients is worse than neither, so this stops here.
        log.error("cannot listen on %s:%s: %s", host, port, exc)
        log.error("something else holds that port; try --port with another "
                  "number, or stop the bridge that is already running")
        raise SystemExit(1) from None
    with server:
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
        "--log",
        metavar="PATH",
        help="write everything, queries in full, to this file as well",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="serve the tables named in this configuration file",
    )
    parser.add_argument(
        "--no-warm",
        action="store_true",
        help="do not load sources at startup; the first client waits instead",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="answer every SELECT with a fixed sample table, ignoring the SQL",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if (args.debug or args.log) else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    if args.log:
        # The console keeps its level and the file takes everything, so
        # asking for a log does not turn the console into a firehose.
        for existing in logging.getLogger().handlers:
            if isinstance(existing, logging.StreamHandler):
                existing.setLevel(logging.DEBUG if args.debug else logging.INFO)
        to_file = logging.FileHandler(args.log, encoding="utf-8")
        to_file.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
        )
        logging.getLogger().addHandler(to_file)
        log.info("writing a full log to %s", args.log)
    handler = None
    if args.config:
        catalog = load_catalog(args.config)
        log.info("serving %d table(s): %s", len(catalog.names), ", ".join(catalog.names))

        if not args.no_warm:
            # Loading every source now, in parallel, so the first client does
            # not wait for a cold fetch. Measured on seven API sources: 1648 ms
            # one after another against 718 ms at once.
            started = time.perf_counter()
            failed = catalog.warm()
            log.info("warmed %d source(s) in %.0f ms",
                     len(catalog.names), (time.perf_counter() - started) * 1000)
            if failed:
                # Kept in the catalog: a source that is down now may be up by
                # the first query.
                log.warning("could not load: %s", ", ".join(failed))

        handler = catalog.answer
    elif args.demo:
        handler = demo_handler

    serve(args.host, args.port, query_handler=handler)


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
