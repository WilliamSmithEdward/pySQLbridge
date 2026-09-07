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

# A map needs at least this many entries before it reads as rows keyed by a
# code. {"uuid": "..."} is one row with one field, not a one-row lookup.
MIN_ENTRIES = 3

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


def looks_like_a_rejection(document: object) -> bool:
    """Whether the document is an API saying no.

    REST Countries answers a bad field list with HTTP 200 and
    {"status": 400, "message": ..., "errors": [...]}, which has every
    appearance of a table until you read it.
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


def _homogeneous(records: list) -> float:
    """Records that share their keys look like a table; ragged ones do not."""
    dicts = [r for r in records[:20] if isinstance(r, dict)]
    if len(dicts) < 2:
        return 0.0
    first = set(dicts[0])
    if not first:
        return 0.0
    overlap = sum(len(first & set(d)) / len(first | set(d)) for d in dicts[1:])
    return 3.0 * overlap / (len(dicts) - 1)


def _looks_like_codes(keys) -> bool:
    """Whether these keys identify rows rather than name fields.

    This is the difference between a table and a record, and neither the value
    types nor the key count settle it: Frankfurter returns 29 same-typed values
    under USD, GBP, SEK, and sunrise-sunset returns 10 same-typed values under
    solar_noon, day_length, civil_twilight_begin. The first is rows keyed by a
    code, the second is one row whose fields happen to all be strings.

    Codes are short, or upper case, or numeric. Field names are words, often
    joined by an underscore.
    """
    keys = [str(k) for k in keys]
    if not keys:
        return False
    wordy = sum(1 for k in keys if "_" in k or "-" in k or len(k) > 8)
    if wordy > len(keys) / 4:
        return False
    coded = sum(
        1 for k in keys
        if k.isdigit() or (k.isupper() and len(k) <= 6) or len(k) <= 4
    )
    return coded >= len(keys) * 0.8


def _looks_like_a_record(document: dict) -> bool:
    """A flat object with field-shaped keys is one row, not a map of rows."""
    scalars = [v for v in document.values() if not isinstance(v, (dict, list))]
    if not scalars:
        return False
    return not _looks_like_codes(document.keys())


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
            score = 10.0 + _volume(len(node)) + _homogeneous(sample)
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
        if len(node) >= MIN_ENTRIES and _looks_like_codes(node.keys()):
            out.append((path, "entries", 9.0 + _volume(len(node)),
                        f"a map of {len(node)} values, keyed by a code"))
        else:
            out.append((path, "single", 9.0 - 1.5 * depth,
                        f"one object with {len(node)} fields"))
    elif values and _looks_like_a_record(node):
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


def detect(document: object) -> Shape | None:
    """The best-supported reading of this document, or None if unsure."""
    if looks_like_a_rejection(document):
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
        return None
    return best


def describe(document: object) -> str:
    """A sentence about what was detected, for an error or a log line."""
    shape = detect(document)
    if shape is None:
        return "the shape could not be worked out"
    where = f"'{shape.path}'" if shape.path else "the response"
    return f"{where} is {shape.why}, read as records={shape.records}"
