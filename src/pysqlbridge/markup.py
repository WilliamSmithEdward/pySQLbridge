"""Turning XML and HTML into the data model the rest of this already speaks.

Neither gets its own pipeline. Shape detection, flattening, type inference and
the SQL layer all work on lists and dicts, so the whole job here is producing
lists and dicts faithfully. An RSS feed and a JSON envelope with rows under
"items" become the same thing, and everything downstream stops caring which
one arrived.

XML maps cleanly. Attributes become keys, repeated sibling tags become a list,
an element with nothing but text becomes that text. The one real decision is
namespaces, which are stripped: a column called
{http://www.w3.org/2005/Atom}title cannot be typed into a query, and no Atom
feed uses two namespaces whose local names collide.

HTML is a scrape, and the honest version of a scrape is a narrow one. A page
has one thing in it that is already a table, which is a <table>, so that is
what comes out: every table on the page, keyed by its caption or its position.
Pages also carry JSON-LD, which is structured data the author published on
purpose, so that comes out too.

Both parsers are handed documents from wherever the config points, which is
not necessarily somewhere trustworthy, so both are bounded and neither
resolves anything external.
"""

from __future__ import annotations

import codecs
import html.parser
import json
import re
import xml.etree.ElementTree as ElementTree

from .source import SourceError

# Where an element puts its own text once it also has attributes or children.
TEXT_KEY = "#text"

# Attributes are prefixed so that <link href="..."> and <link><href>..</href>
# do not collide into one ambiguous column.
ATTRIBUTE_PREFIX = "@"

# Ceilings. A source is whatever a URL returned, and a parser without a bound
# turns a hostile or merely broken document into an out-of-memory server.
MAX_ELEMENTS = 500_000
MAX_TABLE_ROWS = 100_000
MAX_TABLE_COLUMNS = 512

_NAMESPACE = re.compile(r"\{[^}]*\}")
_DOCTYPE = re.compile(rb"<!DOCTYPE", re.IGNORECASE)


def _text_of(element) -> str:
    return (element.text or "").strip()


def _tag_of(element) -> str:
    return _NAMESPACE.sub("", str(element.tag))


def _element_to_data(element, budget: list) -> object:
    """One element as a value, recursively."""
    budget[0] -= 1
    if budget[0] <= 0:
        raise SourceError(
            f"the document holds more than {MAX_ELEMENTS} elements, which is "
            f"more than this will read into memory"
        )

    children = list(element)
    attributes = {
        f"{ATTRIBUTE_PREFIX}{_NAMESPACE.sub('', str(k))}": v
        for k, v in element.attrib.items()
    }
    text = _text_of(element)

    if not children and not attributes:
        return text or None

    value: dict[str, object] = dict(attributes)

    grouped: dict[str, list] = {}
    for child in children:
        grouped.setdefault(_tag_of(child), []).append(child)

    for tag, group in grouped.items():
        if len(group) == 1:
            value[tag] = _element_to_data(group[0], budget)
        else:
            # Repetition is what makes rows. One <item> is a field, several are
            # a table, and an XML document says which by repeating the tag.
            value[tag] = [_element_to_data(one, budget) for one in group]

    if text:
        value[TEXT_KEY] = text
    return value


def parse_xml(raw: bytes | str, origin: str = "the document") -> object:
    """Parse XML into lists and dicts.

    A DOCTYPE is refused rather than parsed. Internal entity definitions live
    in a DTD, and they are how a 900-byte document expands into gigabytes of
    text inside the parser. Refusing the declaration removes that outright,
    and real data XML does not carry one. External references are not resolved
    either, which ElementTree has not done by default since Python 3.7.1.
    """
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    if _DOCTYPE.search(data[:4096]):
        raise SourceError(
            f"{origin} carries a DOCTYPE, which this refuses to parse because "
            f"a DTD can define entities that expand without bound"
        )

    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise SourceError(f"{origin} is not valid XML: {exc}") from exc

    return {_tag_of(root): _element_to_data(root, [MAX_ELEMENTS])}


class _Tables(html.parser.HTMLParser):
    """Every <table> on a page, as a list of dictionaries each.

    Written against html.parser rather than a real HTML5 tree builder because
    the target is a table, and a table is the one structure a stream parser
    can follow without building a tree: rows open and close in order, and the
    header is whatever the first row or the <th> cells said.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self.captions: list[str | None] = []
        # Per table, per row: whether every cell in it was a <th>.
        self.headings: list[list[bool]] = []
        self._rows: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._caption: list[str] | None = None
        self._spans: list[int] = []
        self._headings: list[bool] = []
        self._depth = 0
        self._ignoring = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("style", "script"):
            # Wikipedia puts a <style> block inside the first cell of its
            # larger tables. Left in, the stylesheet becomes the column name.
            self._ignoring += 1
            return
        if tag == "table":
            self._depth += 1
            # A nested table is layout, not data. Its cells belong to the
            # cell of the outer table that contains it.
            if self._depth == 1:
                self._rows, self._caption = [], None
                self._headings = []
            return
        if self._rows is None:
            return
        if tag == "caption":
            self._caption = []
        elif tag == "tr":
            self._row, self._spans = [], []
            self._heading_row = True
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            self._spans.append(_span(attrs))
            if tag == "td":
                self._heading_row = False

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "script"):
            self._ignoring = max(0, self._ignoring - 1)
            return
        if tag == "table":
            self._depth -= 1
            if self._depth == 0 and self._rows is not None:
                self.tables.append(self._rows)
                self.headings.append(self._headings)
                self.captions.append(
                    " ".join("".join(self._caption).split())
                    if self._caption else None
                )
                self._rows = None
            return
        if self._rows is None:
            return
        if tag == "caption":
            pass
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            value = " ".join("".join(self._cell).split())
            # colspan repeats the value across the columns it covers, so the
            # cells after it stay under the headings they belong to.
            self._row.extend([value] * self._spans[-1])
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row and len(self._rows) < MAX_TABLE_ROWS:
                self._rows.append(self._row[:MAX_TABLE_COLUMNS])
                self._headings.append(getattr(self, "_heading_row", False))
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._ignoring:
            return
        if self._cell is not None:
            self._cell.append(data)
        elif self._caption is not None:
            self._caption.append(data)


def _span(attrs) -> int:
    for name, value in attrs:
        if name == "colspan":
            try:
                return max(1, min(int(str(value).strip()), 64))
            except (TypeError, ValueError):
                return 1
    return 1


class _Embedded(html.parser.HTMLParser):
    """JSON the page publishes about itself.

    JSON-LD in a script tag is structured data an author put there on purpose,
    which makes it better evidence about what a page is about than anything
    scraped out of its markup.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.documents: list[object] = []
        self._collecting = False
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag != "script":
            return
        kind = {name: (value or "").lower() for name, value in attrs}.get("type", "")
        self._collecting = kind in ("application/ld+json", "application/json")
        self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._collecting:
            try:
                self.documents.append(json.loads("".join(self._buffer)))
            except ValueError:
                pass
            self._collecting = False

    def handle_data(self, data: str) -> None:
        if self._collecting:
            self._buffer.append(data)


def _named(caption: str | None, position: int, taken: set[str]) -> str:
    """A key for one table on a page: its caption, or where it was."""
    stem = re.sub(r"[^0-9a-zA-Z]+", "_", (caption or "").strip()).strip("_").lower()
    stem = stem[:40].strip("_") or f"table_{position}"
    if stem[0].isdigit():
        stem = f"t_{stem}"
    candidate, n = stem, 2
    while candidate in taken:
        candidate, n = f"{stem}_{n}", n + 1
    return candidate


def parse_html(raw: bytes | str, origin: str = "the page") -> dict:
    """Parse HTML into the tables and published JSON it holds.

    The result is a map of name to rows, which is a shape detection already
    understands: a page with one table detects straight through to it, and a
    page with several needs a "path" naming which one, the same way an API
    with rows under an envelope key does.
    """
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw

    scraper = _Tables()
    try:
        scraper.feed(text)
        scraper.close()
    except AssertionError as exc:                   # html.parser on broken markup
        raise SourceError(f"{origin} could not be parsed as HTML: {exc}") from exc

    found: dict[str, object] = {}
    taken: set[str] = set()
    for position, (rows, caption, headings) in enumerate(
        zip(scraper.tables, scraper.captions, scraper.headings), start=1
    ):
        records = _rows_to_records(rows, headings)
        if not records:
            continue
        name = _named(caption, position, taken)
        taken.add(name)
        found[name] = records

    embedded = _Embedded()
    try:
        embedded.feed(text)
        embedded.close()
    except AssertionError:
        pass
    if embedded.documents:
        found["json_ld"] = (embedded.documents[0] if len(embedded.documents) == 1
                            else embedded.documents)

    if not found:
        raise SourceError(
            f"{origin} holds no table and no published JSON. This reads "
            f"<table> elements and JSON-LD, not page layout."
        )
    return found


def _rows_to_records(rows: list[list[str]], heading_rows: list[bool]) -> list[dict]:
    """A table's cells as records, using its header row for the names.

    The header is chosen from the run of <th>-only rows at the top, taking
    the one with the most distinct values. Two things sit in that run. A
    sub-header under a colspan is narrow, so the Wikipedia list of tallest
    buildings has a ten-cell row of names above a two-cell row reading m, ft.
    A spanning title is wide but says one thing, and since a colspan repeats
    its value across the columns it covers, the navbox above the list of
    chemical elements is sixteen cells of the same words. Counting distinct
    values separates the header from both. Only the leading run counts,
    because a <th> further down a table is a section divider.

    A table with no rows left after the header is dropped. Pages are full of
    one-row tables used for layout, and serving those as empty tables buries
    the real ones.
    """
    if len(rows) < 2:
        return []

    leading = 0
    while leading < len(rows) - 1 and leading < len(heading_rows)             and heading_rows[leading]:
        leading += 1

    at = (max(range(leading), key=lambda i: len({c for c in rows[i] if c}))
          if leading else 0)
    headings, body = rows[at], rows[leading or 1:]
    if not body:
        return []
    names, taken = [], set()
    for position, heading in enumerate(headings, start=1):
        stem = re.sub(r"[^0-9a-zA-Z]+", "_", heading).strip("_").lower()[:60]
        stem = stem or f"column_{position}"
        if stem[0].isdigit():
            stem = f"c_{stem}"
        while stem in taken:
            stem = f"{stem}_{position}"
        taken.add(stem)
        names.append(stem)

    return [
        {name: (row[i] if i < len(row) else None) for i, name in enumerate(names)}
        for row in body
    ]


def without_bom(raw: bytes) -> bytes:
    """These bytes without a UTF-8 byte order mark.

    Microsoft tooling writes one in front of both XML and JSON. It is
    invisible to a person reading the response and fatal to json.loads, and
    the Federal Reserve press feed serves one. The file readers already strip
    it by decoding as utf-8-sig; this is the same thing for bytes that arrived
    over the network.
    """
    return raw[len(codecs.BOM_UTF8):] if raw.startswith(codecs.BOM_UTF8) else raw


# Which of the three formats a document is, decided from its first bytes
# rather than from a Content-Type header. Headers are wrong often enough
# that a server calling an RSS feed text/html would otherwise make it
# unreadable.
def sniff(raw: bytes) -> str:
    """Whether these bytes are json, xml or html."""
    head = without_bom(raw[:1024]).lstrip()
    if head[:1] in (b"{", b"["):
        return "json"
    if head[:5].lower() == b"<?xml":
        return "xml"
    if not head.startswith(b"<"):
        return "json"

    lowered = head[:512].lower()
    if lowered.startswith(b"<!doctype html") or b"<html" in lowered:
        return "html"
    return "xml"
