"""A local API with an awkward surface, for developing the crawler against.

Public APIs are the real test of shape detection, but they are a bad test of
crawling: they rate-limit, they change, they go down, and pointing a breadth
first walk at somebody else's server to see what happens is rude. This serves
every shape the crawler has to handle, from one process, in memory, on a port
the operating system picks.

Run it by hand to poke at it:

    python tests/fake_api.py --port 8099

Then http://127.0.0.1:8099/api/v1/ is a base endpoint to point the bridge at.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

# A key the private routes want. Fake, and only ever compared against itself.
API_KEY = "dev-key-not-a-secret"

USERS = [
    {"id": i, "name": f"user-{i}", "email": f"user{i}@example.test",
     "team": {"id": 1 + i % 3, "name": ["red", "green", "blue"][i % 3]},
     "active": i % 4 != 0}
    for i in range(1, 201)
]

ORDERS = [
    {"id": 1000 + i, "user_id": 1 + i % 25, "total": round(9.99 * (i % 7 + 1), 2),
     "status": ["new", "shipped", "closed"][i % 3]}
    for i in range(1, 41)
]


def _page(rows: list, base: str, query: dict) -> dict:
    """The paginated envelope shape: count, next, results."""
    limit = int(query.get("limit", ["10"])[0])
    offset = int(query.get("offset", ["0"])[0])
    window = rows[offset:offset + limit]
    following = offset + limit
    return {
        "count": len(rows),
        "next": (f"{base}?limit={limit}&offset={following}"
                 if following < len(rows) else None),
        "previous": None,
        "results": window,
    }


class Api:
    """The routes, as methods. Each returns the document for one path."""

    def __init__(self, origin: str) -> None:
        self.origin = origin
        self.hits: list[str] = []
        # Seconds to stall each request. Real APIs answer in tens or hundreds
        # of milliseconds, and anything measuring how requests overlap needs
        # that wait to exist: with an instant server, serial and parallel are
        # indistinguishable.
        self.latency = 0.0

    def base(self, path: str) -> str:
        return f"{self.origin}{path}"

    def answer(self, path: str, query: dict, headers) -> tuple[int, object]:
        self.hits.append(path)
        if self.latency:
            import time

            time.sleep(self.latency)
        name = "_" + path.strip("/").replace("/", "_").replace("-", "_").replace(".", "_")
        route = getattr(self, name, None)
        if route is None:
            return 404, {"error": "not found", "path": path}
        if path.startswith("/api/v1/private"):
            offered = (headers.get("X-API-Key")
                       or query.get("api_key", [None])[0]
                       or (headers.get("Authorization", "").removeprefix("Bearer ")
                           or None))
            if offered != API_KEY:
                return 401, {"error": "unauthorized",
                             "message": "send X-API-Key, ?api_key= or a bearer token"}
        return 200, route(query)

    # -- the index: a map of names to URLs, the way the PokeAPI root works ---
    def _api_v1(self, query: dict) -> object:
        return {
            "users": self.base("/api/v1/users"),
            "orders": self.base("/api/v1/orders"),
            "metrics": self.base("/api/v1/metrics"),
            "rates": self.base("/api/v1/rates"),
            "settings": self.base("/api/v1/settings"),
            "regions": self.base("/api/v1/regions"),
            "topology": self.base("/api/v1/topology"),
            "audit": self.base("/api/v1/private/audit"),
            "documentation": "https://example.test/docs",
        }

    # -- an envelope with rows under "results", paginated --------------------
    def _api_v1_users(self, query: dict) -> object:
        return _page(USERS, self.base("/api/v1/users"), query)

    # -- a bare top-level array ----------------------------------------------
    def _api_v1_orders(self, query: dict) -> object:
        return ORDERS

    # -- parallel arrays, one per column -------------------------------------
    def _api_v1_metrics(self, query: dict) -> object:
        hours = [f"2026-09-06T{h:02d}:00" for h in range(24)]
        return {
            "generated_at": "2026-09-06T00:00:00Z",
            "units": {"latency_ms": "ms", "requests": "count"},
            "hourly": {
                "time": hours,
                "latency_ms": [round(20 + 9 * (h % 5), 1) for h in range(24)],
                "requests": [100 + 13 * h for h in range(24)],
            },
        }

    # -- a map keyed by a code -----------------------------------------------
    def _api_v1_rates(self, query: dict) -> object:
        return {"base": "EUR", "date": "2026-09-06",
                "rates": {"USD": 1.08, "GBP": 0.85, "NOK": 11.6, "SEK": 11.2,
                          "JPY": 162.4, "CHF": 0.95}}

    # -- one object that is one row ------------------------------------------
    def _api_v1_settings(self, query: dict) -> object:
        return {"retention_days": 30, "region": "eu-north-1",
                "maintenance_window": "sun 02:00", "read_only": False}

    # -- a map of objects ----------------------------------------------------
    def _api_v1_regions(self, query: dict) -> object:
        return {
            "eu-north-1": {"city": "Stockholm", "zones": 3, "launched": 2018},
            "us-east-1": {"city": "Ashburn", "zones": 6, "launched": 2006},
            "ap-south-1": {"city": "Mumbai", "zones": 3, "launched": 2016},
        }

    # -- needs a credential --------------------------------------------------
    def _api_v1_private_audit(self, query: dict) -> object:
        return {"results": [{"id": 1, "action": "login", "actor": "user-1"},
                            {"id": 2, "action": "export", "actor": "user-7"}]}

    # -- deliberately not tabular: nesting all the way down ------------------
    def _api_v1_topology(self, query: dict) -> object:
        return {"cluster": {"nodes": {"a": {"peers": {"b": {"latency": 1}}}}}}

    # -- a machine-readable description of the surface -----------------------
    def _openapi_json(self, query: dict) -> object:
        def listing(summary: str) -> dict:
            return {"get": {"summary": summary,
                            "responses": {"200": {"description": "ok"}}}}

        return {
            "openapi": "3.0.0",
            "info": {"title": "fake", "version": "1.0.0"},
            "servers": [{"url": self.origin}],
            "paths": {
                "/api/v1/users": listing("Every user"),
                "/api/v1/orders": listing("Every order"),
                "/api/v1/rates": listing("Exchange rates"),
                "/api/v1/regions": listing("Every region"),
                "/api/v1/users/{id}": listing("One user"),
                "/api/v1/private/audit": listing("The audit log"),
                "/api/v1/topology": listing("Cluster topology"),
            },
        }


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:                       # noqa: N802
        parts = urlsplit(self.path)
        status, document = self.server.api.answer(
            parts.path, parse_qs(parts.query), self.headers
        )
        body = json.dumps(document).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        """Quiet. A test suite that prints a request log is unreadable."""


class FakeApi:
    """The server, as a context manager. Picks its own port."""

    def __init__(self, port: int = 0) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.server.api = Api(self.origin)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def hits(self) -> list[str]:
        """Every path asked for, so a test can assert on what was crawled."""
        return self.server.api.hits

    def url(self, path: str = "/api/v1/") -> str:
        return f"{self.origin}{path}"

    def __enter__(self) -> "FakeApi":
        self.thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Serve the fake API for dev.")
    parser.add_argument("--port", type=int, default=8099)
    chosen = parser.parse_args()
    with FakeApi(chosen.port) as running:
        print(f"serving {running.url()}   x-api-key: {API_KEY}")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            print()
