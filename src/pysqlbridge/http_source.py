"""Tables backed by an HTTP endpoint.

Kept apart from source.py so the network stays out of the pure parsing. What
comes back is handed to from_records, which means an API and a JSON file are
shaped into a table by exactly the same rules.

Two things a file source does not need:

A finite timeout, on every request, always. A source that hangs holds the
connection thread that asked for it, and a client waiting on a query has no way
to tell a slow API from a broken server.

A cache with an expiry. Refetching per query would turn one client's table scan
into a burst of identical requests at somebody else's API, and never refetching
would serve the first response forever. The default leans long: this is a
read-only bridge over data somebody else publishes, not a live feed.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from .source import SourceError, Table, from_records

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_TTL_SECONDS = 300.0

# Sent so an operator reading their logs can tell what is calling them.
USER_AGENT = "pysqlbridge"

Fetcher = Callable[[str, dict[str, str], float], bytes]


def fetch(url: str, headers: dict[str, str], timeout: float) -> bytes:
    """Retrieve a URL, or raise SourceError explaining why not."""
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", **headers}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise SourceError(f"{url} returned HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise SourceError(f"could not reach {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise SourceError(f"{url} did not answer within {timeout:g}s") from exc


def _article(word: str) -> str:
    return f"an {word}" if word[:1].lower() in "aeiou" else f"a {word}"


def extract(payload: object, path: str | None, url: str) -> object:
    """Walk a dotted path into the response.

    Most APIs wrap their rows in an envelope, so they are rarely at the top
    level. PokeAPI puts them under "results" beside a count and paging links,
    and selecting from the envelope would give one row of metadata.

    A numeric segment indexes a list rather than naming a key, which is how the
    World Bank's [metadata, rows] response is reachable as "1".
    """
    if not path:
        return payload

    current = payload
    walked: list[str] = []
    for part in path.split("."):
        walked.append(part)
        here = ".".join(walked[:-1]) or "the response"

        if isinstance(current, list):
            if not part.lstrip("-").isdigit():
                raise SourceError(
                    f"{url}: '{here}' is a list, so '{part}' has to be a "
                    f"number, not a name"
                )
            index = int(part)
            if not -len(current) <= index < len(current):
                raise SourceError(
                    f"{url}: '{here}' has {len(current)} elements, so there is "
                    f"no [{index}]"
                )
            current = current[index]
            continue

        if not isinstance(current, dict):
            raise SourceError(
                f"{url}: '{'.'.join(walked)}' is not reachable, because "
                f"'{here}' is {_article(type(current).__name__)}, not an object"
            )
        if part not in current:
            available = ", ".join(sorted(current)[:8]) or "nothing"
            raise SourceError(f"{url}: no '{part}' in {here}; it has: {available}")
        current = current[part]
    return current


# How to get from the value at `path` to a list of records. Named rather than
# sniffed: a sniffer looking for "the array in this document" would have served
# REST Countries' {"errors": [...]} rejection as a table.
STRATEGIES = ("array", "single", "values", "entries", "columns")

ENTRY_KEY = "key"
ENTRY_VALUE = "value"


def locate(payload: object, strategy: str, url: str) -> list:
    """Turn the value at the path into a list of records."""
    if strategy == "array":
        if not isinstance(payload, list):
            raise SourceError(
                f"{url} is {_article(type(payload).__name__)} where a list was "
                f'expected; try a different "records" strategy, one of '
                f"{', '.join(STRATEGIES)}"
            )
        return payload

    if strategy == "single":
        if not isinstance(payload, dict):
            raise SourceError(
                f'{url}: "single" needs an object, and this is '
                f"{_article(type(payload).__name__)}"
            )
        return [payload]

    if strategy in ("values", "entries", "columns"):
        if not isinstance(payload, dict):
            raise SourceError(
                f'{url}: "{strategy}" needs an object, and this is '
                f"{_article(type(payload).__name__)}"
            )

    if strategy == "values":
        return list(payload.values())

    if strategy == "entries":
        # A map of scalars is rows turned sideways: Frankfurter's
        # {"USD": 1.08, "GBP": 0.85} is two rows, not two columns.
        return [{ENTRY_KEY: k, ENTRY_VALUE: v} for k, v in payload.items()]

    if strategy == "columns":
        # Parallel arrays, one per column, as Open-Meteo returns forecasts.
        arrays = {k: v for k, v in payload.items() if isinstance(v, list)}
        if not arrays:
            raise SourceError(
                f'{url}: "columns" needs arrays to zip, and none of '
                f"{', '.join(sorted(payload)[:8])} is one"
            )
        lengths = {len(v) for v in arrays.values()}
        if len(lengths) != 1:
            sizes = ", ".join(f"{k}={len(v)}" for k, v in sorted(arrays.items()))
            raise SourceError(
                f'{url}: "columns" needs every array the same length, and they '
                f"are not: {sizes}"
            )
        names = list(arrays)
        return [dict(zip(names, values)) for values in zip(*arrays.values())]

    raise SourceError(
        f"'{strategy}' is not a records strategy; use one of "
        f"{', '.join(STRATEGIES)}"
    )


@dataclass
class HttpSource:
    """A table fetched from a URL and remembered for a while."""

    name: str
    url: str
    path: str | None = None
    records: str = "array"
    flatten: bool = True
    columns: list[str] | None = None
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    ttl: float = DEFAULT_TTL_SECONDS
    headers: dict[str, str] = field(default_factory=dict)
    fetcher: Fetcher = fetch
    clock: Callable[[], float] = time.monotonic

    _cached: Table | None = field(default=None, init=False, repr=False)
    _fetched_at: float = field(default=0.0, init=False, repr=False)

    def load(self) -> Table:
        """The table, fetching it again only once the cache has expired."""
        if self._cached is not None and self.clock() - self._fetched_at < self.ttl:
            return self._cached

        raw = self.fetcher(self.url, self.headers, self.timeout)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SourceError(f"{self.url} did not return valid JSON: {exc}") from exc

        located = locate(
            extract(payload, self.path, self.url), self.records, self.url
        )
        table = from_records(
            located,
            name=self.name,
            origin=self.url,
            flatten=self.flatten,
            columns=self.columns,
        )
        self._cached = table
        self._fetched_at = self.clock()
        return table

    def invalidate(self) -> None:
        """Forget the cached response, so the next load fetches again."""
        self._cached = None
        self._fetched_at = 0.0


@dataclass(frozen=True)
class StaticSource:
    """A table that was read once and does not change.

    Files load at startup, so this only has to hand back what it holds. It
    exists so the catalog has one kind of thing to ask, rather than a union it
    has to keep testing.
    """

    table: Table

    @property
    def name(self) -> str:
        return self.table.name

    def load(self) -> Table:
        return self.table
