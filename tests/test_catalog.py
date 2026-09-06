import json

import pytest

from pysqlbridge.catalog import INVALID_OBJECT_NAME, UNSUPPORTED, Catalog, load
from pysqlbridge.source import SourceError, from_records
from pysqlbridge.tds.result import QueryError


def catalog() -> Catalog:
    c = Catalog()
    c.add(from_records([{"id": 1, "name": "ada"}, {"id": 2, "name": "grace"}],
                       name="people"))
    c.add(from_records([{"city": "Oslo"}], name="cities"))
    return c


class TestAnswering:
    def test_select_star_returns_the_table(self):
        result = catalog().answer("SELECT * FROM people")
        assert [c.name for c in result.columns] == ["id", "name"]
        assert result.rows == [[1, "ada"], [2, "grace"]]

    def test_columns_are_projected(self):
        result = catalog().answer("SELECT name FROM people")
        assert [c.name for c in result.columns] == ["name"]
        assert result.rows == [["ada"], ["grace"]]

    def test_top_limits_the_rows(self):
        assert len(catalog().answer("SELECT TOP 1 * FROM people").rows) == 1

    def test_top_larger_than_the_table_is_not_an_error(self):
        assert len(catalog().answer("SELECT TOP 99 * FROM people").rows) == 2

    def test_table_names_match_case_insensitively(self):
        assert catalog().answer("SELECT * FROM PEOPLE").rows

    def test_bracketed_and_schema_qualified_names_resolve(self):
        assert catalog().answer("SELECT * FROM [dbo].[people]").rows


class TestSetupBatches:
    """Clients open a session before they ask anything."""

    @pytest.mark.parametrize("sql", [
        "SET QUOTED_IDENTIFIER OFF", "SET TEXTSIZE 4096", "USE master",
    ])
    def test_complete_without_a_result_set(self, sql):
        result = catalog().answer(sql)
        assert result.columns == [] and result.rows == []


class TestErrors:
    def test_an_unknown_table_uses_the_invalid_object_name_number(self):
        # 208 is what SQL Server sends, and clients already present it well.
        with pytest.raises(QueryError, match="invalid object name 'nope'") as caught:
            catalog().answer("SELECT * FROM nope")
        assert caught.value.number == INVALID_OBJECT_NAME == 208

    def test_an_unknown_table_lists_what_is_available(self):
        with pytest.raises(QueryError, match="cities, people"):
            catalog().answer("SELECT * FROM nope")

    def test_an_unknown_column_is_also_an_invalid_object_name(self):
        with pytest.raises(QueryError, match="invalid column name 'nope'") as caught:
            catalog().answer("SELECT nope FROM people")
        assert caught.value.number == INVALID_OBJECT_NAME

    def test_unsupported_sql_uses_the_user_defined_number(self):
        # This project's complaint, not one of SQL Server's.
        with pytest.raises(QueryError, match="ORDER is not supported") as caught:
            catalog().answer("SELECT * FROM people ORDER BY id")
        assert caught.value.number == UNSUPPORTED == 50000

    def test_duplicate_table_names_are_refused_when_added(self):
        c = catalog()
        with pytest.raises(SourceError, match="both called 'people'"):
            c.add(from_records([{"x": 1}], name="people"))


class TestConfig:
    @staticmethod
    def build(tmp_path):
        (tmp_path / "people.csv").write_text("id,name\n1,ada\n", encoding="utf-8")
        (tmp_path / "cities.json").write_text(
            json.dumps([{"city": "Oslo"}]), encoding="utf-8")
        config = tmp_path / "tables.json"
        config.write_text(json.dumps({"tables": [
            {"name": "people", "csv": "people.csv"},
            {"name": "cities", "json": "cities.json"},
        ]}), encoding="utf-8")
        return config

    def test_loads_both_source_kinds(self, tmp_path):
        assert load(self.build(tmp_path)).names == ["cities", "people"]

    def test_paths_resolve_against_the_config_not_the_working_directory(self, tmp_path):
        # So a config and its data can be moved together.
        nested = tmp_path / "deep"
        nested.mkdir()
        config = self.build(nested)
        assert load(config).answer("SELECT * FROM people").rows == [[1, "ada"]]

    def test_the_file_name_supplies_the_table_name_when_none_is_given(self, tmp_path):
        (tmp_path / "widgets.csv").write_text("a\n1\n", encoding="utf-8")
        config = tmp_path / "c.json"
        config.write_text(json.dumps({"tables": [{"csv": "widgets.csv"}]}),
                          encoding="utf-8")
        assert load(config).names == ["widgets"]

    def test_a_table_needs_exactly_one_source_kind(self, tmp_path):
        config = tmp_path / "c.json"
        config.write_text(json.dumps({"tables": [{"csv": "a.csv", "json": "a.json"}]}),
                          encoding="utf-8")
        with pytest.raises(SourceError, match="exactly one of"):
            load(config)

    def test_no_tables_array(self, tmp_path):
        config = tmp_path / "c.json"
        config.write_text(json.dumps({}), encoding="utf-8")
        with pytest.raises(SourceError, match='non-empty "tables" array'):
            load(config)

    def test_a_missing_config_says_so(self, tmp_path):
        with pytest.raises(SourceError, match="could not read"):
            load(tmp_path / "absent.json")
