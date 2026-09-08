"""The log a client's queries are written to, and read back out of.

Not the server's behaviour but the tooling's, and it is worth a test file
because when this is wrong nobody is told. A log that cut every query at its
first line break made scripts/replay.py report seventeen refusals that were
its own and hide twenty queries entirely, and the only symptom was a work
list that looked longer and shorter than it was at the same time.
"""

import pathlib
import sys

import pytest

from pysqlbridge.server import written_out

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
from replay import MARK, queries_in, unescaped  # noqa: E402

SENT = [
    "SELECT 1",
    "SELECT 1\nFROM people",
    "SELECT 1\r\nFROM people\r\nWHERE id = 1",
    "SELECT 1\rFROM people",
    "SELECT 'a backslash \\ in a string'",
    "SELECT 'ends with one \\'",
    "SELECT '\\n is two characters here'",
    "SELECT 1 -- a comment\nSELECT 2",
]


class TestWritingAQueryToALog:
    @pytest.mark.parametrize("sent", SENT)
    def test_it_takes_one_line(self, sent):
        assert "\n" not in written_out(sent)
        assert "\r" not in written_out(sent)

    @pytest.mark.parametrize("sent", SENT)
    def test_and_comes_back_as_what_went_in(self, sent):
        assert unescaped(written_out(sent)) == sent

    def test_a_comment_does_not_swallow_what_follows_it(self):
        # The reason the log holds the query as it arrived rather than
        # flattened: a line comment run together with the next line takes
        # the statement that mattered with it.
        sent = "SELECT 1 -- a comment\nSELECT 2"
        assert unescaped(written_out(sent)).splitlines()[-1] == "SELECT 2"


class TestReadingThemBackOut:
    def written(self, tmp_path, queries, terminator="\n"):
        log = tmp_path / "one.log"
        log.write_text(
            "".join(f"127.0.0.1:1 {MARK}{written_out(one)}{terminator}"
                    for one in queries),
            encoding="utf-8", newline="",
        )
        return log

    def test_every_query_comes_back(self, tmp_path):
        assert queries_in(self.written(tmp_path, SENT)) == [
            one.rstrip() for one in SENT
        ]

    def test_a_log_whose_lines_end_with_crlf_reads_the_same(self, tmp_path):
        assert queries_in(self.written(tmp_path, SENT, "\r\n")) == [
            one.rstrip() for one in SENT
        ]

    def test_a_log_from_before_carriage_returns_were_escaped(self, tmp_path):
        # What the writer used to produce: the newline escaped and the
        # carriage return left where it was. Read as a line break it cut the
        # query in half, and two queries sharing a first line became one.
        log = tmp_path / "old.log"
        log.write_text(
            f"127.0.0.1:1 {MARK}" + "SELECT a\r\\nFROM people\n"
            f"127.0.0.1:1 {MARK}" + "SELECT a\r\\nFROM tasks\n",
            encoding="utf-8", newline="",
        )
        found = queries_in(log)
        assert found == ["SELECT a\r\nFROM people", "SELECT a\r\nFROM tasks"]

    def test_a_log_with_nothing_in_it(self, tmp_path):
        empty = tmp_path / "empty.log"
        empty.write_text("nothing was logged here\n", encoding="utf-8")
        assert queries_in(empty) == []

    def test_a_line_cut_off_by_the_end_of_the_file(self, tmp_path):
        log = tmp_path / "cut.log"
        log.write_text(f"127.0.0.1:1 {MARK}SELECT 1", encoding="utf-8")
        assert queries_in(log) == ["SELECT 1"]
