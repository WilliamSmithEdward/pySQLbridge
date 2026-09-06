"""Tables backed by an HTTP endpoint.

Kept apart from source.py so the network stays out of the pure parsing. What
comes back is handed to from_records, which means an API and a JSON file are
shaped into a table by exactly the same rules.

A source may name several URLs serving the same data, a load-balanced set or
a set of mirrors. They are raced and the first useful answer wins, which is
wait-any rather than wait-all: one slow replica cannot hold up a query that
another already answered.

Two things a file source does not need:

A finite timeout, on every request, always. A source that hangs holds the
connection thread that asked for it, and a client waiting on a query has no way
to tell a slow API from a broken server.

A cache with an expiry. Refetching per query would turn one client's table scan
into a burst of identical requests at somebody else's API, and never refetching
would serve the first response forever. The default leans long: this is a
read-only bridge over data somebody else publishes, not a live feed.

Once something has been served, an expiry refreshes in the background and the
waiting query gets the previous answer immediately. A stale row beats a query
that blocks for a second; the first fetch still waits, because there is nothing
else to give it.
"""

from __future__ import annotations

import concurrent.futures
import gzip
import json
import threading
import time
import zlib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from .source import SourceError, Table, from_records

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_TTL_SECONDS = 300.0

# Following a next link is unbounded by nature: the API decides when to stop.
# These are the bounds that make it safe to point at something unfamiliar.
DEFAULT_MAX_PAGES = 10
DEFAULT_MAX_ROWS = 100_000

# Sent so an operator reading their logs can tell what is calling them.
USER_AGENT = "pysqlbridge"

Fetcher = Callable[[str, dict[str, str], float], bytes]


def fetch(url: str, headers: dict[str, str], timeout: float) -> bytes:
    """Retrieve a URL, or raise SourceError explaining why not.

    Compression is asked for because these payloads are JSON, which compresses
    to a fraction of itself, and the cost of a fetch is almost entirely the
    bytes on the wire.
    """
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            **headers,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            return _decompress(body, response.headers.get("Content-Encoding", ""))
    except urllib.error.HTTPError as exc:
        raise SourceError(f"{url} returned HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise SourceError(f"could not reach {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise SourceError(f"{url} did not answer within {timeout:g}s") from exc


def _decompress(body: bytes, encoding: str) -> bytes:
    """Undo whatever the server compressed with, if anything."""
    encoding = (encoding or "").lower().strip()
    try:
        if encoding == "gzip":
            return gzip.decompress(body)
        if encoding == "deflate":
            # Some servers send raw deflate without the zlib wrapper.
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except (OSError, zlib.error) as exc:
        raise SourceError(f"could not decompress a {encoding} response: {exc}") from exc
    return body


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
STRATEGIES = ("array", "single", "values", "entries", "columns", "scalars")

ENTRY_KEY = "key"
ENTRY_VALUE = "value"

# An array of bare strings or numbers has no key to name a column with, so
# one is supplied. Chuck Norris' joke categories are sixteen strings and
# nothing else.
SCALAR_COLUMN = "value"


def locate(payload: object, strategy: str, url: str) -> list:
    """Turn the value at the path into a list of records."""
    if strategy == "array":
        if isinstance(payload, list) and payload and not any(
            isinstance(v, dict) for v in payload
        ):
            # Naming the fix beats reporting that a string is not an object.
            raise SourceError(
                f"{url} is a list of plain values rather than objects, so its "
                f'column cannot be named; use "records": "scalars"'
            )
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

    if strategy == "scalars":
        if not isinstance(payload, list):
            raise SourceError(
                f'{url}: "scalars" needs a list, and this is '
                f"{_article(type(payload).__name__)}"
            )
        nested = next((v for v in payload if isinstance(v, (dict, list))), None)
        if nested is not None:
            raise SourceError(
                f'{url}: "scalars" is for a list of plain values, and this one '
                f"holds {_article(type(nested).__name__)}; use \"array\" instead"
            )
        return [{SCALAR_COLUMN: value} for value in payload]

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
    url: str | list[str]
    path: str | None = None
    records: str = "array"
    flatten: bool = True
    columns: list[str] | None = None
    next_key: str | None = None
    max_pages: int = DEFAULT_MAX_PAGES
    max_rows: int = DEFAULT_MAX_ROWS
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    ttl: float = DEFAULT_TTL_SECONDS
    headers: dict[str, str] = field(default_factory=dict)
    fetcher: Fetcher = fetch
    clock: Callable[[], float] = time.monotonic

    _cached: Table | None = field(default=None, init=False, repr=False)
    _fetched_at: float = field(default=0.0, init=False, repr=False)
    # One connection per client means several threads can reach an expired
    # source at the same moment. Without this they all fetch, which turns a
    # busy minute into a burst at somebody else's API.
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _refreshing: bool = field(default=False, init=False, repr=False)

    @property
    def urls(self) -> list[str]:
        """Every URL that serves this table, in preference order."""
        return [self.url] if isinstance(self.url, str) else list(self.url)

    @property
    def origin(self) -> str:
        """What to call this source in a message."""
        urls = self.urls
        return urls[0] if len(urls) == 1 else f"{urls[0]} (+{len(urls) - 1} more)"

    def _race(self) -> tuple[str, bytes]:
        """Fetch from every URL at once and take the first useful answer.

        First successful, not first finished. A mirror that fails fast would
        otherwise beat one that succeeds slowly, which is the opposite of what
        racing them is for.

        The losers are not waited on. Their requests carry the same timeout, so
        they end on their own, and their answers are simply not read.
        """
        urls = self.urls
        if len(urls) == 1:
            return urls[0], self.fetcher(urls[0], self.headers, self.timeout)

        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=len(urls), thread_name_prefix="pysqlbridge-mirror"
        )
        try:
            pending = {
                pool.submit(self.fetcher, url, self.headers, self.timeout): url
                for url in urls
            }
            failures: list[str] = []
            remaining = set(pending)

            while remaining:
                finished, remaining = concurrent.futures.wait(
                    remaining, return_when=concurrent.futures.FIRST_COMPLETED
                )
                for future in finished:
                    try:
                        return pending[future], future.result()
                    except SourceError as exc:
                        failures.append(f"{pending[future]}: {exc}")
                    except Exception as exc:      # noqa: BLE001
                        failures.append(f"{pending[future]}: {exc}")

            raise SourceError(
                f"every URL for '{self.name}' failed:\n  "
                + "\n  ".join(failures)
            )
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _fresh(self) -> Table | None:
        cached = self._cached
        if cached is not None and self.clock() - self._fetched_at < self.ttl:
            return cached
        return None

    def load(self) -> Table:
        """The table, refetching only once the cache has expired.

        An expiry with something already cached refreshes in the background and
        hands back the previous answer, so no query waits on the network twice.
        The very first load has nothing to serve and does wait.
        """
        fresh = self._fresh()
        if fresh is not None:
            return fresh

        stale = self._cached
        if stale is not None:
            self._begin_background_refresh()
            return stale

        with self._lock:
            # Checked again inside the lock: whoever held it may have just
            # filled the cache while this thread waited.
            fresh = self._fresh()
            if fresh is not None:
                return fresh
            return self._refresh()

    def _begin_background_refresh(self) -> None:
        """Start one refresh, and only one, behind the returning query."""
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True

        def run() -> None:
            try:
                with self._lock:
                    self._refresh()
            except Exception:      # noqa: BLE001
                # The stale table stays served. A background failure must not
                # take down the query that already got its answer.
                self._refreshing = False

        threading.Thread(
            target=run, name=f"pysqlbridge-refresh-{self.name}", daemon=True
        ).start()

    def _decode(self, url: str, raw: bytes) -> object:
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SourceError(f"{url} did not return valid JSON: {exc}") from exc

    def _follow(self, payload: object) -> str | None:
        """The next page's URL, if the response offers one.

        Read through the same dotted path machinery as the rows, because APIs
        put it in as many places: PokeAPI at the top level, Rick and Morty
        under info.
        """
        if not self.next_key:
            return None
        try:
            nxt = extract(payload, self.next_key, self.origin)
        except SourceError:
            return None      # a last page often drops the key entirely
        return nxt if isinstance(nxt, str) and nxt else None

    def _refresh(self) -> Table:
        url, raw = self._race()
        payload = self._decode(url, raw)
        records = list(locate(extract(payload, self.path, url), self.records, url))

        pages = 1
        following = self._follow(payload)
        while following and pages < self.max_pages and len(records) < self.max_rows:
            raw = self.fetcher(following, self.headers, self.timeout)
            payload = self._decode(following, raw)
            records.extend(
                locate(extract(payload, self.path, following), self.records, following)
            )
            following = self._follow(payload)
            pages += 1

        if len(records) > self.max_rows:
            # Truncating silently would look like the API only has this many.
            raise SourceError(
                f"{self.name} reached {len(records)} rows over {pages} page(s), "
                f"past the {self.max_rows} it is allowed; narrow the request or "
                f'raise "max_rows"'
            )

        table = from_records(
            records,
            name=self.name,
            origin=url,
            flatten=self.flatten,
            columns=self.columns,
        )
        self._cached = table
        self._fetched_at = self.clock()
        self._refreshing = False
        return table

    def invalidate(self) -> None:
        """Forget the cached response, so the next load fetches again."""
        with self._lock:
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
