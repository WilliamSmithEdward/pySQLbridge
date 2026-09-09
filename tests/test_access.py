"""Reading an Access database.

Two halves. The first asks the mapping questions directly, and the second
builds a real database and reads it back. Both run anywhere: the reading is
pure Python, so there is no engine to install and nothing to skip.
"""

import datetime
import decimal
import pathlib

import pytest

from pysqlbridge.access import (
    JET_BOOLEAN,
    JET_DATETIME,
    JET_DOUBLE,
    JET_GUID,
    JET_INT,
    JET_LONG,
    JET_MEMO,
    JET_MONEY,
    JET_OLE,
    JET_TEXT,
    _as_declared,
    _column_for,
    _typed,
    _value,
)
from pysqlbridge.source import SourceError, from_access
from pysqlbridge.tds.result import Column, DateTime, Float, Integer, NVarChar

from .databases import build


class TestTheTypesItServes:
    def test_the_declared_width_of_an_integer_is_kept(self):
        # Access's Number/Integer is two bytes and its Long Integer is four.
        # Widening both to four would declare a column bigger than the
        # database says it is.
        assert _column_for("a", JET_INT, 2, "t", "f").type == Integer(2)
        assert _column_for("a", JET_LONG, 4, "t", "f").type == Integer(4)

    def test_a_double_is_a_float(self):
        assert _column_for("a", JET_DOUBLE, 8, "t", "f").type == Float(8)

    def test_a_date_is_a_moment(self):
        assert isinstance(_column_for("a", JET_DATETIME, 8, "t", "f").type,
                          DateTime)

    def test_text_keeps_the_width_it_was_declared_with(self):
        # Which inference could not know: a text column holding only digits
        # is still a text column, and the database is the thing that says so.
        # Jet declares the length in bytes, two to a character, so TEXT(50)
        # arrives as 100 and a column half the width it should be is what
        # taking the number at face value would give.
        assert _column_for("a", JET_TEXT, 100, "t", "f").type == NVarChar(50)
        assert _column_for("a", JET_TEXT, 20, "t", "f").type == NVarChar(10)

    def test_a_memo_takes_the_max_form(self):
        # A memo declares no length at all, and past what a sized nvarchar
        # can declare there is no size to declare.
        assert _column_for("a", JET_MEMO, 0, "t", "f").type == NVarChar(None)

    def test_a_type_with_no_answer_here_is_left_to_the_values(self):
        # Yes/No, currency and the GUID. None is not a failure: it says to
        # read the column off what is in it.
        assert _column_for("a", JET_BOOLEAN, 1, "t", "f") is None
        assert _column_for("a", JET_MONEY, 8, "t", "f") is None
        assert _column_for("a", JET_GUID, 16, "t", "f") is None

    def test_a_binary_column_is_refused_by_name(self):
        # An OLE Object holds an embedded file and there is no column type
        # here that carries one. The message names the column, because the
        # fix is to leave it out of a query saved in Access.
        with pytest.raises(SourceError, match="'photo'"):
            _column_for("photo", JET_OLE, 0, "Members", "db.accdb")


class TestTheValuesItPassesOn:
    def test_a_decimal_is_passed_on_as_its_digits(self):
        # Currency arrives as a Decimal, which nothing downstream types. As
        # digits, the same rule that reads a number out of a CSV decides
        # whether a float holds it exactly or it has to stay text.
        assert _value(decimal.Decimal("12.3400"), "a column") == "12.3400"

    def test_binary_data_is_refused(self):
        with pytest.raises(SourceError, match="binary"):
            _value(b"\x00\x01", "a column")

    def test_the_refusal_names_where_it_was(self):
        with pytest.raises(SourceError, match="column 'photo'"):
            _value(b"\x00", "column 'photo'")

    def test_a_moment_is_already_a_plain_one(self):
        # Nothing to strip. The reader gives a naive datetime because Access
        # stores no zone, where the COM layer this replaced attached the
        # local one and made every date incomparable with every other.
        when = datetime.datetime(2024, 1, 15, 13, 30)
        assert _value(when, "a column") is when

    def test_everything_else_is_itself(self):
        assert _value(None, "a column") is None
        assert _value("ada", "a column") == "ada"
        assert _value(True, "a column") is True


class TestBuildingATable:
    def built(self, name, declared, rows):
        return _typed(name, declared, rows, "db.accdb")

    def test_declared_columns_keep_their_type(self):
        table = self.built("T", [("id", Column("id", Integer(4))),
                                 ("name", Column("name", NVarChar(50)))],
                           [[1, "ada"], [2, "grace"]])
        assert [type(c.type).__name__ for c in table.columns] == [
            "Integer", "NVarChar"]
        assert table.columns[1].type == NVarChar(50)
        assert table.rows == [[1, "ada"], [2, "grace"]]

    def test_a_text_column_of_digits_stays_text(self):
        # The case inference gets wrong and the database gets right.
        table = self.built("T", [("code", Column("code", NVarChar(10)))],
                           [["1"], ["2"]])
        assert isinstance(table.columns[0].type, NVarChar)
        assert table.rows == [["1"], ["2"]]

    def test_a_column_with_no_declared_answer_is_read_off_its_values(self):
        table = self.built("T", [("fee", None)],
                           [[decimal.Decimal("12.34")], [decimal.Decimal("0.5")]])
        assert isinstance(table.columns[0].type, Float)
        assert table.rows == [[12.34], [0.5]]

    def test_two_columns_of_one_name_are_refused(self):
        with pytest.raises(SourceError, match="more than once"):
            self.built("T", [("a", Column("a", Integer(4))),
                             ("A", Column("A", Integer(4)))], [])

    def test_a_table_with_no_rows_keeps_its_columns(self):
        # Unlike a sheet, which has no headings when it is empty, an empty
        # Access table still declares what it holds.
        table = self.built("T", [("a", Column("a", NVarChar(5)))], [])
        assert [c.name for c in table.columns] == ["a"]
        assert table.rows == []

    def test_values_are_converted_to_what_the_column_declares(self):
        assert _as_declared(Column("a", Integer(4)), [1, None, "2"]) == [1, None, 2]
        assert _as_declared(Column("a", Float(8)), [1, None]) == [1.0, None]
        assert _as_declared(Column("a", NVarChar(5)), [1, None]) == ["1", None]


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    """One database for the lot, because building one costs a second."""
    return build(tmp_path_factory.mktemp("access") / "test.accdb")


class TestAgainstARealDatabase:
    """A database written by the same library that reads one."""

    def test_every_table_and_saved_query_is_served(self, database):
        names = [table.name for table in from_access(database)]
        # BigRooms is a saved query, which Access calls a view and which is
        # the shape somebody deliberately made. NoRooms is one too, and it
        # answers nothing, so there is nothing to describe it by.
        assert names == ["BigRooms", "Members", "Nothing", "Rooms"]

    def test_the_engines_own_tables_are_not_served(self, database):
        # MSysObjects and the rest are the engine's bookkeeping, and nobody
        # pointed this at a database to read those.
        names = [table.name for table in from_access(database)]
        assert not [name for name in names if name.startswith("MSys")]

    def test_the_declared_types_reach_the_client(self, database):
        table, = from_access(database, table="Members")
        kinds = {c.name: c.type for c in table.columns}
        assert kinds["name"] == NVarChar(50)
        assert kinds["small"] == Integer(2)
        assert kinds["tally"] == Integer(4)
        assert kinds["score"] == Float(8)
        assert isinstance(kinds["joined"], DateTime)
        assert kinds["notes"] == NVarChar(None)

    def test_the_rows_come_back_as_they_went_in(self, database):
        table, = from_access(database, table="Members")
        at = table.column_names
        rows = [dict(zip(at, row)) for row in table.rows]
        assert rows[0]["name"] == "ada"
        assert rows[0]["joined"] == datetime.datetime(2024, 1, 15)
        assert rows[1]["joined"] == datetime.datetime(2023, 6, 1, 13, 30)
        assert rows[1]["notes"] is None
        assert rows[2]["score"] is None

    def test_a_date_arrives_as_a_plain_datetime(self, database):
        table, = from_access(database, table="Members")
        when = table.rows[0][table.column_names.index("joined")]
        assert type(when) is datetime.datetime

    def test_a_memo_takes_the_max_form(self, database):
        table, = from_access(database, table="Members")
        notes = table.columns[table.column_names.index("notes")]
        assert notes.type == NVarChar(None)

    def test_currency_is_read_off_its_values(self, database):
        table, = from_access(database, table="Members")
        fee = table.columns[table.column_names.index("fee")]
        assert isinstance(fee.type, Float)
        assert [row[table.column_names.index("fee")] for row in table.rows] == [
            12.34, 0.5, 0.0]

    def test_yes_no_reads_as_its_word(self, database):
        # As a JSON true and an Excel TRUE do, so that the same value written
        # in three sources answers the same thing.
        table, = from_access(database, table="Members")
        active = table.column_names.index("active")
        assert [row[active] for row in table.rows] == ["True", "False", "True"]

    def test_an_empty_table_is_served_with_its_columns(self, database):
        table, = from_access(database, table="Nothing")
        assert [c.name for c in table.columns] == ["a"]
        assert table.rows == []

    def test_a_saved_query_answers_what_it_selects(self, database):
        # Run where it was written, in Access's own SQL, and its rows arrive
        # here already worked out. Its columns are read off those rows,
        # because a query has no stored schema to read them off instead.
        table, = from_access(database, table="BigRooms")
        assert table.column_names == ["code", "seats"]
        assert table.rows == [["A1", 30]]

    def test_a_saved_query_that_answers_nothing_is_passed_over(self, database):
        # No rows, so no column names, so nothing to describe a table with.
        # Skipped when reading the lot, as an empty sheet is.
        assert "NoRooms" not in [t.name for t in from_access(database)]

    def test_but_says_so_when_that_query_was_the_one_asked_for(self, database):
        with pytest.raises(SourceError, match="no rows"):
            from_access(database, table="NoRooms")

    def test_a_table_that_is_not_there(self, database):
        with pytest.raises(SourceError, match="no table called"):
            from_access(database, table="Missing")

    def test_a_file_that_is_not_there(self, tmp_path):
        with pytest.raises(SourceError, match="no such file"):
            from_access(tmp_path / "nothing.accdb")

    def test_one_table_can_be_renamed(self, database):
        table, = from_access(database, table="Rooms", name="spaces")
        assert table.name == "spaces"

    def test_naming_a_database_of_several_tables_is_refused(self, database):
        with pytest.raises(SourceError, match='"table"'):
            from_access(database, name="things")

    def test_it_is_queried_like_anything_else(self, database):
        from pysqlbridge.catalog import Catalog
        from pysqlbridge.tds.result import Query

        catalog = Catalog()
        for table in from_access(database):
            catalog.add(table)
        answer = catalog.answer(Query(
            sql="SELECT name FROM Members WHERE joined < '2000-01-01'",
            parameters={}, session={}))
        assert [list(row) for row in answer.rows] == [["edsger"]]

    def test_a_config_can_name_a_database(self, database):
        import json

        from pysqlbridge.catalog import load

        config = database.parent / "config.json"
        config.write_text(json.dumps({"tables": [
            {"access": database.name, "table": "Rooms"}]}), encoding="utf-8")
        catalog = load(config)
        assert list(catalog.sources) == ["rooms"]

    def test_reading_leaves_the_file_alone(self, database):
        # The bytes are read once and never written, which is stronger than
        # opening read-only: no handle is held, so nothing can be locked and
        # nothing can be changed. Somebody can have the same database open
        # in Access while it is being served.
        before = database.read_bytes()
        from_access(database)
        assert database.read_bytes() == before
        assert not list(database.parent.glob("*.laccdb"))

    def test_nothing_it_needs_is_windows_only(self):
        # The reason this replaced the database engine: the engine is a
        # separate download, installs in one bit width, and is not on Linux
        # at all. Everything below is pure Python and imports anywhere.
        import pysqlbridge.access as reader

        source = pathlib.Path(reader.__file__).read_text(encoding="utf-8")
        for named in ("win32com", "pythoncom", "pyodbc", "ADODB", "OLEDB"):
            assert named not in source
