"""Finding out what an API serves, from its base URL alone.

The target is a person typing one URL and getting a schema full of tables.
That means answering a question the API was never asked directly: what is
here? Three routes answer it, tried in order of how much they can be trusted.

A description document, if the API publishes one. OpenAPI at one of the
conventional locations names every path and every method, which is the answer
rather than an inference about it. Costs a handful of requests to look for.

A link index, if the base returns one. Plenty of APIs answer their own root
with a map of name to URL, which is a description document by another name.
The PokeAPI root and every HAL response work this way.

A crawl, otherwise. Fetch the base, take the same-origin URLs out of what
comes back, fetch those, repeat. This is the route that works on an API that
documents nothing, and it is the one that needs bounding: an unbounded walk of
somebody else's server is an outage with good intentions. Depth, request
count, and concurrency are all capped, one URL per path is enough, and a
collection of 500 records contributes the links from its first record rather
than from all 500.

Each response found is put to detect(), which decides whether it is a table
and how to read it. Responses that are not tables are still useful: they carry
links onward.
"""

from __future__ import annotations

import concurrent.futures
import re
import threading
import urllib.parse
from dataclasses import dataclass, field

from .credentials import Credential
from .detect import Shape, detect, is_rejection
from .http_source import (
    DEFAULT_TIMEOUT_SECONDS,
    Fetcher,
    Paging,
    declared_total,
    fetch,
)
from .source import SourceError

# Where APIs put a description of themselves. Every one is a real convention:
# the first two are what most hand-rolled services use, v3/api-docs is what
# springdoc serves, swagger/v1/swagger.json is what ASP.NET generates, and the
# .well-known path is RFC 8615 registered.
#
# Split in two because looking costs requests. The first wave finds the great
# majority; the second only runs when the first came back empty, so an API
# that publishes nothing is asked four times rather than twelve.
DESCRIPTION_PATHS = ("openapi.json", "swagger.json")
MORE_DESCRIPTION_PATHS = (
    "v3/api-docs", "swagger/v1/swagger.json", ".well-known/openapi.json",
    "api-docs",
)

# Names to try when an API describes itself nowhere and its base returns
# nothing to follow. Taken from the collections actually served by the public
# APIs this was developed against, not invented: dummyjson, the SpaceX API and
# TVmaze between them account for most of this list, and all three have no
# index and no description document.
COMMON_COLLECTIONS = (
    "users", "products", "orders", "posts", "comments", "items", "customers",
    "events", "articles", "categories", "tags", "todos", "tasks", "projects",
    "teams", "accounts", "transactions", "invoices", "messages", "files",
    "books", "movies", "shows", "songs", "albums", "artists", "countries",
    "cities", "companies", "jobs", "recipes", "quotes", "carts", "people",
    "places", "locations", "characters", "episodes", "series", "games",
    "launches", "rockets", "ships", "crew", "capsules", "payloads", "stats",
    "reports",
)

# Bounds on a walk of a server that never agreed to be walked. The ceiling is
# set by the guessing route, which is the only one that spends requests on
# names that are probably not there; an API with an index or a description
# finishes in a fraction of it.
DEFAULT_MAX_REQUESTS = 120
DEFAULT_MAX_DEPTH = 3
DEFAULT_CONCURRENCY = 8

# Link names that move within one resource rather than to another one.
# Following them turns discovery into pagination and finds page 2 of users
# under the name "next".
NAVIGATION_KEYS = frozenset({
    "self", "next", "previous", "prev", "first", "last", "related", "up",
    "parent", "canonical", "alternate", "page", "href",
})

# Where an API puts the link to the next page. Drawn from the surveyed
# endpoints: the bare key is what Django REST Framework and the PokeAPI use,
# info.next is Rick and Morty, links.next is JSON:API and Laravel, and
# _links.next.href is HAL. Read through the same dotted-path machinery as the
# rows, so a nested one costs nothing extra.
NEXT_KEYS = (
    "next", "next_page_url", "nextPageUrl", "info.next", "links.next",
    "_links.next.href", "paging.next", "pagination.next_url", "next_url",
)

# Where a response reports its own position when it offers no next link.
# Every one of these was read off a real envelope: dummyjson answers with skip
# and limit, GBIF with offset and limit, Algolia with page. They are paired
# rather than listed because an offset means nothing without the step, and the
# step is what the response called its page size.
OFFSET_KEYS = ("skip", "offset", "start", "startIndex", "start_index", "from")
LIMIT_KEYS = ("limit", "per_page", "perPage", "pageSize", "page_size",
              "hitsPerPage", "page_size_limit")
PAGE_KEYS = ("page", "pageNumber", "page_number", "current_page", "currentPage",
             "pageIndex")

# Envelopes that nest their paging rather than putting it at the top level.
PAGING_PARENTS = ("", "pagination", "paging", "meta", "info", "page_info",
                  "pageInfo", "_meta")

# A path segment that identifies one record rather than naming a collection.
_IDENTIFIER = re.compile(
    r"\A(?:\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|[0-9a-f]{24,})\Z",
    re.IGNORECASE,
)

# A path or URL with a parameter in it cannot be fetched without a value.
_TEMPLATED = re.compile(r"[{}]")

# Only these are worth fetching. A crawler that follows anything string-shaped
# ends up downloading images.
_FETCHABLE = ("http://", "https://")

_UNINTERESTING_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".css", ".js",
    ".zip", ".gz", ".pdf", ".mp3", ".mp4", ".woff", ".woff2", ".ttf",
)


@dataclass(frozen=True)
class Resource:
    """One endpoint that turned out to hold a table."""

    name: str
    url: str
    shape: Shape
    rows: int
    columns: list[str]
    found_by: str
    next_key: str | None = None
    paging: Paging | None = None
    total: int | None = None
    table: object = field(default=None, repr=False, compare=False)

    @property
    def truncated(self) -> bool:
        """Whether the API holds more rows than can be reached.

        A collection that declares 1302 records, serves 20 of them, and offers
        no next link is a table that will quietly answer with page one
        forever. Saying so is the difference between a limitation and a lie.
        """
        return (self.total is not None and self.total > self.rows
                and self.next_key is None and self.paging is None)

    def as_config(self) -> dict:
        """The configuration entry that would serve this table.

        In the shape the loader reads, so that what discovery writes can be
        saved, edited and loaded back. A generated config nobody can reload is
        a report, not a configuration.

        Written out even though the same shape would be detected again at
        query time, because a person who wants to pin a table down, rename it,
        or restrict its columns needs somewhere to start.
        """
        spec: dict[str, object] = {"url": self.url}
        spec.update(self.shape.as_config())
        if self.next_key:
            spec["next"] = self.next_key
        if self.paging:
            spec["paging"] = {"key": self.paging.key,
                              "parameter": self.paging.parameter,
                              "step": self.paging.step}
        return {"name": self.name, "http": spec}


@dataclass
class Survey:
    """What a crawl found, and what it cost."""

    base: str
    resources: list[Resource] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    requests: int = 0
    description: str | None = None

    def as_config(self) -> dict:
        """Everything found, as a configuration file that can be loaded."""
        return {"tables": [r.as_config() for r in self.resources]}

    @property
    def truncated(self) -> list[Resource]:
        """Tables that hold less than the API says exists, with no way on."""
        return [r for r in self.resources if r.truncated]

    def summary(self) -> str:
        found = ", ".join(r.name for r in self.resources) or "nothing"
        via = f" via {self.description}" if self.description else ""
        short = self.truncated
        warning = (f"; {len(short)} serve only a first page: "
                   f"{', '.join(r.name for r in short)}" if short else "")
        return (f"{len(self.resources)} tables from {self.base}{via} "
                f"in {self.requests} requests: {found}{warning}")


def _normalise(url: str) -> str:
    """A URL reduced to what makes it a distinct resource.

    The fragment never reaches the server, and a trailing slash is the same
    resource. Query strings are kept: /search?q=a and /search?q=b are two
    things, however similar they look.
    """
    parts = urllib.parse.urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, parts.query, "")
    )


def _within(url: str, base: str) -> bool:
    """Whether a URL is inside the surface the base named.

    Pointing at /api/v1 is a statement about scope. A link from there to the
    marketing site is not part of the API, and following it is how a crawl
    that was meant to find six tables ends up reading a blog.
    """
    here, there = urllib.parse.urlsplit(url), urllib.parse.urlsplit(base)
    if here.netloc.lower() != there.netloc.lower():
        return False
    prefix = there.path.rstrip("/")
    return not prefix or here.path == prefix or here.path.startswith(prefix + "/")


def _worth_fetching(url: str) -> bool:
    """Whether this string is a URL a crawler should ask for.

    URI templates are the reason this is not just a scheme check. The GitHub
    root advertises https://api.github.com/repos/{owner}/{repo}, which is an
    invitation to substitute rather than an address; fetching it literally
    asks for a repository owned by "{owner}".
    """
    if not url.lower().startswith(_FETCHABLE):
        return False
    if _TEMPLATED.search(url):
        return False
    path = urllib.parse.urlsplit(url).path.lower()
    return not path.endswith(_UNINTERESTING_SUFFIXES)


def _name_for(url: str, base: str, taken: set[str]) -> str:
    """A table name from the part of the path below the base.

    /api/v1/private/audit under /api/v1 becomes private_audit. The base is
    stripped because every table would otherwise start with the same three
    segments, and a schema of api_v1_users, api_v1_orders is unusable in the
    place these names are actually read, a table picker.
    """
    here = urllib.parse.urlsplit(url)
    prefix = urllib.parse.urlsplit(base).path.rstrip("/")
    trailing = here.path[len(prefix):] if here.path.startswith(prefix) else here.path
    parts = [p for p in trailing.strip("/").split("/") if p]
    if not parts:
        parts = [p for p in here.path.strip("/").split("/") if p] or [here.netloc]

    stem = re.sub(r"[^0-9a-zA-Z]+", "_", "_".join(parts)).strip("_").lower()
    stem = stem or "resource"
    if stem[0].isdigit():
        stem = f"t_{stem}"

    candidate, n = stem, 2
    while candidate in taken:
        candidate, n = f"{stem}_{n}", n + 1
    return candidate


def _is_index(document: object, base: str) -> bool:
    """Whether a document is a map of links rather than a row of data.

    Detection would happily call {"users": "http://.../users", ...} a single
    row with eight string columns. It is technically right and completely
    useless: those are the tables, not a table.
    """
    if not isinstance(document, dict) or not document:
        return False
    strings = [v for v in document.values() if isinstance(v, str)]
    if len(strings) < max(2, len(document) * 0.6):
        return False
    # Templated URLs count here even though they are never fetched. The GitHub
    # root is 30 of them, and reading it as a one-row table of 30 string
    # columns is the single least useful thing to do with an API index.
    linked = sum(
        1 for v in strings
        if v.lower().startswith(_FETCHABLE) and _within(v, base)
    )
    return linked >= max(2, len(strings) * 0.6)


def _links_from(node: object, base: str, out: list[str], depth: int = 0) -> None:
    """Every same-origin URL worth following out of one document.

    Structure is followed and data is not. A list of objects is rows, and the
    URLs inside rows address individual records: crawling them turns a table
    of 20 characters into 20 more one-row tables named character_1 through
    character_20, which is noise wearing the shape of discovery. Links in the
    envelope around the rows still count, because that is where an API puts
    the way on to its other collections.
    """
    if depth > 4 or len(out) > 200:
        return

    if isinstance(node, list):
        if any(isinstance(item, dict) for item in node[:4]):
            return
        return

    if not isinstance(node, dict):
        return

    for key, value in node.items():
        if isinstance(value, str):
            if (str(key).lower() not in NAVIGATION_KEYS
                    and _worth_fetching(value) and _within(value, base)):
                out.append(value)
        elif isinstance(value, dict) and str(key).lower() in ("_links", "links"):
            for name, target in value.items():
                if str(name).lower() in NAVIGATION_KEYS:
                    continue
                href = target.get("href") if isinstance(target, dict) else target
                if (isinstance(href, str) and _worth_fetching(href)
                        and _within(href, base)):
                    out.append(href)
        else:
            _links_from(value, base, out, depth + 1)


def _paths_from_description(document: object, base: str) -> list[str]:
    """The GET-able collection URLs an OpenAPI document names.

    Templated paths are dropped: /users/{id} needs an id, and inventing one to
    see what happens is a request nobody asked for. Their collection parent is
    almost always listed alongside anyway.
    """
    if not isinstance(document, dict):
        return []
    paths = document.get("paths")
    if not isinstance(paths, dict):
        return []

    servers = document.get("servers")
    root = ""
    if isinstance(servers, list) and servers and isinstance(servers[0], dict):
        root = str(servers[0].get("url", ""))
    if not root:
        parts = urllib.parse.urlsplit(base)
        root = f"{parts.scheme}://{parts.netloc}"

    found: list[str] = []
    for path, operations in paths.items():
        if _TEMPLATED.search(str(path)):
            continue
        if isinstance(operations, dict) and "get" not in {
            str(k).lower() for k in operations
        }:
            continue
        found.append(urllib.parse.urljoin(root.rstrip("/") + "/", str(path).lstrip("/")))
    return found


class Crawler:
    """A bounded, parallel walk of one API surface."""

    def __init__(
        self,
        base: str,
        *,
        auth: Credential | None = None,
        headers: dict[str, str] | None = None,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        max_depth: int = DEFAULT_MAX_DEPTH,
        concurrency: int = DEFAULT_CONCURRENCY,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        guess: bool = True,
        fetcher: Fetcher = fetch,
    ) -> None:
        self.base = base.rstrip("/")
        self.auth = auth
        self.headers = dict(headers or {})
        self.max_requests = max_requests
        self.max_depth = max_depth
        self.concurrency = max(1, concurrency)
        self.timeout = timeout
        self.guess = guess
        self.fetcher = fetcher

        self._seen: set[str] = set()
        self._lock = threading.Lock()
        self.survey = Survey(base=self.base)

    # -- fetching ----------------------------------------------------------

    def _claim(self, url: str) -> bool:
        """Reserve a request, or refuse because the budget is spent.

        Claiming and counting under one lock, before the request goes out, is
        what keeps the cap real: eight threads that each check the budget and
        then fetch will overshoot it by up to eight.
        """
        with self._lock:
            key = _normalise(url)
            if key in self._seen or self.survey.requests >= self.max_requests:
                return False
            self._seen.add(key)
            self.survey.requests += 1
            return True

    def _read(self, url: str, quiet: bool = False) -> object | None:
        """Fetch and parse one URL, or None with the reason recorded.

        Quiet is for looking rather than following. Asking six conventional
        locations whether a description document is there produces five 404s
        that mean nothing went wrong, and reporting them as skipped endpoints
        buries the one failure that matters.
        """
        import json

        addressed, headers = url, self.headers
        if self.auth is not None:
            addressed, headers = self.auth.apply(url, self.headers)
        try:
            raw = self.fetcher(addressed, headers, self.timeout)
        except SourceError as exc:
            if not quiet:
                self.survey.skipped[url] = str(exc)
            return None
        except Exception as exc:                    # noqa: BLE001
            if not quiet:
                self.survey.skipped[url] = f"{type(exc).__name__}: {exc}"
            return None

        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            if not quiet:
                self.survey.skipped[url] = "not JSON"
            return None

    def _read_many(self, urls: list[str],
                   quiet: bool = False) -> list[tuple[str, object]]:
        """Fetch a whole level at once.

        Discovery is dominated by latency, not by work: sixty sequential
        requests at 200ms is twelve seconds of waiting for a schema a person
        is watching a connection dialog for.
        """
        wanted = [u for u in urls if self._claim(u)]
        if not wanted:
            return []
        if len(wanted) == 1:
            document = self._read(wanted[0], quiet)
            return [(wanted[0], document)] if document is not None else []

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self.concurrency, len(wanted)),
            thread_name_prefix="pysqlbridge-crawl",
        ) as pool:
            answers = list(pool.map(lambda u: self._read(u, quiet), wanted))
        return [(u, d) for u, d in zip(wanted, answers) if d is not None]

    # -- the three routes --------------------------------------------------

    def _describe(self) -> list[str]:
        """Ask the conventional locations for a description of the surface."""
        parts = urllib.parse.urlsplit(self.base)
        origin = f"{parts.scheme}://{parts.netloc}"

        # Looked for under the base first. A description sitting at the
        # origin describes the whole server, so its paths are cut back to the
        # surface that was actually asked for; one found under the base is
        # describing that base and is taken at its word, whatever server URL
        # it names.
        for names in (DESCRIPTION_PATHS, MORE_DESCRIPTION_PATHS):
            seen: set[str] = set()
            wave = [
                url
                for root in (self.base, origin)
                for url in (f"{root}/{name}" for name in names)
                if not (_normalise(url) in seen or seen.add(_normalise(url)))
            ]
            for url, document in self._read_many(wave, quiet=True):
                paths = _paths_from_description(document, self.base)
                if not paths:
                    continue
                within = [p for p in paths if _within(p, self.base)]
                if not within and not url.startswith(self.base + "/"):
                    continue
                self.survey.description = url
                return within or paths
        return []

    # -- the walk ----------------------------------------------------------

    def run(self) -> Survey:
        """Walk the surface and return what is on it."""
        found: dict[str, tuple[object, str]] = {}

        described = self._describe()
        if described:
            for url, document in self._read_many(described):
                found[url] = (document, "the description document")

        # The crawl runs regardless. A description document is authoritative
        # about what it lists and silent about what it omits, and a stale one
        # is common enough that treating it as the whole surface loses tables
        # that are plainly reachable from the base.
        frontier = [self.base]
        depth = 0
        while frontier and depth <= self.max_depth:
            # What the base itself served is "the base"; what it pointed at is
            # "the index"; anything further out was reached by following a
            # link from something else.
            route = ("the base", "the index")[min(depth, 1)] if depth < 2 else "a link"
            harvested: list[str] = []
            for url, document in self._read_many(frontier):
                if _is_index(document, self.base):
                    _links_from(document, self.base, harvested)
                    continue
                found[url] = (document, route)
                _links_from(document, self.base, harvested)

            frontier = self._unvisited(harvested)
            depth += 1

        if not found and self.guess:
            found.update(self._guess())

        self._collect(found)
        return self.survey

    def _guess(self) -> dict[str, tuple[object, str]]:
        """Ask for conventional collection names, as a last resort.

        An API that publishes no description and answers its base with a 404
        has told a crawler nothing, and plenty of real ones are like that. The
        remaining move is to ask for the names such an API probably uses.

        This is the impolite route and it runs last, only when the other two
        found nothing at all, so an API that describes itself is never subject
        to it. The misses are 404s, which cost the server less than the one
        page of an index would have.
        """
        wanted = [f"{self.base}/{name}" for name in COMMON_COLLECTIONS]
        found: dict[str, tuple[object, str]] = {}
        for url, document in self._read_many(wanted, quiet=True):
            if not _is_index(document, self.base):
                found[url] = (document, "a guessed name")
        return found

    def _unvisited(self, urls: list[str]) -> list[str]:
        """The URLs not already claimed, collections before single records."""
        with self._lock:
            fresh, seen = [], set()
            for url in urls:
                key = _normalise(url)
                if key in self._seen or key in seen:
                    continue
                seen.add(key)
                fresh.append(url)
        fresh.sort(key=lambda u: (_is_record_url(u), len(u)))
        return fresh

    def _collect(self, found: dict[str, tuple[object, str]]) -> None:
        """Turn the documents that hold tables into resources."""
        from .http_source import extract, locate
        from .source import from_records

        taken: set[str] = set()
        for url in sorted(found):
            document, route = found[url]
            if is_rejection(document):
                self.survey.skipped[url] = "the response is a rejection"
                continue
            shape = detect(document)
            if shape is None:
                self.survey.skipped[url] = "no table-shaped reading"
                continue

            name = _name_for(url, self.base, taken)
            try:
                records = list(locate(extract(document, shape.path, url),
                                      shape.records, url))
                table = from_records(records, name=name, origin=url)
            except SourceError as exc:
                self.survey.skipped[url] = str(exc)
                continue

            taken.add(name)
            following = _next_key(document, url)
            stepping = None if following else _paging(document)
            self.survey.resources.append(
                Resource(
                    name=name, url=url, shape=shape, rows=len(table.rows),
                    columns=list(table.column_names), found_by=route,
                    next_key=following, paging=stepping,
                    total=declared_total(document),
                    # The crawl already paid for this document. Handing the
                    # table over lets the server skip refetching every source
                    # it just discovered. A paginated one is held back: what
                    # was fetched is page one, and page one is not the table.
                    table=(table if following is None and stepping is None
                           else None),
                )
            )


def _next_key(document: object, url: str) -> str | None:
    """The path to this response's next-page link, if it offers one.

    Confirmed against the document rather than assumed from the key being
    present: the value has to be a URL on the same host. A "next" holding a
    cursor token cannot be followed by fetching it, and following a "next"
    that points somewhere else is not pagination.
    """
    from .http_source import extract

    for key in NEXT_KEYS:
        try:
            value = extract(document, key, url)
        except SourceError:
            continue
        if not isinstance(value, str) or not value:
            continue
        if _worth_fetching(value) and _same_host(value, url):
            return key
    return None


def _paging(document: object) -> Paging | None:
    """How to advance this response by a page, from what it says about itself.

    Only used when no next link was offered. The response has to report both
    a position and, for an offset, the size of a page: an offset with no step
    cannot be advanced, and inventing a step would be guessing at how much the
    API serves rather than reading it.

    The query parameter is taken to be the name the response used. That holds
    because an API reporting skip and limit is echoing back the parameters it
    was given. Where it does not hold, the second page comes back identical to
    the first and the fetch stops there rather than repeating it.
    """
    if not isinstance(document, dict):
        return None

    from .http_source import extract

    def number(parent: str, name: str) -> int | None:
        path = f"{parent}.{name}" if parent else name
        try:
            value = extract(document, path, "")
        except SourceError:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    for parent in PAGING_PARENTS:
        for offset_key in OFFSET_KEYS:
            if number(parent, offset_key) is None:
                continue
            for limit_key in LIMIT_KEYS:
                step = number(parent, limit_key)
                if step:
                    return Paging(
                        key=f"{parent}.{offset_key}" if parent else offset_key,
                        parameter=offset_key,
                        step=step,
                    )

        for page_key in PAGE_KEYS:
            if number(parent, page_key) is not None:
                return Paging(
                    key=f"{parent}.{page_key}" if parent else page_key,
                    parameter=page_key,
                    step=1,
                )
    return None


def _same_host(url: str, other: str) -> bool:
    return (urllib.parse.urlsplit(url).netloc.lower()
            == urllib.parse.urlsplit(other).netloc.lower())


def _is_record_url(url: str) -> bool:
    """Whether a URL addresses one record rather than a collection."""
    parts = [p for p in urllib.parse.urlsplit(url).path.split("/") if p]
    return bool(parts) and bool(_IDENTIFIER.match(parts[-1]))


def survey(base: str, **options) -> Survey:
    """Discover what an API serves. The whole module in one call."""
    return Crawler(base, **options).run()
