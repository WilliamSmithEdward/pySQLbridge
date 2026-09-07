import json

import pytest

from pysqlbridge.source import (
    MAX_COLUMNS,
    MAX_NVARCHAR_CHARS,
    SourceError,
    Table,
    from_csv,
    from_json,
    from_records,
    infer_column,
)
from pysqlbridge.tds.result import Column, Float, Integer, NVarChar


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
