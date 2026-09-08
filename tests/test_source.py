import codecs
import json
import re

import pytest

from pysqlbridge.source import (
    MAX_COLUMNS,
    MAX_NVARCHAR_CHARS,
    SourceError,
    Table,
    column_of,
    csv_records,
    from_csv,
    from_json,
    from_records,
    infer_column,
)
from pysqlbridge.tds.result import Float, Integer, NVarChar


def write(tmp_path, name: str, text: str):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


class TestInference:
    def test_all_integers_becomes_an_integer_column(self):
        column, values = infer_column("n", ["1", "2", "-3"])
        assert isinstance(column.type, Integer)
        assert values == [1, 2, -3]

    def test_one_non_integer_drops_the_whole_column_to_float(self):
        # The type is declared once in COLMETADATA, so it has to hold every
        # row. Deciding per row would produce values the metadata cannot carry.
        column, values = infer_column("n", ["1", "2", "3.5"])
        assert isinstance(column.type, Float)
        assert values == [1.0, 2.0, 3.5]

    def test_one_non_number_drops_the_column_to_text(self):
        column, values = infer_column("n", ["1", "2", "n/a"])
        assert isinstance(column.type, NVarChar)
        assert values == ["1", "2", "n/a"]

    def test_nulls_do_not_influence_the_type(self):
        column, values = infer_column("n", ["1", None, "3"])
        assert isinstance(column.type, Integer)
        assert values == [1, None, 3]

    def test_an_all_null_column_is_text(self):
        # Nothing to infer from, and text accepts whatever a later row holds.
        column, values = infer_column("n", [None, None])
        assert isinstance(column.type, NVarChar)
        assert values == [None, None]

    def test_a_wide_integer_gets_the_wider_column(self):
        column, _ = infer_column("n", [str(2**40)])
        assert isinstance(column.type, Integer) and column.type.width == 8

    def test_booleans_stay_readable_rather_than_becoming_ones_and_zeroes(self):
        # bool subclasses int in Python, so this would silently become an
        # integer column without the check.
        column, values = infer_column("flag", [True, False])
        assert isinstance(column.type, NVarChar)
        assert values == ["True", "False"]

    def test_text_column_is_wide_enough_for_its_longest_value(self):
        column, _ = infer_column("s", ["a", "abcde"])
        assert column.type.max_chars >= 5

    def test_a_value_too_wide_to_size_takes_the_max_form(self):
        # An API array flattened to JSON reaches this routinely: one Rick and
        # Morty location carries 11,250 characters of residents.
        column, _ = infer_column("s", ["x" * (MAX_NVARCHAR_CHARS + 1)])
        assert isinstance(column.type, NVarChar) and column.type.is_max

    def test_a_value_that_fits_keeps_a_declared_size(self):
        column, _ = infer_column("s", ["short"])
        assert isinstance(column.type, NVarChar) and not column.type.is_max


class TestCsv:
    def test_reads_a_header_and_rows(self, tmp_path):
        path = write(tmp_path, "people.csv", "id,name\n1,ada\n2,grace\n")
        table = from_csv(path)
        assert table.name == "people"
        assert table.column_names == ["id", "name"]
        assert table.rows == [[1, "ada"], [2, "grace"]]

    def test_empty_cells_are_null(self, tmp_path):
        path = write(tmp_path, "t.csv", "id,note\n1,\n2,hi\n")
        assert from_csv(path).rows == [[1, None], [2, "hi"]]

    def test_name_can_be_overridden(self, tmp_path):
        path = write(tmp_path, "raw_export_v2.csv", "a\n1\n")
        assert from_csv(path, name="people").name == "people"

    def test_a_byte_order_mark_does_not_become_part_of_a_name(self, tmp_path):
        # Excel writes one, and it would otherwise land in the first column.
        path = tmp_path / "bom.csv"
        path.write_bytes("﻿id,name\n1,ada\n".encode("utf-8"))
        assert from_csv(path).column_names == ["id", "name"]

    def test_a_ragged_row_is_refused_with_its_line_number(self, tmp_path):
        path = write(tmp_path, "t.csv", "a,b\n1,2\n3\n")
        with pytest.raises(SourceError, match="line 3 has 1 fields"):
            from_csv(path)

    def test_an_empty_file_is_refused(self, tmp_path):
        with pytest.raises(SourceError, match="no header row"):
            from_csv(write(tmp_path, "t.csv", ""))

    def test_a_header_with_no_rows_is_a_table_with_no_rows(self, tmp_path):
        table = from_csv(write(tmp_path, "t.csv", "a,b\n"))
        assert table.column_names == ["a", "b"]
        assert table.rows == []

    def test_a_missing_file_says_so(self, tmp_path):
        with pytest.raises(SourceError, match="could not read"):
            from_csv(tmp_path / "absent.csv")


class TestJson:
    def test_reads_an_array_of_objects(self, tmp_path):
        path = write(tmp_path, "cities.json",
                     json.dumps([{"city": "Oslo", "pop": 709037}]))
        table = from_json(path)
        assert table.name == "cities"
        assert table.rows == [["Oslo", 709037]]

    def test_keys_are_unioned_in_first_appearance_order(self, tmp_path):
        path = write(tmp_path, "t.json",
                     json.dumps([{"a": 1}, {"b": 2}, {"a": 3, "c": 4}]))
        assert from_json(path).column_names == ["a", "b", "c"]

    def test_a_missing_key_becomes_null_rather_than_shifting_the_row(self, tmp_path):
        path = write(tmp_path, "t.json", json.dumps([{"a": 1, "b": 2}, {"a": 3}]))
        assert from_json(path).rows == [[1, 2], [3, None]]

    def test_nested_objects_are_flattened_into_dotted_columns(self, tmp_path):
        # Most APIs nest, so refusing this ruled out five of the nine surveyed
        # in docs/api-shapes.md.
        path = write(tmp_path, "t.json",
                     json.dumps([{"a": {"b": 1}, "c": 2}]))
        table = from_json(path)
        assert table.column_names == ["a.b", "c"]
        assert table.rows == [[1, 2]]

    def test_nested_values_are_still_refused_when_flattening_is_off(self):
        with pytest.raises(SourceError, match="nested dict"):
            from_records([{"a": {"b": 1}}], name="t", flatten=False)

    def test_a_bare_object_is_not_a_table(self, tmp_path):
        path = write(tmp_path, "t.json", json.dumps({"a": 1}))
        with pytest.raises(SourceError, match="not a JSON array"):
            from_json(path)

    def test_an_empty_array_says_how_to_serve_it(self, tmp_path):
        # A search that matched nothing is a legitimate empty table, but
        # nothing in an empty list says what the columns were.
        with pytest.raises(SourceError, match="says nothing about its columns"):
            from_json(write(tmp_path, "t.json", "[]"))

    def test_an_empty_array_with_declared_columns_is_an_empty_table(self):
        table = from_records([], name="search", columns=["title", "year"])
        assert table.column_names == ["title", "year"] and table.rows == []

    def test_malformed_json_says_so(self, tmp_path):
        with pytest.raises(SourceError, match="not valid JSON"):
            from_json(write(tmp_path, "t.json", "{oops"))

    def test_records_already_in_memory_take_the_same_path(self):
        table = from_records([{"a": 1}], name="inline")
        assert table.name == "inline" and table.rows == [[1]]


class TestProjection:
    @staticmethod
    def table() -> Table:
        return from_records(
            [{"id": 1, "name": "ada"}, {"id": 2, "name": "grace"}], name="people"
        )

    def test_star_returns_every_column(self):
        columns, rows = self.table().select(None)
        assert [c.name for c in columns] == ["id", "name"]
        assert rows == [[1, "ada"], [2, "grace"]]

    def test_named_columns_are_projected_in_the_order_asked(self):
        columns, rows = self.table().select(["name", "id"])
        assert [c.name for c in columns] == ["name", "id"]
        assert rows == [["ada", 1], ["grace", 2]]

    def test_a_column_can_be_repeated(self):
        columns, rows = self.table().select(["id", "id"])
        assert len(columns) == 2 and rows[0] == [1, 1]

    def test_matching_is_case_insensitive(self):
        columns, _ = self.table().select(["ID"])
        assert columns[0].name == "id"

    def test_an_unknown_column_names_itself_and_its_table(self):
        with pytest.raises(SourceError, match="invalid column name 'nope'.*'people'"):
            self.table().select(["nope"])


class TestByteOrderMarks:
    """Windows writes these, and json.loads refuses them outright.

    Found by the executable's smoke test: PowerShell's -Encoding utf8 wrote a
    BOM into a generated config and the build refused to start. A config typed
    into Notepad would have hit the same thing.
    """

    def test_a_json_file_with_a_bom_still_loads(self, tmp_path):
        path = tmp_path / "t.json"
        path.write_bytes(("\ufeff" + json.dumps([{"a": 1}])).encode("utf-8"))
        assert from_json(path).rows == [[1]]

    def test_a_csv_with_a_bom_still_loads(self, tmp_path):
        path = tmp_path / "t.csv"
        path.write_bytes(("\ufeff" + "a\n1\n").encode("utf-8"))
        assert from_csv(path).column_names == ["a"]


class TestWidth:
    """What SQL Server allows in a table, and therefore what this serves.

    A flattened OpenAlex work is 534 columns and a Coinbase rate response 643,
    both of which a client can hold. A limit of our own below the protocol one
    would refuse responses for no reason a client could see.
    """

    def widely(self, count):
        return from_records(
            [{f"c{i}": i for i in range(count)}], name="wide", origin="a test"
        )

    def test_a_table_may_be_as_wide_as_sql_server_allows(self):
        assert len(self.widely(MAX_COLUMNS).columns) == MAX_COLUMNS

    def test_past_that_it_says_so_and_says_what_to_do(self):
        with pytest.raises(SourceError, match="past the 1024 this serves"):
            self.widely(MAX_COLUMNS + 1)

    def test_naming_the_columns_gets_a_wide_response_served(self):
        table = from_records(
            [{f"c{i}": i for i in range(MAX_COLUMNS + 1)}],
            name="wide", origin="a test", columns=["c0", "c9"],
        )
        assert table.column_names == ["c0", "c9"] and table.rows == [[0, 9]]


class TestCsvRecords:
    """The same reader, for bytes that arrived over a network."""

    def test_a_header_and_rows_become_dictionaries(self):
        assert csv_records(b"name,age\nada,36\n") == [{"name": "ada", "age": "36"}]

    def test_an_empty_field_is_null(self):
        assert csv_records(b"a,b\n1,\n")[0]["b"] is None

    def test_a_spreadsheet_export_carries_a_mark_and_it_is_stripped(self):
        records = csv_records(codecs.BOM_UTF8 + b"name\nada\n")
        assert list(records[0]) == ["name"]

    def test_a_header_with_no_rows_is_no_rows(self):
        assert csv_records(b"a,b\n") == []

    def test_a_ragged_line_names_its_number(self):
        with pytest.raises(SourceError, match="line 3 has 1 fields"):
            csv_records(b"a,b\n1,2\n3\n")

    def test_bytes_that_are_not_text_are_refused(self):
        with pytest.raises(SourceError, match="not UTF-8 text"):
            csv_records(b"a,b\n" + bytes([0xFF, 0xFE, 0x00]))

    def test_the_reader_is_the_one_the_file_path_uses(self, tmp_path):
        # Same bytes, same table, whichever way in they came.
        path = tmp_path / "t.csv"
        path.write_bytes(b"name,age\nada,36\n")
        off_disk = from_csv(path)
        over_the_wire = from_records(csv_records(b"name,age\nada,36\n"),
                                     name="t", origin="a test")
        assert off_disk.column_names == over_the_wire.column_names
        assert off_disk.rows == over_the_wire.rows


class TestTextThatLooksNumeric:
    """When a value written as text is read as a number, and when it is not.

    A source is believed unless reading it as a number is exact. CSV and XML
    have no types at all, so a number there can only arrive as text and has to
    be recognised; JSON has types per value, and a string of digits is a
    string the source chose to write. One rule serves both.

    Measured over 239 public API responses: before this, 791 columns were
    typed numeric from text and 377 of them lost something by it.
    """

    def typed(self, values):
        column, converted = infer_column("v", values)
        return column.type.__class__.__name__, converted

    def test_plain_digits_are_a_number(self):
        assert self.typed(["12345", "6"]) == ("Integer", [12345, 6])

    def test_a_leading_zero_is_not(self):
        # ipapi answers utc_offset with "-0700", and -700 is a different
        # thing; a postcode of 02134 is not 2134 either.
        assert self.typed(["007", "008"])[0] == "NVarChar"
        assert self.typed(["-0700"]) == ("NVarChar", ["-0700"])

    def test_a_written_sign_is_not(self):
        # ipapi answers country_calling_code with "+1".
        assert self.typed(["+1", "+44"])[0] == "NVarChar"

    def test_trailing_zeros_are_spelling_and_may_go(self):
        # The Nobel Prize API writes latitudes as "56.000000".
        assert self.typed(["56.000000", "40.825930"])[0] == "Float"

    def test_digits_a_float_cannot_hold_are_kept_as_text(self):
        # Coinbase quotes rates to 19 significant digits as JSON strings.
        assert self.typed(["65.8843992331055929"]) == (
            "NVarChar", ["65.8843992331055929"]
        )

    def test_a_number_that_arrived_as_a_number_is_one(self):
        assert self.typed([12345, 6]) == ("Integer", [12345, 6])
        assert self.typed([1.5, 2]) == ("Float", [1.5, 2.0])

    def test_one_value_that_does_not_survive_holds_the_whole_column(self):
        assert self.typed(["1", "2", "007"])[0] == "NVarChar"

    def test_a_csv_keeps_its_leading_zeros_too(self, tmp_path):
        path = tmp_path / "t.csv"
        path.write_text("code,n\n02134,5\n90210,6\n", encoding="utf-8")
        table = from_csv(path)
        assert table.rows[0][0] == "02134"
        assert table.rows[0][1] == 5


class TestValuesAnExpressionMade:
    """A column for values whose types are already decided, not inferred.

    A source has to be read to find out what it holds. An expression says
    what it made, and reading it back would undo it.
    """

    def typed(self, values):
        column, converted = column_of("v", values)
        return column.type.__class__.__name__, converted

    def test_text_stays_text_even_when_it_reads_as_a_number(self):
        # This is what CAST(id AS nvarchar(10)) produces.
        assert self.typed(["1", "2"]) == ("NVarChar", ["1", "2"])

    def test_integers_are_integers(self):
        assert self.typed([1, 2]) == ("Integer", [1, 2])

    def test_a_mix_of_whole_and_not_is_a_float(self):
        assert self.typed([1, 2.5]) == ("Float", [1.0, 2.5])

    def test_a_mix_of_text_and_number_is_text(self):
        assert self.typed([1, "x"])[0] == "NVarChar"

    def test_nothing_at_all_is_text(self):
        assert self.typed([None, None]) == ("NVarChar", [None, None])

class TestANumberTooBigForAnInteger:
    """A whole number outside the widest integer column keeps its digits.

    It used to be declared bigint on the strength of being a whole number,
    and then fail to encode: a nineteen digit id in a file reached the
    client as an internal error rather than as its own digits. SQL Server
    reads such a number as a decimal and keeps every digit; there is no
    decimal here and text keeps them too.

    A float does not, and is not used even where it happens to fit:
    9223372036854775808 is exactly representable and 9223372036854775809 is
    not, and a column whose type turns over between one row and the next is
    worse than one that keeps what the file said. Excel would show it as
    9.22E+18 either way.
    """

    def kind(self, values):
        column, converted = infer_column("v", values)
        return column.type.__class__.__name__, converted

    @pytest.mark.parametrize("value", [
        9223372036854775808,                 # one past the top
        2**63,
        123456789012345678901234567890,
        -9223372036854775809,                # one past the bottom
        "9223372036854775808",               # and the same arriving as text
        "123456789012345678901234567890",
    ])
    def test_it_is_text_and_the_digits_are_kept(self, value):
        kind, converted = self.kind([value])
        assert kind == "NVarChar"
        assert converted == [str(value)]

    @pytest.mark.parametrize("value", [
        9223372036854775807,                 # the top itself
        -9223372036854775808,                # and the bottom
        "9223372036854775807",
        0, 1, -1,
    ])
    def test_one_that_fits_is_still_an_integer(self, value):
        kind, converted = self.kind([value])
        assert kind == "Integer"
        assert converted == [int(value)]

    def test_one_row_over_the_edge_takes_the_column_with_it(self):
        kind, converted = self.kind([1, 9223372036854775808])
        assert kind == "NVarChar"
        assert converted == ["1", "9223372036854775808"]

    def test_the_widest_that_fits_is_declared_eight_bytes(self):
        column, _ = infer_column("v", [9223372036854775807])
        assert column.type.width == 8

    def test_every_value_can_be_sent(self):
        # Which is the whole point: the column used to be one the encoder
        # could not write.
        column, converted = infer_column("v", [1, 9223372036854775808])
        for value in converted:
            column.type.encode(value)

    def test_a_float_that_big_is_still_a_float(self):
        # The rule is about whole numbers. 1e300 was never an integer.
        kind, _ = self.kind([1e300])
        assert kind == "Float"

class TestACsvSeparatedBySomethingElse:
    """A delimiter the configuration names, rather than one guessed at.

    Half of Europe writes a CSV with semicolons because the comma is its
    decimal point. Read with commas such a file is one column called
    "id;name" holding "1;ada": no error, no missing rows, and nothing a
    person can act on. Sniffing it would be guessing from a resemblance,
    which this project does not do; being able to say is the fix.
    """

    def written(self, tmp_path, text):
        path = tmp_path / "sales.csv"
        path.write_text(text, encoding="utf-8")
        return path

    def test_semicolons(self, tmp_path):
        table = from_csv(self.written(tmp_path, "id;name\n1;ada\n2;Grace\n"),
                         delimiter=";")
        assert table.column_names == ["id", "name"]
        assert [list(row) for row in table.rows] == [[1, "ada"], [2, "Grace"]]

    def test_tabs(self, tmp_path):
        table = from_csv(self.written(tmp_path, "id\tname\n1\tada\n"),
                         delimiter="\t")
        assert table.column_names == ["id", "name"]

    def test_pipes(self, tmp_path):
        table = from_csv(self.written(tmp_path, "id|name\n1|ada\n"),
                         delimiter="|")
        assert table.column_names == ["id", "name"]

    def test_a_decimal_comma_is_kept_as_text(self, tmp_path):
        # The reason the file uses semicolons in the first place. 10,5 is
        # not a number this serves, and it is not 105 either.
        table = from_csv(self.written(tmp_path, "id;score\n1;10,5\n"),
                         delimiter=";")
        assert [list(row) for row in table.rows] == [[1, "10,5"]]

    def test_a_comma_is_still_the_default(self, tmp_path):
        table = from_csv(self.written(tmp_path, "id,name\n1,ada\n"))
        assert table.column_names == ["id", "name"]

    def test_quoting_still_works_inside_one(self, tmp_path):
        table = from_csv(
            self.written(tmp_path, 'id;name\n1;"ada; the first"\n'),
            delimiter=";")
        assert [list(row) for row in table.rows] == [[1, "ada; the first"]]

    @pytest.mark.parametrize("delimiter", ["", ";;", ", ", None, 1])
    def test_a_delimiter_is_one_character(self, tmp_path, delimiter):
        with pytest.raises(SourceError, match="exactly one character"):
            from_csv(self.written(tmp_path, "id;name\n1;ada\n"),
                     delimiter=delimiter)

    def test_the_wrong_delimiter_is_refused_where_it_shows(self, tmp_path):
        # Not always: a file with no commas in it reads as one column and
        # says nothing, which is why the setting exists. Where the rows come
        # out ragged, the reader already refuses rather than padding.
        with pytest.raises(SourceError, match="fields but the header"):
            from_csv(self.written(tmp_path, "id;name\n1;ada,x\n"))

class TestAFileThatIsNotUtf8:
    """A spreadsheet saved as Windows-1252 with one accented name in it.

    Which is the commonest file this will ever be handed. The reader that
    takes a CSV over HTTP has always said so; the two that take a file let
    the decoder's own UnicodeDecodeError out instead, and a person got a
    traceback rather than the one fact they needed.
    """

    def written(self, tmp_path, name):
        path = tmp_path / name
        path.write_bytes("id,name\n1,caf\xe9\n".encode("cp1252"))
        return path

    def test_a_csv_says_so(self, tmp_path):
        with pytest.raises(SourceError, match="is not UTF-8 text"):
            from_csv(self.written(tmp_path, "a.csv"))

    def test_a_json_says_so(self, tmp_path):
        with pytest.raises(SourceError, match="is not UTF-8 text"):
            from_json(self.written(tmp_path, "a.json"))

    def test_it_names_the_file(self, tmp_path):
        path = self.written(tmp_path, "a.csv")
        with pytest.raises(SourceError, match=re.escape(path.name)):
            from_csv(path)

    def test_a_file_that_is_utf8_still_loads(self, tmp_path):
        path = tmp_path / "good.csv"
        path.write_text("id,name\n1,caf\u00e9\n", encoding="utf-8")
        assert [list(row) for row in from_csv(path).rows] == [[1, "caf\u00e9"]]
