"""Working out an API's shape from its response.

The goal is a table that needs no configuration beyond a URL. The danger is
that a document almost always contains *something* array-shaped, so a detector
that simply looks for one is confidently wrong a lot. Measured over 85 public
endpoints, naive detection found a shape for 84 of them and picked:

    restcountries          the "errors" array of a rejected request
    catfact.ninja          "links", the paging metadata, instead of "data"
    pokeapi/pokemon/pikachu  "moves", a nested array, instead of the record
    frankfurter/latest     "single", when a map of rates is one row per currency

So candidates are scored rather than found, and a low-scoring best answer is
refused instead of served. Being told the shape could not be worked out is
recoverable; being served an error envelope as data is not.

Three signals do most of the work: what the key is called, how much is under
it, and how deep it sits.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Keys that hold the payload. Drawn from the survey rather than invented.
ROW_KEYS = frozenset({
    "data", "results", "items", "records", "rows", "docs", "features",
    "content", "entries", "list", "products", "meals", "drinks", "cards",
    "people", "verses", "jokes", "departments", "objects", "hits", "edges",
    "nodes", "articles", "photos", "observations", "launches", "collection",
    "elements", "values", "children", "members", "breweries", "countries",
})

# Keys that hold something about the payload, or about a failure. Never rows.
META_KEYS = frozenset({
    "links", "_links", "meta", "metadata", "pagination", "paging", "info",
    "error", "errors", "status", "warnings", "messages", "debug", "self",
    "next", "previous", "prev", "first", "last", "page", "pages", "count",
    "total", "limit", "offset", "cursor", "href", "url", "_meta", "extra",
    "copyright", "license", "attribution", "generated", "took", "timing",
})

# Below this the answer is a guess rather than a reading, and a guess that
# serves an error envelope as data is worse than admitting defeat.
MINIMUM_SCORE = 6.0

# A map holding exactly one object is a wrapper around a record rather than a
# table of one: {"slip": {...}} is one piece of advice. Two is already a table.
WRAPPER_THRESHOLD = 2

# How many keys must agree before their agreement is evidence of anything.
#
# Two keys sharing a shape is a coincidence that happens constantly: over 116
# public API responses there were 44 distinct two-key objects, of which 11
# had keys of one shape and none of those were rows. lat and lng are three
# lower-case letters each; so are sha and url, svg and png, and eight more.
# From three keys up the same corpus had 7 agreements and every one was rows,
# with no record of three or more keys agreeing by chance.
MIN_KEYS_TO_AGREE = 3

MAX_DEPTH = 3

# Discovery reads the document's shape, not its contents, so it looks at a
# sample. A response with 100,000 rows has the same shape as its first fifty,
# and walking all of it to learn that costs more than the fetch did.
SAMPLE = 50

# A ceiling on how many nodes the walk visits, so a pathological document
# cannot make discovery cost more than the query it precedes.
MAX_NODES = 2_000

# Keys that say the response is a rejection. An API answering HTTP 200 with one
# of these is still refusing, and serving it as a table would present the
# refusal as data.
FAILURE_KEYS = frozenset({"error", "errors", "fault", "exception"})


@dataclass(frozen=True)
class Shape:
    """What to put in a configuration to serve this document."""

    path: str | None
    records: str
    score: float
    why: str

    def as_config(self) -> dict:
        spec: dict[str, object] = {}
        if self.path:
            spec["path"] = self.path
        if self.records != "array":
            spec["records"] = self.records
        return spec


def is_rejection(document: object) -> bool:
    """Whether the document is an API refusing rather than answering.

    Decided on what the document states about itself, not on its shape: a
    failure key carrying something, a status at or above 400, or success
    stated as false. REST Countries answers a bad field list with HTTP 200
    and {"status": 400, "message": ..., "errors": [...]}, and serving that as
    a table presents the refusal as data.
    """
    if not isinstance(document, dict):
        return False

    lowered = {str(k).lower(): v for k, v in document.items()}
    if not (FAILURE_KEYS & set(lowered)):
        return False

    for key in FAILURE_KEYS & set(lowered):
        value = lowered[key]
        if value not in (None, "", [], {}, False):
            return True

    status = lowered.get("status") or lowered.get("code")
    if isinstance(status, int) and status >= 400:
        return True
    return lowered.get("success") is False


def _tail(path: str | None) -> str:
    return (path or "").rsplit(".", 1)[-1].lower()


def _is_meta(path: str | None) -> bool:
    """Whether any segment of a path names metadata rather than data."""
    if not path:
        return False
    return any(part.lower() in META_KEYS for part in path.split("."))


def _volume(count: int) -> float:
    """More rows is better evidence, with diminishing returns."""
    return min(5.0, math.log2(count + 1))


def _shared_keys(records: list) -> float:
    """How much of their key set the records have in common.

    The mean Jaccard overlap of the first record's keys with each of the
    others, scaled. Rows of one table share a key set; a list of unrelated
    objects does not.
    """
    dicts = [r for r in records[:20] if isinstance(r, dict)]
    if len(dicts) < 2:
        return 0.0
    first = set(dicts[0])
    if not first:
        return 0.0
    overlap = sum(len(first & set(d)) / len(first | set(d)) for d in dicts[1:])
    return 3.0 * overlap / (len(dicts) - 1)


def _shape_of(key: str) -> tuple:
    """A key reduced to its length and the run of character classes in it.

    USD becomes (3, ("upper",)) and so do GBP and SEK. solar_noon becomes
    (10, ("lower", "other", "lower")) and day_length (10, the same runs), but
    civil_twilight_begin is longer and sunrise has no separator at all.
    """
    runs: list[str] = []
    for character in key:
        if character.isupper():
            kind = "upper"
        elif character.islower():
            kind = "lower"
        elif character.isdigit():
            kind = "digit"
        else:
            kind = "other"
        if not runs or runs[-1] != kind:
            runs.append(kind)
    return len(key), tuple(runs)


def keys_identify_rows(keys) -> bool:
    """Whether these keys are values from a domain rather than field names.

    Frankfurter answers with 29 numbers under USD, GBP and SEK; sunrise-sunset
    answers with 10 strings under sunrise, solar_noon and day_length. Both are
    a map of same-typed scalars, so neither the value types nor the number of
    keys separates them. What separates them is where the keys came from.

    A key that identifies a row was produced by whatever produces that domain,
    so every key in the map shares one shape. A key that names a field was
    chosen by a person writing a schema, so the keys share nothing but being
    words. That is the whole rule, and it is the only thing this asks.

    Agreement only counts once there are enough keys for it to be unlikely.
    Two keys of one shape is a coincidence that happens constantly, and the
    minimum is set from that; see MIN_KEYS_TO_AGREE.

    It cannot separate a record whose field names happen to share a shape,
    such as name, city and team, from a lookup keyed by four-letter codes. No
    rule reading one document can: the two are identical in every respect a
    document carries.
    """
    keys = [str(k) for k in keys]
    if len(keys) < MIN_KEYS_TO_AGREE:
        return False
    return len({_shape_of(key) for key in keys}) == 1


def _is_single_record(document: dict) -> bool:
    """Whether this object is one row rather than a map of them."""
    scalars = [v for v in document.values() if not isinstance(v, (dict, list))]
    if not scalars:
        return False
    return not keys_identify_rows(document.keys())


def _candidates(node: object, path: str | None, depth: int, out: list,
                budget: list) -> None:
    if depth > MAX_DEPTH or budget[0] <= 0:
        return
    budget[0] -= 1

    if isinstance(node, list):
        if not node:
            return
        # A sample decides the shape. Scanning a hundred thousand rows to learn
        # they are all objects costs more than the fetch that produced them.
        sample = node[:SAMPLE]
        if all(isinstance(x, dict) for x in sample):
            score = 10.0 + _volume(len(node)) + _shared_keys(sample)
            out.append((path, "array", score, f"a list of {len(node)} objects"))
        elif all(not isinstance(x, (dict, list)) for x in sample):
            out.append((path, "scalars", 5.0 + _volume(len(node)),
                        f"a list of {len(node)} plain values"))
        elif len(node) == 2 and isinstance(node[1], list):
            # The World Bank shape: a metadata object, then the rows.
            inner = f"{path}.1" if path else "1"
            _candidates(node[1], inner, depth + 1, out, budget)
        return

    if not isinstance(node, dict) or not node:
        return

    arrays = {k: v for k, v in node.items() if isinstance(v, list) and v}

    # Parallel arrays of equal length are columns, not rows.
    columnar = {k: v for k, v in arrays.items()
                if not isinstance(v[0], (dict, list))}
    if len(columnar) >= 2 and len({len(v) for v in columnar.values()}) == 1:
        rows = len(next(iter(columnar.values())))
        out.append((path, "columns", 9.0 + _volume(rows),
                    f"{len(columnar)} arrays of {rows}, one per column"))

    values = list(node.values())

    if values and all(isinstance(v, dict) for v in values):
        # A map of a couple of objects is usually a wrapper, and the record
        # inside it is the better answer, so it is scored down rather than out.
        wrapper = len(node) < WRAPPER_THRESHOLD
        out.append((path, "values", (2.0 if wrapper else 8.0) + _volume(len(node)),
                    f"a map of {len(node)} objects"))
    elif values and all(not isinstance(v, (dict, list)) for v in values):
        if keys_identify_rows(node.keys()):
            out.append((path, "entries", 9.0 + _volume(len(node)),
                        f"a map of {len(node)} values, keyed by a code"))
        else:
            out.append((path, "single", 9.0 - 1.5 * depth,
                        f"one object with {len(node)} fields"))
    elif values and _is_single_record(node):
        # A record with a mixture of scalars and nested parts. Shallow ones
        # score better: a record three levels down is usually a fragment of
        # something else rather than the thing being served.
        scalar_share = sum(
            1 for v in values if not isinstance(v, (dict, list))
        ) / len(values)
        out.append((path, "single", 8.0 - 1.5 * depth + 2.0 * scalar_share,
                    f"one object with {len(node)} fields"))

    for key, value in node.items():
        if budget[0] <= 0:
            return
        inner = f"{path}.{key}" if path else key
        _candidates(value, inner, depth + 1, out, budget)


def _empty(document: object) -> Shape | None:
    """The reading for a response that is a list with nothing in it.

    A search that matched nothing is still a table, and refusing it makes a
    source work only on the days its query has results: GitHub answers
    /releases with a bare [] for a project that publishes tags instead. What
    comes back has no columns, so serving it needs them named, and that error
    says so; "the shape could not be worked out" does not.

    Only where the empty list is the whole response. An empty list inside an
    envelope cannot be told apart from a record with an empty field: a repository
    with no topics and a search with no results are both one key holding [], and
    reading the second as rows would serve nothing for the first.
    """
    if isinstance(document, list) and not document:
        return Shape(path=None, records="array", score=MINIMUM_SCORE,
                     why="a list of nothing")
    return None


def detect(document: object) -> Shape | None:
    """The best-supported reading of this document, or None if unsure."""
    if is_rejection(document):
        return None

    found: list = []
    _candidates(document, None, 0, found, [MAX_NODES])

    best: Shape | None = None
    for path, records, score, why in found:
        if _is_meta(path):
            # Never. This is the rule that stops a rejected request being
            # served as a table of its own error messages.
            continue

        adjusted = score
        tail = _tail(path)
        if tail in ROW_KEYS:
            adjusted += 8.0
        adjusted -= 1.0 * (path.count(".") + 1 if path else 0)

        if best is None or adjusted > best.score:
            best = Shape(path=path, records=records, score=adjusted, why=why)

    if best is None or best.score < MINIMUM_SCORE:
        return _empty(document)
    return best


def describe(document: object) -> str:
    """A sentence about what was detected, for an error or a log line."""
    shape = detect(document)
    if shape is None:
        return "the shape could not be worked out"
    where = f"'{shape.path}'" if shape.path else "the response"
    return f"{where} is {shape.why}, read as records={shape.records}"
