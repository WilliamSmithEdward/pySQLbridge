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
import http.client
import json
import logging
import math
import threading
import time
import zlib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from .credentials import Credential, with_parameter
from .detect import detect, describe, is_rejection
from .markup import parse_html, parse_xml, sniff, without_bom
from .source import (
    SourceError,
    Table,
    array_columns,
    child_tables,
    csv_records,
    from_records,
    identifying_column,
)

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_TTL_SECONDS = 300.0

# Following a next link is unbounded by nature: the API decides when to stop.
# These are the bounds that make it safe to point at something unfamiliar.
#
# Fifty rather than ten because a page is usually 20 to 50 rows, and ten pages
# stopped every large collection well short of itself: 200 of 1351 pokemon,
# 200 of 826 characters. The row count is the bound that matters, and it is
# the one an operator should tune.
DEFAULT_MAX_PAGES = 50

# How many pages of one collection to ask for at once, once the pattern of its
# links is known. Held below the per-host gate so that reading one big table
# cannot starve every other source pointed at the same server.
DEFAULT_PAGE_WORKERS = 4
DEFAULT_MAX_ROWS = 100_000

# Sent so an operator reading their logs can tell what is calling them.
USER_AGENT = "pysqlbridge"

# Keys an API uses to say how much there is in total. Read to work out how
# many pages a collection has, which is what makes fetching them in parallel
# possible instead of one after the next.
COUNT_KEYS = ("count", "total", "totalCount", "total_count", "totalResults",
              "info.count", "meta.total", "totalSize", "numFound")

# How many requests may be in flight to one host at a time.
#
# Sources load in parallel and each one may be following pages, so without a
# limit a catalog of fifty PokeAPI tables opens several hundred connections to
# one server in a few seconds. Measured: that is enough for the Rick and Morty
# API to start refusing, which arrives as a source that will not load rather
# than as anything identifying the cause. The limit is per host, so unrelated
# APIs still run fully in parallel with each other.
MAX_PARALLEL_PER_HOST = 4

_gates: dict[str, threading.Semaphore] = {}
_gates_lock = threading.Lock()


def _gate(url: str) -> threading.Semaphore:
    """The permit-holder for one host, made on first use."""
    host = urllib.parse.urlsplit(url).netloc.lower()
    with _gates_lock:
        gate = _gates.get(host)
        if gate is None:
            gate = _gates[host] = threading.Semaphore(MAX_PARALLEL_PER_HOST)
        return gate

log = logging.getLogger(__name__)

Fetcher = Callable[[str, dict[str, str], float], bytes]


def fetch(url: str, headers: dict[str, str], timeout: float) -> bytes:
    """Retrieve a URL, or raise SourceError explaining why not.

    Compression is asked for because these payloads are JSON, which compresses
    to a fraction of itself, and the cost of a fetch is almost entirely the
    bytes on the wire.

    Held behind the host's gate, so this stays a well-behaved client no matter
    how many sources point at one server. The wait is inside the call rather
    than around it because every path to the network runs through here:
    loading a table, following its pages, racing mirrors, and crawling.
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
        with _gate(url):
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
                return _decompress(
                    body, response.headers.get("Content-Encoding", "")
                )
    except urllib.error.HTTPError as exc:
        raise SourceError(f"{url} returned HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise SourceError(f"could not reach {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise SourceError(f"{url} did not answer within {timeout:g}s") from exc
    except http.client.HTTPException as exc:
        # A server that promised more than it sent, or hung up part way
        # through. urllib does not wrap these, so IncompleteRead came out of
        # the read as itself and reached the client as an internal error.
        raise SourceError(
            f"{url} stopped part way through its answer: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _advance(url: str, parameter: str, step: int) -> str:
    """The same URL with its paging parameter moved on by one page."""
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    try:
        current = int(query.get(parameter, 0))
    except (TypeError, ValueError):
        current = 0
    return with_parameter(url, parameter, current + step)


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


# How to get from the value at `path` to a list of records. "auto" scores the
# readings and refuses a weak winner; the rest are named explicitly, for a
# document whose shape is known or whose detection needs overriding.
STRATEGIES = (
    "auto", "array", "single", "values", "entries", "columns", "scalars", "json",
)

# The column a JSON-shaped table puts each element into.
JSON_COLUMN = "document"

# What a response is made of. Sniffed by default; named when a server sends
# something the first bytes do not settle.
# csv is not sniffed for, only asked for: every format below announces
# itself in its first bytes and a CSV announces nothing, so guessing at
# one would mean reading any unparseable response as a table of text.
FORMATS = ("auto", "json", "xml", "html", "csv")

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

    if strategy == "json":
        # The escape hatch. Anything at all becomes rows of JSON text: lossless,
        # queryable with LIKE, and honest about not having found a shape. A
        # payload that is genuinely not tabular still has to be servable.
        items = payload if isinstance(payload, list) else [payload]
        return [
            {JSON_COLUMN: value if isinstance(value, str)
             else json.dumps(value, ensure_ascii=False)}
            for value in items
        ]

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


def declared_total(document: object) -> int | None:
    """How many records the response says exist, if it says at all."""
    for key in COUNT_KEYS:
        try:
            value = extract(document, key, "")
        except SourceError:
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            return value
    return None


def stride_between(here: str, there: str) -> tuple[str, int] | None:
    """The query parameter that advances a page, from two consecutive URLs.

    An API that hands out next links is describing its own pagination one step
    at a time. Two consecutive links show which parameter carries the position
    and how far one page moves it, and from that the rest of the links can be
    written down without asking for them, which is what turns forty round
    trips into four.

    Both URLs have to carry the parameter for it to count. The first page is
    usually a bare URL with no query string at all, so comparing it against
    its next link cannot tell an offset from a page size: the PokeAPI answers
    /pokemon with a link to ?offset=20&limit=20, where both parameters look
    equally new. Its second link, ?offset=40&limit=20, settles it.
    """
    first = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(here).query))
    second = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(there).query))
    if urllib.parse.urlsplit(here).path != urllib.parse.urlsplit(there).path:
        return None

    moved = []
    for name, before in first.items():
        after = second.get(name)
        if after is None:
            continue
        try:
            step = int(after) - int(before)
        except (TypeError, ValueError):
            continue
        if step > 0:
            moved.append((name, step))

    # Exactly one. Two parameters moving together cannot be told apart, and
    # guessing which is the position is how a crawl reads the same page twice.
    return moved[0] if len(moved) == 1 else None


@dataclass(frozen=True)
class Paging:
    """How to ask for the next page when there is no link to follow.

    Plenty of APIs paginate without offering a next URL, and say so in the
    envelope instead: dummyjson answers with skip and limit alongside a total
    of 194 while serving 30, GBIF with offset and limit, Algolia with page and
    nbPages. Every one of them echoes the current position back, which is what
    makes this general rather than a guess: the next page is the value the
    response just reported, advanced by one step, put back in the query string
    under the name the response used for it.

        key         where the response reports the current position
        parameter   the query parameter carrying it
        step        how much one page advances it, 1 for page numbers
    """

    key: str
    parameter: str
    step: int = 1

    def next_url(self, url: str, payload: object) -> str | None:
        try:
            current = extract(payload, self.key, url)
        except SourceError:
            return None
        if isinstance(current, bool) or not isinstance(current, int):
            return None
        return with_parameter(url, self.parameter, current + self.step)


@dataclass
class HttpSource:
    """A table fetched from a URL and remembered for a while."""

    name: str
    url: str | list[str]
    path: str | None = None
    records: str = "auto"
    format: str = "auto"
    # Whether an array inside a row becomes a table of its own.
    expand: bool = True
    flatten: bool = True
    columns: list[str] | None = None
    next_key: str | None = None
    paging: Paging | None = None
    max_pages: int = DEFAULT_MAX_PAGES
    page_workers: int = DEFAULT_PAGE_WORKERS
    max_rows: int = DEFAULT_MAX_ROWS
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    ttl: float = DEFAULT_TTL_SECONDS
    headers: dict[str, str] = field(default_factory=dict)
    auth: Credential | None = None
    fetcher: Fetcher = fetch
    clock: Callable[[], float] = time.monotonic

    _cached: Table | None = field(default=None, init=False, repr=False)
    _fetched_at: float = field(default=0.0, init=False, repr=False)
    # The first page, kept apart from the full table. It answers what the
    # columns are, which is all the catalog views need.
    _schema: Table | None = field(default=None, init=False, repr=False)
    _schema_at: float = field(default=0.0, init=False, repr=False)
    # The tables built from arrays inside the rows, by column name.
    _children: dict = field(default_factory=dict, init=False, repr=False)
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

    def _get(self, url: str) -> bytes:
        """Fetch one URL with the credential in place.

        Every request this source makes goes through here. A credential
        applied at each call site instead would be one forgotten on the
        pagination path or the background refresh, and a request that quietly
        drops its authentication looks exactly like an API revoking a key.
        """
        if self.auth is None:
            return self.fetcher(url, self.headers, self.timeout)
        addressed, headers = self.auth.apply(url, self.headers)
        return self.fetcher(addressed, headers, self.timeout)

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
            return urls[0], self._get(urls[0])

        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=len(urls), thread_name_prefix="pysqlbridge-mirror"
        )
        try:
            pending = {
                pool.submit(self._get, url): url
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

    def prime(self, table: Table) -> None:
        """Seed the cache with a table that has already been fetched.

        Discovery downloads every document on a surface and then builds the
        table from it. Without this the server throws all of that away and
        fetches the same URLs again a moment later at warm-up, which for a
        crawl of sixty endpoints is sixty needless requests to an API that
        just answered them.

        Seeded as though fetched now, so the usual expiry applies and the
        seeded copy is replaced on schedule like any other.
        """
        with self._lock:
            self._cached = table
            self._fetched_at = self.clock()

    def _begin_background_refresh(self) -> None:
        """Start one refresh, and only one, behind the returning query.

        Freshness is checked again inside the lock, for the same reason the
        foreground load checks it there: the refresh holds this lock for the
        whole of its fetch, so a second query arriving during one waits here
        and reaches the flag only after that refresh has cleared it. It then
        found no refresh in progress and started another, over a cache that
        had just been filled. Caught by CI, on one runner in ten, as a third
        fetch where the source promises two.
        """
        with self._lock:
            if self._refreshing or self._fresh() is not None:
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

    def _shape(self, payload: object, url: str) -> tuple[str | None, str]:
        """Where the rows are and how to read them.

        Both halves come from detection, not one: finding that the rows sit
        under "results" and then reading the whole document as an array is how
        auto-detection silently produced nothing.

        Detection is cheap enough to run on every refresh rather than being
        cached. It samples the shape instead of reading the data, and measures
        under a tenth of a millisecond on payloads where parsing the JSON took
        twenty times longer, so a source that changes shape is noticed rather
        than decoded with a stale assumption.
        """
        if self.records != "auto":
            return self.path, self.records

        if is_rejection(payload):
            raise SourceError(
                f"{url} answered with a refusal rather than data. If that is "
                f"wrong, name the shape explicitly with "
                f'"records" and "path".'
            )

        # A configured path narrows where to look; detection fills in the rest.
        subject = extract(payload, self.path, url) if self.path else payload
        shape = detect(subject)
        if shape is None:
            raise SourceError(
                f"could not work out the shape of {url}: {describe(subject)}. "
                f'Name it with "records" and "path", or use "records": "json" '
                f"to serve each element as text."
            )

        if self.path and shape.path:
            path = f"{self.path}.{shape.path}"
        else:
            path = self.path or shape.path
        return path, shape.records

    def _decode(self, url: str, raw: bytes) -> object:
        """Parse a response into lists and dicts, whatever it is made of.

        The format is sniffed from the first bytes rather than taken from the
        Content-Type header, which is wrong often enough to matter: a feed
        served as text/html and a JSON API served as text/plain are both
        common, and both would be unreadable if the header were believed.
        """
        raw = without_bom(raw)
        kind = self.format if self.format != "auto" else sniff(raw)
        if kind == "xml":
            return parse_xml(raw, url)
        if kind == "html":
            return parse_html(raw, url)
        if kind == "csv":
            return csv_records(raw, url)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SourceError(
                f"{url} did not return valid JSON: {exc}; if it serves CSV, "
                f'set "format": "csv" for this table'
            ) from exc

    def _follow(self, payload: object, url: str) -> str | None:
        """The next page's URL, if there is one.

        A link the response offers is preferred, read through the same dotted
        path machinery as the rows because APIs put it in as many places:
        PokeAPI at the top level, Rick and Morty under info. Failing that, a
        position the response reported is advanced by one page.
        """
        if self.next_key:
            try:
                nxt = extract(payload, self.next_key, self.origin)
            except SourceError:
                nxt = None   # a last page often drops the key entirely
            if isinstance(nxt, str) and nxt:
                return nxt

        return self.paging.next_url(url, payload) if self.paging else None

    def schema(self) -> Table:
        """The table's columns, without paying for the rest of its pages.

        The catalog views and the startup warm want to know what a table is
        called and what is in it. Answering that with a full read means
        following every page of every source before the server will listen,
        which for fifty paginated collections is several hundred requests and
        twenty seconds, most of it for tables nobody goes on to query.

        The first page settles the columns, so that is all this fetches. A
        query still reads the whole table: this is a cheaper answer to a
        narrower question, not a smaller version of the same one.

        The one thing it can differ on is a column type, since types are
        inferred from the values present and a later page can hold a float in
        a column whose first page was whole numbers. The query is unaffected,
        because it infers over everything it read.
        """
        full = self._fresh()
        if full is not None:
            return full

        shaped = self._schema
        if shaped is not None and self.clock() - self._schema_at < self.ttl:
            return shaped

        with self._lock:
            if self._schema is not None and (
                self.clock() - self._schema_at < self.ttl
            ):
                return self._schema
            table = self._fetch(follow=False)
            self._schema = table
            self._schema_at = self.clock()
            return table

    def _refresh(self) -> Table:
        table = self._fetch(follow=True)
        self._cached = table
        self._fetched_at = self.clock()
        # A full read answers the narrower question too, and answers it
        # better, so the cheaper copy is dropped rather than left to expire.
        self._schema = None
        self._schema_at = 0.0
        # Cleared on the way out whether this was a foreground load or the
        # background one: left set, no later expiry would ever refresh again.
        self._refreshing = False
        return table

    def _fetch(self, follow: bool) -> Table:
        url, raw = self._race()
        payload = self._decode(url, raw)
        path, strategy = self._shape(payload, url)
        records = list(locate(extract(payload, path, url), strategy, url))

        pages, more = 1, None
        if follow:
            pages, more = self._read_pages(url, payload, path, strategy, records)

        if more:
            # Stopped by the page bound with more still on offer. Silence here
            # would present part of a collection as all of it, which is the
            # one failure a person querying the table cannot detect.
            log.warning(
                "%s: stopped after %d pages with more available; "
                'raise "max_pages" to read further',
                self.name, pages,
            )

        if len(records) > self.max_rows:
            # Truncating silently would look like the API only has this many.
            raise SourceError(
                f"{self.name} reached {len(records)} rows over {pages} page(s), "
                f"past the {self.max_rows} it is allowed; narrow the request or "
                f'raise "max_rows"'
            )

        if self.expand and follow:
            # Built before the table, because an array that becomes a table of
            # its own should not also sit in the parent as JSON text: that is
            # the same data twice, and the wide one is not queryable.
            shaped = [r for r in records if isinstance(r, dict)]
            self._children = child_tables(
                self.name, shaped, identifying_column(shaped)
            )
            moved = {
                name for name in array_columns(shaped)
                if f"{self.name}_{name}".replace(".", "_") in self._children
            }
            if moved:
                records = [
                    {k: v for k, v in record.items() if k not in moved}
                    if isinstance(record, dict) else record
                    for record in records
                ]

        return from_records(
            records,
            name=self.name,
            origin=url,
            flatten=self.flatten,
            columns=self.columns,
        )

    def _read_pages(self, url, payload, path, strategy, records) -> tuple[int, bool]:
        """Read the rest of the collection into `records`.

        Serially at first, because the only way on is the link the last page
        gave. Once two consecutive links reveal which parameter is moving and
        by how much, the remaining links can be written down instead of asked
        for, and then they are fetched in parallel: the host gate still holds
        the request rate down, but the waiting overlaps rather than stacking.

        Returns how many pages were read, and whether more were left behind.
        """
        rows_per_page = len(records)
        pages = 1
        seen = records[:]
        following = self._follow(payload, url)
        stride = None

        while following and pages < self.max_pages and len(records) < self.max_rows:
            page, payload = self._page(following, path, strategy)
            if not page or page == seen:
                return pages, False
            records.extend(page)
            seen, pages = page, pages + 1

            onward = self._follow(payload, following)
            if not onward:
                return pages, False
            stride = stride_between(following, onward)
            following = onward
            if stride:
                break

        if not stride or not following:
            return pages, bool(following)

        return self._read_ahead(following, stride, path, strategy, records,
                                seen, pages, rows_per_page,
                                declared_total(payload))

    def _read_ahead(self, following, stride, path, strategy, records, seen,
                    pages, rows_per_page, total) -> tuple[int, bool]:
        """Fetch the predicted page URLs in parallel batches, in order.

        The URLs are a prediction; the end of the collection is not. Each
        answer is checked in the order it was asked for, and a page that comes
        back empty, repeated, or without a next link of its own ends the read,
        which is the API saying so rather than this guessing from a row count.

        A batch can therefore run past the end. Where the response declared a
        total, that bounds the last batch and the overshoot is nothing; where
        it did not, it is under one batch of requests, which is the price of
        not spending a round trip to find each page boundary.
        """
        parameter, step = stride
        while following and pages < self.max_pages and len(records) < self.max_rows:
            room = self.max_pages - pages
            if total is not None and rows_per_page:
                left = max(0, total - len(records))
                room = min(room, math.ceil(left / rows_per_page))
            batch, cursor = [], following
            for _ in range(min(self.page_workers, room)):
                batch.append(cursor)
                cursor = _advance(cursor, parameter, step)
            if not batch:
                return pages, False

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(batch), thread_name_prefix="pysqlbridge-page"
            ) as pool:
                answers = list(pool.map(
                    lambda u: self._page(u, path, strategy, quiet=True), batch
                ))

            for asked, (page, payload) in zip(batch, answers):
                if not page or page == seen:
                    return pages, False
                records.extend(page)
                seen, pages = page, pages + 1
                if payload is not None and not self._follow(payload, asked):
                    return pages, False
            following = cursor

        return pages, bool(following)

    def _page(self, url: str, path: str | None, strategy: str,
              quiet: bool = False) -> tuple[list, object]:
        """One page of records, plus the document they came from.

        The later pages keep the first page's reading rather than being
        detected again: a short last page can look like a different shape.

        Quiet is for a speculative read past the end of a collection, where a
        404 or a refusal is the answer rather than a fault.
        """
        try:
            payload = self._decode(url, self._get(url))
        except SourceError:
            if quiet:
                return [], None
            raise
        return list(locate(extract(payload, path, url), strategy, url)), payload

    def children(self) -> dict:
        """The tables built from arrays inside this source's rows.

        Loading first if nothing has been read yet, because the arrays are
        only known once a response has been seen.
        """
        if not self._children and self._cached is None:
            self.load()
        return dict(self._children)

    def invalidate(self) -> None:
        """Forget what was fetched, so the next load fetches again."""
        with self._lock:
            self._cached = None
            self._fetched_at = 0.0
            self._schema = None
            self._schema_at = 0.0


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

    def schema(self) -> Table:
        """The same table. A file has no pages to decline to follow."""
        return self.table

    def children(self) -> dict:
        """None. A file source was already shaped when it was read."""
        return {}


@dataclass(frozen=True)
class ChildSource:
    """One table built from an array inside another source's rows.

    Holds no data of its own: it asks its parent, which rebuilds both together
    on the same expiry, so the two can never drift apart.
    """

    parent: object
    column: str
    name: str

    def load(self) -> Table:
        found = self.parent.children().get(self.column)
        if found is None:
            raise SourceError(
                f"'{self.name}' comes from the '{self.column}' array in "
                f"'{self.parent.name}', which the last response did not carry"
            )
        return found

    def schema(self) -> Table:
        return self.load()

    def children(self) -> dict:
        return {}
