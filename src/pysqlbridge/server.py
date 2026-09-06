"""A socket listener that runs the login sequence.

Thin on purpose. Everything about the protocol lives in tds.connection, which
knows nothing about sockets; this module only moves bytes between a socket and
that state machine, one thread per client.

It carries a client through login and then holds the connection open. Query
handling does not exist yet, so a client that goes on to send one waits until
its own timeout. Closing the socket instead would be faster but would report a
broken connection for what is really an unimplemented feature.
"""

from __future__ import annotations

import logging
import socket
import socketserver
import threading

from . import DEFAULT_PORT
from .certificate import Certificate, self_signed
from .tds.connection import Connection, ConnectionState

log = logging.getLogger(__name__)

RECV_SIZE = 65536

# A client that connects and says nothing should not hold a thread forever.
CLIENT_TIMEOUT_SECONDS = 30.0


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        peer = self.client_address
        self.request.settimeout(CLIENT_TIMEOUT_SECONDS)
        connection = Connection(self.server.certificate)
        announced = False
        log.info("connection from %s:%s", *peer[:2])

        try:
            while True:
                data = self.request.recv(RECV_SIZE)
                if not data:
                    log.info("%s:%s closed the connection", *peer[:2])
                    return

                for response in connection.receive(data):
                    self.request.sendall(response)

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
    ) -> None:
        # Generated once and shared, not per connection: RSA key generation is
        # slow enough that doing it per client would be a denial of service
        # anyone could trigger by connecting repeatedly.
        self.certificate = certificate or self_signed()
        super().__init__((host, port), _Handler)

    @property
    def address(self) -> tuple[str, int]:
        return self.server_address[:2]


def serve(
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    certificate: Certificate | None = None,
) -> None:
    """Run until interrupted."""
    with BridgeServer(host, port, certificate) as server:
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
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s"
    )
    serve(args.host, args.port)


def start_background(
    host: str = "127.0.0.1", port: int = 0, certificate: Certificate | None = None
) -> tuple[BridgeServer, threading.Thread]:
    """Start a server on its own thread and return it with that thread.

    Port 0 asks the OS for a free one, which keeps tests off a fixed port.
    Call shutdown() then server_close() on the server when finished.
    """
    server = BridgeServer(host, port, certificate)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


if __name__ == "__main__":
    _main()
