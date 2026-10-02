"""What each fuzz target feeds its parser, and what the parser may raise.

Every target takes raw bytes. A parser has to either succeed or raise the
one error it documents for bad input: TdsProtocolError for the wire
protocol, SqlError for SQL text, SourceError for a document a source
fetched. Anything else escaping, a hang or a crash, is a finding.

fuzz/fuzz_parsers.py runs these under Atheris, and test_fuzz_corpus.py
replays every seed in tests/fuzz_corpus through them on every CI run, so a
finding that became a seed stays fixed. They live with the tests rather
than in fuzz/ because the sdist ships the tests and not the fuzzer.
"""

from __future__ import annotations

from pysqlbridge.detect import detect
from pysqlbridge.http_source import extract, locate
from pysqlbridge.markup import parse_html, parse_xml, sniff
from pysqlbridge.source import SourceError, csv_records, from_records
from pysqlbridge.sql import SqlError, loose_exits, malformed, parse_select, statements
from pysqlbridge.tds import TdsProtocolError, reassemble
from pysqlbridge.tds.batch import parse_sql_batch
from pysqlbridge.tds.login import Login7, deobfuscate_password
from pysqlbridge.tds.packet import TDS_71, TDS_74
from pysqlbridge.tds.prelogin import Prelogin
from pysqlbridge.tds.rpc import parse_rpc

ORIGIN = "the fuzzed document"


def packet(data: bytes) -> None:
    """Packet headers and message reassembly, which see every byte first."""
    try:
        reassemble(data)
    except TdsProtocolError:
        pass


def prelogin(data: bytes) -> None:
    try:
        Prelogin.parse(data)
    except TdsProtocolError:
        pass


def login7(data: bytes) -> None:
    """LOGIN7, offsets and all, and the password it carries."""
    try:
        login = Login7.parse(data)
        deobfuscate_password(login.password)
    except TdsProtocolError:
        pass


def batch(data: bytes) -> None:
    """A SQL batch as a 7.4 client and as a 7.1 client would send it."""
    for version in (TDS_74, TDS_71):
        try:
            parse_sql_batch(data, version)
        except TdsProtocolError:
            pass


def rpc(data: bytes) -> None:
    """An RPC as a 7.4 client and as a 7.1 client would send it."""
    for version in (TDS_74, TDS_71):
        try:
            parse_rpc(data, version)
        except TdsProtocolError:
            pass


def sql(data: bytes) -> None:
    """A batch split into statements, checked, and each read as a SELECT."""
    text = data.decode("utf-8", errors="replace")
    try:
        malformed(text)
        loose_exits(text)
        for statement in statements(text):
            try:
                parse_select(statement)
            except SqlError:
                pass
    except SqlError:
        pass


def _serve(document: object) -> None:
    """What a source does with a parsed document: find the rows, build a table."""
    shape = detect(document)
    if shape is None:
        return
    rows = locate(extract(document, shape.path, ORIGIN), shape.records, ORIGIN)
    from_records(rows, name="fuzzed", origin=ORIGIN)


def json_document(data: bytes) -> None:
    """JSON shape detection, then the rows it found made into a table."""
    import json

    try:
        document = json.loads(data)
    except (ValueError, RecursionError):
        # The standard library's parser refusing the input, not this
        # project's code.
        return
    try:
        _serve(document)
    except SourceError:
        pass


def xml_document(data: bytes) -> None:
    sniff(data)
    try:
        _serve(parse_xml(data, ORIGIN))
    except SourceError:
        pass


def html_document(data: bytes) -> None:
    sniff(data)
    try:
        _serve(parse_html(data, ORIGIN))
    except SourceError:
        pass


def csv_document(data: bytes) -> None:
    try:
        from_records(csv_records(data, ORIGIN), name="fuzzed", origin=ORIGIN)
    except SourceError:
        pass


TARGETS = {
    "packet": packet,
    "prelogin": prelogin,
    "login7": login7,
    "batch": batch,
    "rpc": rpc,
    "sql": sql,
    "json": json_document,
    "xml": xml_document,
    "html": html_document,
    "csv": csv_document,
}
