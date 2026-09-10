"""Reading a workbook, against what Excel actually writes into one."""

import datetime
import zipfile

import pytest

from pysqlbridge.source import MAX_COLUMNS, SourceError, from_excel
from pysqlbridge.tds.result import DateTime, Float, Integer
from pysqlbridge.workbook import MAX_SHEETS, _is_a_date, _letters, sheets

from .workbooks import Error, Formula, Inline, Raw, Table, serial, workbook


class TestWhatASheetHolds:
    def test_the_first_row_names_the_columns(self, tmp_path):
        path = workbook(tmp_path / "b.xlsx",
                        {"S": [["id", "name"], [1, "ada"], [2, "grace"]]})
        table, = from_excel(path)
        assert table.name == "S"
        assert table.column_names == ["id", "name"]
        assert table.rows == [[1, "ada"], [2, "grace"]]

    def test_text_comes_out_of_the_shared_table(self, tmp_path):
        # The sheet holds an index, not the word, and the same word written
        # twice is stored once. Reading the index as the value would give a
        # column of small integers that looked plausible.
        path = workbook(tmp_path / "b.xlsx",
                        {"S": [["word"], ["ada"], ["grace"], ["ada"]]})
        table, = from_excel(path)
        assert table.rows == [["ada"], ["grace"], ["ada"]]

    def test_a_whole_number_stays_a_whole_number(self, tmp_path):
        # Excel keeps every number as a double, and writing them back as
        # floats would declare a float column for a column of counts.
        path = workbook(tmp_path / "b.xlsx", {"S": [["n"], [1], [2], [3]]})
        table, = from_excel(path)
        assert isinstance(table.columns[0].type, Integer)
        assert table.rows == [[1], [2], [3]]

    def test_a_fractional_number_makes_a_float_column(self, tmp_path):
        path = workbook(tmp_path / "b.xlsx", {"S": [["n"], [1], [2.5]]})
        table, = from_excel(path)
        assert isinstance(table.columns[0].type, Float)
        assert table.rows == [[1.0], [2.5]]

    def test_a_cell_that_is_not_there_is_null(self, tmp_path):
        # Excel writes no <c> at all for an empty cell, so the position of
        # everything after it comes from its reference and not from counting.
        path = workbook(tmp_path / "b.xlsx",
                        {"S": [["a", "b", "c"], [1, None, 3]]})
        table, = from_excel(path)
        assert table.rows == [[1, None, 3]]

    def test_a_row_that_is_not_there_is_not_a_row(self, tmp_path):
        # A gap between two rows is a gap, not a row of nulls: Excel leaves
        # the row out entirely, and inventing one would add a phantom record
        # to every count and average over the sheet.
        path = workbook(tmp_path / "b.xlsx",
                        {"S": [["a"], [1], [None], [3]]})
        table, = from_excel(path)
        assert table.rows == [[1], [3]]

    def test_a_formula_arrives_as_its_last_value(self, tmp_path):
        # Nothing here evaluates a formula. What Excel worked out last is in
        # the file beside it, and that is what a client asking the sheet
        # would have seen on screen.
        path = workbook(tmp_path / "b.xlsx", {
            "S": [["n", "word"],
                  [Formula("99.5/2", 49.75), Formula('UPPER("q")', "Q", "str")]],
        })
        table, = from_excel(path)
        assert table.rows == [[49.75, "Q"]]

    def test_an_error_cell_is_null(self, tmp_path):
        # #DIV/0! is not a value SQL has, and what it stands for is the
        # absence of one. Every other reader of the format answers null here.
        path = workbook(tmp_path / "b.xlsx",
                        {"S": [["a"], [1], [Error("#DIV/0!")]]})
        table, = from_excel(path)
        assert table.rows == [[1], [None]]

    def test_a_row_of_nothing_but_errors_is_still_a_row(self, tmp_path):
        # Somebody wrote formulas across it and every one of them failed.
        # The row is there, and its values are null; dropping it because
        # nothing in it survived would lose a record that a person can see
        # on the screen.
        path = workbook(tmp_path / "b.xlsx", {
            "S": [["a", "b"], [1, 2], [Error(), Error("#N/A")], [5, 6]]})
        table, = from_excel(path)
        assert table.rows == [[1, 2], [None, None], [5, 6]]

    def test_a_row_that_is_only_formatting_is_not_a_row(self, tmp_path):
        # Excel writes a <row> for a range somebody coloured, with cells that
        # carry a style and no value. There is no record there.
        path = workbook(tmp_path / "b.xlsx", {
            "S": [["a"], [1], [Raw(' s="1"', "")], [3]]})
        table, = from_excel(path)
        assert table.rows == [[1], [3]]

    def test_a_boolean_reads_as_its_word(self, tmp_path):
        # As a JSON true does, and for the same reason: a bit column here
        # would show 1 and 0 where the sheet shows TRUE and FALSE.
        path = workbook(tmp_path / "b.xlsx",
                        {"S": [["on"], [True], [False]]})
        table, = from_excel(path)
        assert table.rows == [["True"], ["False"]]

    def test_text_written_into_the_sheet_is_read(self, tmp_path):
        # An inline string, which is what a producer that is not Excel often
        # writes rather than building a shared table.
        path = workbook(tmp_path / "b.xlsx",
                        {"S": [[Inline("id")], [Inline("x")]]})
        table, = from_excel(path)
        assert table.column_names == ["id"]
        assert table.rows == [["x"]]

    def test_an_empty_sheet_serves_nothing_rather_than_failing(self, tmp_path):
        path = workbook(tmp_path / "b.xlsx",
                        {"Data": [["a"], [1]], "Notes": []})
        names = [table.name for table in from_excel(path)]
        assert names == ["Data"]

    def test_an_empty_sheet_asked_for_by_name_says_so(self, tmp_path):
        # Skipping it silently is right when reading everything and wrong
        # when somebody named it: they were expecting rows.
        path = workbook(tmp_path / "b.xlsx", {"Notes": []})
        with pytest.raises(SourceError, match="empty"):
            from_excel(path, sheet="Notes")


class TestDates:
    def test_a_styled_number_is_a_moment(self, tmp_path):
        # 45306 is only a date because the style points at a date format.
        # Nothing else in the file says so.
        when = datetime.datetime(2024, 1, 15)
        path = workbook(tmp_path / "b.xlsx", {"S": [["hired"], [when]]})
        table, = from_excel(path)
        assert isinstance(table.columns[0].type, DateTime)
        assert table.rows == [[when]]

    def test_the_time_of_day_survives(self, tmp_path):
        when = datetime.datetime(2023, 6, 1, 13, 30)
        path = workbook(tmp_path / "b.xlsx", {"S": [["at"], [when]]})
        table, = from_excel(path)
        assert table.rows == [[when]]

    def test_an_unstyled_number_stays_a_number(self, tmp_path):
        path = workbook(tmp_path / "b.xlsx", {"S": [["n"], [45306]]})
        table, = from_excel(path)
        assert isinstance(table.columns[0].type, Integer)
        assert table.rows == [[45306]]

    def test_a_date_before_the_leap_year_fiction(self, tmp_path):
        # Day 59 is 1900-02-28, and day 61 is 1900-03-01. Excel believes
        # there was a day between them. One epoch cannot put both back.
        path = workbook(tmp_path / "b.xlsx", {
            "S": [["d"], [Raw(' s="1"', "<v>59</v>")],
                  [Raw(' s="1"', "<v>61</v>")]],
        })
        table, = from_excel(path)
        assert table.rows == [[datetime.datetime(1900, 2, 28)],
                              [datetime.datetime(1900, 3, 1)]]

    def test_the_day_that_did_not_happen_is_refused(self, tmp_path):
        path = workbook(tmp_path / "b.xlsx",
                        {"S": [["d"], [Raw(' s="1"', "<v>60</v>")]]})
        with pytest.raises(SourceError, match="1900-02-29"):
            from_excel(path)

    def test_a_workbook_that_counts_from_1904(self, tmp_path):
        # Excel for Mac before 2011. The same serial is four years and a day
        # away from what the 1900 workbook means by it, and nothing about a
        # date read against the wrong epoch looks wrong.
        path = workbook(tmp_path / "b.xlsx",
                        {"S": [["d"], [Raw(' s="1"', "<v>0</v>")]]},
                        date1904=True)
        table, = from_excel(path)
        assert table.rows == [[datetime.datetime(1904, 1, 1)]]

    def test_a_built_in_date_format_needs_no_format_code(self, tmp_path):
        # numFmtId 14 is Excel's own short date and has no formatCode in the
        # file at all, so a reader that only scans format codes misses it.
        path = workbook(tmp_path / "b.xlsx", {
            "S": [["d"], [Raw(' s="2"', "<v>45306</v>")]]}, styles=False)
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr("xl/styles.xml", (
                '<styleSheet xmlns="http://schemas.openxmlformats.org/'
                'spreadsheetml/2006/main"><cellXfs count="3">'
                '<xf numFmtId="0"/><xf numFmtId="0"/><xf numFmtId="14"/>'
                "</cellXfs></styleSheet>"))
        table, = from_excel(path)
        assert table.rows == [[datetime.datetime(2024, 1, 15)]]

    def test_what_counts_as_a_date_format(self):
        assert _is_a_date(r"yyyy\-mm\-dd")
        assert _is_a_date("d/m/yy h:mm")
        assert _is_a_date("[$-409]mmmm d, yyyy")
        # Nothing here names a field, however many letters it holds.
        assert not _is_a_date("General")
        assert not _is_a_date("#,##0.00")
        assert not _is_a_date('0.0" metres"')
        assert not _is_a_date(r"0.00\d")
        assert not _is_a_date(None)


class TestWhatItRefuses:
    def test_a_value_past_the_last_heading(self, tmp_path):
        # There is no name for it, so no query could ever reach it. Dropping
        # it is the silent loss this exists to stop; the message names the
        # column so the sheet can be fixed.
        path = workbook(tmp_path / "b.xlsx",
                        {"S": [["a", "b"], [1, 2, "stray"]]})
        with pytest.raises(SourceError, match="column C"):
            from_excel(path)

    def test_two_headings_of_one_name(self, tmp_path):
        path = workbook(tmp_path / "b.xlsx",
                        {"S": [["id", "Id"], [1, 2]]})
        with pytest.raises(SourceError, match="more than once"):
            from_excel(path)

    def test_a_heading_that_is_blank(self, tmp_path):
        path = workbook(tmp_path / "b.xlsx",
                        {"S": [["id", None, "b"], [1, 2, 3]]})
        with pytest.raises(SourceError):
            from_excel(path)

    def test_something_that_is_not_a_workbook(self, tmp_path):
        path = tmp_path / "old.xls"
        path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 40)
        with pytest.raises(SourceError, match=r"\.xls"):
            from_excel(path)

    def test_a_file_that_is_not_there(self, tmp_path):
        with pytest.raises(SourceError, match="could not read"):
            from_excel(tmp_path / "nothing.xlsx")

    def test_a_zip_with_no_workbook_in_it(self, tmp_path):
        path = tmp_path / "b.xlsx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("hello.txt", "not a workbook")
        with pytest.raises(SourceError, match="not a workbook"):
            from_excel(path)

    def test_a_sheet_that_is_not_there(self, tmp_path):
        path = workbook(tmp_path / "b.xlsx", {"S": [["a"], [1]]})
        with pytest.raises(SourceError, match="no sheet called"):
            from_excel(path, sheet="Missing")


class TestATabThatCannotBeRead:
    """Left out with its reason, rather than refusing everything beside it.

    One tab with a stray value used to refuse the whole workbook, including
    every sheet in it that read perfectly. A workbook Excel saved for another
    project was refused for a note in column L of a tab nobody had asked for.
    """

    def test_it_is_left_out_and_the_rest_is_served(self, tmp_path, caplog):
        path = workbook(tmp_path / "b.xlsx",
                        {"Good": [["id"], [1]],
                         "Analytics": [["a", "b"], [1, 2, "stray"]]})
        with caplog.at_level("WARNING", logger="pysqlbridge.workbook"):
            found = from_excel(path)
        assert [one.name for one in found] == ["Good"]
        # Where the stray value is, so the tab can be fixed, and what became
        # of the rest, so nobody wonders whether anything was served.
        assert "column C" in caplog.text
        assert "the rest of the workbook is served" in caplog.text

    def test_named_explicitly_it_still_says_why(self, tmp_path):
        # Somebody who asked for that tab is owed the reason, not a log line.
        path = workbook(tmp_path / "b.xlsx",
                        {"Good": [["id"], [1]],
                         "Analytics": [["a", "b"], [1, 2, "stray"]]})
        with pytest.raises(SourceError, match="column C"):
            from_excel(path, sheet="Analytics")

    def test_nothing_left_is_refused_with_every_reason(self, tmp_path):
        # "No sheet with anything on it" would be untrue: both have plenty.
        path = workbook(tmp_path / "b.xlsx",
                        {"A": [["a", "b"], [1, 2, "stray"]],
                         "B": [["id", "Id"], [1, 2]]})
        with pytest.raises(SourceError,
                           match="nothing that can be served") as refused:
            from_excel(path)
        assert "column C" in str(refused.value)
        assert "more than once" in str(refused.value)

    def test_a_cell_that_cannot_be_read_still_refuses_the_file(self, tmp_path):
        # Only the layout is forgiven. Day 60 is Excel's 29 February 1900,
        # which did not happen, and a file holding it is damaged rather than
        # laid out oddly, so nothing in it is served on trust.
        path = workbook(tmp_path / "b.xlsx",
                        {"Good": [["id"], [1]],
                         "Bad": [["d"], [Raw(' s="1"', "<v>60</v>")]]})
        with pytest.raises(SourceError, match="1900-02-29"):
            from_excel(path)


class TestNamingTheTables:
    def test_each_sheet_is_called_after_its_tab(self, tmp_path):
        path = workbook(tmp_path / "b.xlsx",
                        {"People": [["a"], [1]], "Budget": [["b"], [2]]})
        assert [one.name for one in from_excel(path)] == ["People", "Budget"]

    def test_sheets_keep_the_workbook_order(self, tmp_path):
        # Not the order of the parts inside the zip, which is the order the
        # tabs were made in rather than the order they are shown in.
        path = workbook(tmp_path / "b.xlsx",
                        {"Z": [["a"], [1]], "A": [["b"], [2]]})
        assert [one.name for one in from_excel(path)] == ["Z", "A"]

    def test_one_sheet_can_be_picked_out(self, tmp_path):
        path = workbook(tmp_path / "b.xlsx",
                        {"People": [["a"], [1]], "Budget": [["b"], [2]]})
        table, = from_excel(path, sheet="budget")
        assert table.name == "Budget" and table.column_names == ["b"]

    def test_a_named_single_sheet_takes_the_name(self, tmp_path):
        path = workbook(tmp_path / "b.xlsx", {"Sheet1": [["a"], [1]]})
        table, = from_excel(path, name="things")
        assert table.name == "things"

    def test_naming_a_workbook_of_several_sheets_is_refused(self, tmp_path):
        # There is one name and four tables, so three of them would have to
        # be called something this invented.
        path = workbook(tmp_path / "b.xlsx",
                        {"One": [["a"], [1]], "Two": [["b"], [2]]})
        with pytest.raises(SourceError, match='"sheet"'):
            from_excel(path, name="things")


class TestTheReaderItself:
    def test_column_letters(self):
        assert [_letters(n) for n in (0, 25, 26, 27, 701, 702)] == [
            "A", "Z", "AA", "AB", "ZZ", "AAA"]

    def test_a_serial_is_the_day_excel_means(self):
        assert serial(datetime.datetime(2024, 1, 15)) == 45306

    def test_sheets_answers_what_it_read(self, tmp_path):
        # A number arrives as the text the file spelled it with, and the
        # column type is decided afterwards by the same inference a CSV goes
        # through. That is what keeps a column of counts an integer column
        # rather than a float column full of values ending .0.
        path = workbook(tmp_path / "b.xlsx", {"S": [["a", "b"], [1, "x"]]})
        found, = sheets(path)
        assert found.name == "S"
        assert found.headers == ["a", "b"]
        assert found.rows == [["1", "x"]]


class TestThroughTheCatalog:
    def test_a_workbook_is_queried_like_anything_else(self, tmp_path):
        from pysqlbridge.catalog import Catalog
        from pysqlbridge.tds.result import Query

        path = workbook(tmp_path / "b.xlsx", {
            "People": [["id", "name", "hired"],
                       [1, "ada", datetime.datetime(2024, 1, 15)],
                       [2, "grace", datetime.datetime(2023, 6, 1)]],
        })
        catalog = Catalog()
        for table in from_excel(path):
            catalog.add(table)

        answer = catalog.answer(Query(
            sql="SELECT name FROM People WHERE hired > '2023-07-01'",
            parameters={}, session={}))
        assert [list(row) for row in answer.rows] == [["ada"]]

    def test_a_date_column_is_declared_as_one(self, tmp_path):
        # A client reads INFORMATION_SCHEMA to learn the types before it
        # reads a row, and being told nvarchar for a column the wire sends as
        # a datetime is the two answers disagreeing.
        from pysqlbridge.catalog import Catalog
        from pysqlbridge.tds.result import Query

        path = workbook(tmp_path / "b.xlsx", {
            "S": [["when"], [datetime.datetime(2024, 1, 15)]]})
        catalog = Catalog()
        for table in from_excel(path):
            catalog.add(table)

        answer = catalog.answer(Query(
            sql="SELECT DATA_TYPE, DATETIME_PRECISION FROM "
                "INFORMATION_SCHEMA.COLUMNS WHERE COLUMN_NAME = 'when'",
            parameters={}, session={}))
        # Measured on SQL Server 2025: a datetime reports precision 3 and
        # leaves every other field describing the type empty.
        assert [list(row) for row in answer.rows] == [["datetime", 3]]


class TestConfiguredWorkbooks:
    def test_a_workbook_named_in_a_config(self, tmp_path):
        import json

        from pysqlbridge.catalog import load

        workbook(tmp_path / "book.xlsx",
                 {"People": [["id"], [1]], "Budget": [["q"], ["Q1"]]})
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"tables": [{"excel": "book.xlsx"}]}),
                          encoding="utf-8")
        catalog = load(config)
        assert sorted(catalog.sources) == ["budget", "people"]

    def test_one_sheet_named_in_a_config(self, tmp_path):
        import json

        from pysqlbridge.catalog import load

        workbook(tmp_path / "book.xlsx",
                 {"People": [["id"], [1]], "Budget": [["q"], ["Q1"]]})
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"tables": [
            {"excel": "book.xlsx", "sheet": "Budget", "name": "money"}]}),
            encoding="utf-8")
        catalog = load(config)
        assert list(catalog.sources) == ["money"]

    def test_a_sheet_named_beside_something_that_has_no_sheets(self, tmp_path):
        import json

        from pysqlbridge.catalog import load

        (tmp_path / "d.csv").write_text("a\n1\n", encoding="utf-8")
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"tables": [
            {"csv": "d.csv", "sheet": "Budget"}]}), encoding="utf-8")
        with pytest.raises(SourceError, match='only a "excel" table has'):
            load(config)

    def test_a_table_named_in_a_config(self, tmp_path):
        import json

        from pysqlbridge.catalog import load

        workbook(tmp_path / "book.xlsx",
                 {"People": [["Staff, Q3"], []] + [["id"], [1]]},
                 tables={"People": [Table("Roster", "A3:A4", ["id"])]})
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"tables": [
            {"excel": "book.xlsx", "table": "Roster"}]}), encoding="utf-8")
        catalog = load(config)
        assert list(catalog.sources) == ["roster"]

    def test_several_sheets_named_in_a_config(self, tmp_path):
        import json

        from pysqlbridge.catalog import load

        workbook(tmp_path / "book.xlsx", {"A": [["a"], [1]], "B": [["b"], [2]],
                                          "C": [["c"], [3]]})
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"tables": [
            {"excel": "book.xlsx", "sheet": ["A", "C"]}]}), encoding="utf-8")
        catalog = load(config)
        assert sorted(catalog.sources) == ["a", "c"]

    def test_a_table_named_beside_something_that_has_no_tables(self, tmp_path):
        # The message names both kinds that do, because "table" now belongs
        # to two of them.
        import json

        from pysqlbridge.catalog import load

        (tmp_path / "d.csv").write_text("a\n1\n", encoding="utf-8")
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"tables": [
            {"csv": "d.csv", "table": "Budget"}]}), encoding="utf-8")
        with pytest.raises(SourceError,
                           match='only "excel" or "access" tables have'):
            load(config)

    def test_a_table_named_like_its_own_tab(self, tmp_path):
        # Naming the table after the sheet is what people do, and it would
        # otherwise be two sources of one name, which the catalog refuses.
        # The table wins and the tab is not served separately, so a workbook
        # laid out this way still loads.
        import json

        from pysqlbridge.catalog import load

        workbook(tmp_path / "book.xlsx",
                 {"Rooms": [["id"], [1], [], ["a note"]]},
                 tables={"Rooms": [Table("Rooms", "A1:A2", ["id"])]})
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"tables": [{"excel": "book.xlsx"}]}),
                          encoding="utf-8")
        catalog = load(config)
        assert list(catalog.sources) == ["rooms"]
        # The table's rows, so the note under it is not one.
        assert catalog.tables["rooms"].rows == [[1]]


class TestWhatASecondHuntFound:
    """A file nobody here wrote, and a limit that was only half applied."""

    MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    RELATIONSHIPS = ("http://schemas.openxmlformats.org/officeDocument/"
                     "2006/relationships")
    PACKAGE = "http://schemas.openxmlformats.org/package/2006/relationships"

    def one_sheet(self, path, sheet_data):
        """A package holding exactly the sheetData given, written by hand.

        The builder writes rows in order, which is the whole point of these:
        Excel does too, and a producer that is not Excel need not.
        """
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("xl/workbook.xml",
                             f'<workbook xmlns="{self.MAIN}" '
                             f'xmlns:r="{self.RELATIONSHIPS}"><sheets>'
                             f'<sheet name="S" sheetId="1" r:id="rId1"/>'
                             f"</sheets></workbook>")
            archive.writestr("xl/_rels/workbook.xml.rels",
                             f'<Relationships xmlns="{self.PACKAGE}">'
                             f'<Relationship Id="rId1" '
                             f'Type="{self.RELATIONSHIPS}/worksheet" '
                             f'Target="worksheets/sheet1.xml"/></Relationships>')
            archive.writestr("xl/worksheets/sheet1.xml",
                             f'<worksheet xmlns="{self.MAIN}"><sheetData>'
                             f"{sheet_data}</sheetData></worksheet>")
        return path

    def test_rows_are_read_in_the_order_they_say_they_are_in(self, tmp_path):
        # Not the order they were written. A file whose rows ran 3, 1, 2 took
        # the third row as the heading and served a column called '3', which
        # is the wrong answer with nothing to say so.
        path = self.one_sheet(tmp_path / "b.xlsx", (
            '<row r="3"><c r="A3"><v>3</v></c></row>'
            '<row r="1"><c r="A1" t="str"><v>id</v></c></row>'
            '<row r="2"><c r="A2"><v>2</v></c></row>'
        ))
        table, = from_excel(path)
        assert table.column_names == ["id"]
        assert table.rows == [[2], [3]]

    def test_a_row_with_no_number_follows_the_one_before_it(self, tmp_path):
        path = self.one_sheet(tmp_path / "b.xlsx", (
            '<row r="1"><c r="A1" t="str"><v>id</v></c></row>'
            '<row><c r="A2"><v>2</v></c></row>'
            '<row r="3"><c r="A3"><v>3</v></c></row>'
        ))
        table, = from_excel(path)
        assert table.column_names == ["id"] and table.rows == [[2], [3]]

    def test_rows_already_in_order_are_left_alone(self, tmp_path):
        # The ordinary file, which must not be sorted into a different one.
        path = workbook(tmp_path / "b.xlsx",
                        {"S": [["id"], [3], [1], [2]]})
        table, = from_excel(path)
        assert table.rows == [[3], [1], [2]]

    def test_a_sheet_wider_than_a_result_set_can_carry(self, tmp_path):
        # Measured nowhere: 1024 is what this serves, and the check lived
        # only where a flattened API response is built, so a sheet went
        # straight past it and made a table no client can hold a row of.
        wide = [[f"c{n}" for n in range(MAX_COLUMNS + 1)],
                list(range(MAX_COLUMNS + 1))]
        path = workbook(tmp_path / "b.xlsx", {"S": wide})
        with pytest.raises(SourceError, match=str(MAX_COLUMNS)):
            from_excel(path)

    def test_a_sheet_at_the_limit_is_served(self, tmp_path):
        wide = [[f"c{n}" for n in range(MAX_COLUMNS)],
                list(range(MAX_COLUMNS))]
        path = workbook(tmp_path / "b.xlsx", {"S": wide})
        table, = from_excel(path)
        assert len(table.columns) == MAX_COLUMNS


class TestATableOnASheet:
    """What Excel calls a table and the object model calls a ListObject.

    It is not in the sheet. The sheet points at a part of its own holding the
    range covered and the column names, so a reader of <sheetData> alone
    cannot see one, and before this every layout below was either refused or
    served wrong.
    """

    ROOMS = [["code", "seats"], ["A1", 30], ["B2", 12]]

    def test_a_table_is_served_beside_the_sheet_it_sits_on(self, tmp_path):
        # Two names for the same rows, and both work. The tab is what a
        # person sees along the bottom; the table is what they named.
        path = workbook(tmp_path / "b.xlsx", {"S": self.ROOMS},
                        tables={"S": [Table("Rooms", "A1:B3",
                                            ["code", "seats"])]})
        sheet, table = from_excel(path)
        assert [sheet.name, table.name] == ["S", "Rooms"]
        assert sheet.rows == table.rows == [["A1", 30], ["B2", 12]]

    def test_a_table_is_named_after_itself(self, tmp_path):
        # Not after its tab. The name is the one thing a table has that a
        # sheet does not, so serving it under the tab's name throws away the
        # only name a query was ever going to be written with.
        path = workbook(tmp_path / "b.xlsx", {"Sheet1": self.ROOMS},
                        tables={"Sheet1": [Table("Rooms", "A1:B3",
                                                 ["code", "seats"])]})
        assert [one.name for one in from_excel(path)] == ["Sheet1", "Rooms"]

    def test_a_table_under_a_title_is_served(self, tmp_path):
        # The commonest layout there is, and the sheet reading of it has
        # never worked: the title is the first row, so it becomes the header
        # and everything under it is past the last heading.
        path = workbook(tmp_path / "b.xlsx",
                        {"S": [["Room bookings, Q3"], []] + self.ROOMS},
                        tables={"S": [Table("Rooms", "A3:B5",
                                            ["code", "seats"])]})
        table, = from_excel(path)
        assert table.name == "Rooms"
        assert table.rows == [["A1", 30], ["B2", 12]]

    def test_one_sheet_that_cannot_be_read_no_longer_sinks_the_workbook(
            self, tmp_path):
        # It used to. A title over a table on the fourth tab refused the
        # whole file, including three sheets that read perfectly.
        path = workbook(
            tmp_path / "b.xlsx",
            {"Good": [["id"], [1]], "Report": [["Bookings"], []] + self.ROOMS},
            tables={"Report": [Table("Rooms", "A3:B5", ["code", "seats"])]},
        )
        assert [one.name for one in from_excel(path)] == ["Good", "Rooms"]

    def test_a_table_off_column_a_does_not_sink_the_workbook(self, tmp_path):
        # Found in a workbook Excel saved rather than imagined: a table drawn
        # at D1 leaves the tab's own reading with three columns that have no
        # heading. Those are refused where the names are checked, which is
        # further along than the tolerance for a tab reached, so the whole
        # workbook was refused for want of names nobody had asked for.
        path = workbook(
            tmp_path / "b.xlsx",
            {"Good": [["id"], [1]],
             "Users": [[None, None, None, "code", "seats"],
                       [None, None, None, "A1", 30]]},
            tables={"Users": [Table("UsersTable", "D1:E2",
                                    ["code", "seats"])]},
        )
        assert [one.name for one in from_excel(path)] == ["Good", "UsersTable"]

    def test_a_note_beside_a_table_does_not_sink_the_workbook(self, tmp_path):
        # The other shape of the same thing, also from a real workbook: a
        # table in A:B and a note in D, so the tab reads with a column C that
        # has no heading.
        path = workbook(
            tmp_path / "b.xlsx",
            {"S": [["code", "seats", None, "checked by"],
                   ["A1", 30, None, "ada"]]},
            tables={"S": [Table("Rooms", "A1:B2", ["code", "seats"])]},
        )
        assert [one.name for one in from_excel(path)] == ["Rooms"]

    def test_a_tab_whose_table_ends_in_a_totals_row_is_not_served(
            self, tmp_path, caplog):
        # Measured on a workbook Excel wrote: the tab read whole served the
        # totals row as a record called Total, so a count over it was one
        # too many and a sum over seats counted every seat twice.
        path = workbook(tmp_path / "b.xlsx",
                        {"Totals": self.ROOMS + [["Total", 42]]},
                        tables={"Totals": [Table("Rooms", "A1:B4",
                                                 ["code", "seats"],
                                                 totals_rows=1)]})
        with caplog.at_level("INFO", logger="pysqlbridge.workbook"):
            found = from_excel(path)
        assert [one.name for one in found] == ["Rooms"]
        assert found[0].rows == [["A1", 30], ["B2", 12]]
        assert "totals row" in caplog.text

    def test_a_tab_whose_table_has_no_headings_is_not_served(self, tmp_path):
        # Also measured: with the header row switched off, Excel empties the
        # row above the table, and the tab read whole took the first record
        # for its headings, serving columns called A1 and 30.
        path = workbook(tmp_path / "b.xlsx",
                        {"NoHeader": [[], ["A1", 30], ["B2", 12]]},
                        tables={"NoHeader": [Table("Bare", "A2:B3",
                                                   ["code", "seats"],
                                                   header_rows=0)]})
        found = from_excel(path)
        assert [one.name for one in found] == ["Bare"]
        assert found[0].column_names == ["code", "seats"]

    def test_naming_the_tab_serves_it_whatever_its_table_says(self, tmp_path):
        # The rules protect a tab nobody asked for. Somebody who named it
        # gets it as it reads, totals row and all.
        path = workbook(tmp_path / "b.xlsx",
                        {"Totals": self.ROOMS + [["Total", 42]]},
                        tables={"Totals": [Table("Rooms", "A1:B4",
                                                 ["code", "seats"],
                                                 totals_rows=1)]})
        found = from_excel(path, sheet="Totals")
        assert [one.name for one in found] == ["Totals"]
        assert found[0].rows == [["A1", 30], ["B2", 12], ["Total", 42]]

    def test_a_sheet_carrying_two_tables_is_not_itself_a_table(self, tmp_path):
        # It has two header rows. There is no reading of it as one table, and
        # the file says so, so the sheet is left out and its tables serve.
        path = workbook(
            tmp_path / "b.xlsx",
            {"S": [["code", "seats", None, "day", "booked"],
                   ["A1", 30, None, "Mon", 4],
                   ["B2", 12, None, "Tue", 7]]},
            tables={"S": [Table("Rooms", "A1:B3", ["code", "seats"]),
                          Table("Bookings", "D1:E3", ["day", "booked"])]},
        )
        assert [one.name for one in from_excel(path)] == ["Rooms", "Bookings"]

    def test_a_second_table_does_not_become_records_of_the_first(self, tmp_path):
        # The one that was silently wrong. Stacked tables read as one sheet
        # put the second table's headings in as a record and dragged its
        # numbers over to text with them, so a count was out by the number of
        # tables and a sum was against an nvarchar column.
        path = workbook(
            tmp_path / "b.xlsx",
            {"S": [["code", "seats"], ["A1", 30], [], ["day", "booked"],
                   ["Mon", 4]]},
            tables={"S": [Table("Rooms", "A1:B2", ["code", "seats"]),
                          Table("Bookings", "A4:B5", ["day", "booked"])]},
        )
        rooms, bookings = from_excel(path)
        assert rooms.rows == [["A1", 30]]
        assert isinstance(rooms.columns[1].type, Integer)
        assert bookings.rows == [["Mon", 4]]
        assert isinstance(bookings.columns[1].type, Integer)

    def test_a_totals_row_is_not_a_record(self, tmp_path):
        # The range covers it, so slicing on the range alone gives a workbook
        # of two rooms three of them, one called Total.
        path = workbook(
            tmp_path / "b.xlsx",
            {"S": self.ROOMS + [["Total", 42]]},
            tables={"S": [Table("Rooms", "A1:B4", ["code", "seats"],
                                totals_rows=1)]},
        )
        table, = from_excel(path, table="Rooms")
        assert table.rows == [["A1", 30], ["B2", 12]]

    def test_a_table_with_no_header_row_takes_its_names_from_its_part(
            self, tmp_path):
        # headerRowCount="0" is legal, and then there is no header row on the
        # sheet at all. The names are in the table part either way, which is
        # why they are read from there rather than off the cells.
        path = workbook(
            tmp_path / "b.xlsx",
            {"S": [["A1", 30], ["B2", 12]]},
            tables={"S": [Table("Rooms", "A1:B2", ["code", "seats"],
                                header_rows=0)]},
        )
        table, = from_excel(path, table="Rooms")
        assert table.column_names == ["code", "seats"]
        assert table.rows == [["A1", 30], ["B2", 12]]

    def test_a_table_holds_only_the_columns_it_covers(self, tmp_path):
        # A note typed beside a table is not in the table. This is most of
        # the reason a table is worth serving apart from its sheet.
        path = workbook(
            tmp_path / "b.xlsx",
            {"S": [["code", "seats", None, "checked by"],
                   ["A1", 30, None, "ada"]]},
            tables={"S": [Table("Rooms", "A1:B2", ["code", "seats"])]},
        )
        table, = from_excel(path, table="Rooms")
        assert table.column_names == ["code", "seats"]
        assert table.rows == [["A1", 30]]

    def test_a_table_holds_only_the_rows_it_covers(self, tmp_path):
        path = workbook(
            tmp_path / "b.xlsx",
            {"S": self.ROOMS + [[], ["a note about the above"]]},
            tables={"S": [Table("Rooms", "A1:B3", ["code", "seats"])]},
        )
        table, = from_excel(path, table="Rooms")
        assert table.rows == [["A1", 30], ["B2", 12]]

    def test_which_rows_are_inside_comes_from_their_numbers(self, tmp_path):
        # Not from their positions. Excel writes no row at all for a blank
        # one, so a gap above a table makes the two disagree, and counting
        # positions would take the wrong slice with nothing to show for it.
        path = workbook(
            tmp_path / "b.xlsx",
            {"S": [[], [], ["code", "seats"], ["A1", 30], ["B2", 12]]},
            tables={"S": [Table("Rooms", "A3:B5", ["code", "seats"])]},
        )
        table, = from_excel(path, table="Rooms")
        assert table.rows == [["A1", 30], ["B2", 12]]

    def test_a_blank_row_inside_a_table_is_not_a_record(self, tmp_path):
        # The same rule the sheet reading uses. A row of nulls is not
        # something anybody put there.
        path = workbook(
            tmp_path / "b.xlsx",
            {"S": [["code", "seats"], ["A1", 30], [], ["B2", 12]]},
            tables={"S": [Table("Rooms", "A1:B4", ["code", "seats"])]},
        )
        table, = from_excel(path, table="Rooms")
        assert table.rows == [["A1", 30], ["B2", 12]]

    def test_a_one_cell_table_covers_one_cell(self, tmp_path):
        # A ref can be a single reference rather than a range.
        path = workbook(tmp_path / "b.xlsx", {"S": [["code"], ["A1"]]},
                        tables={"S": [Table("Rooms", "A1:A2", ["code"])]})
        table, = from_excel(path, table="Rooms")
        assert table.rows == [["A1"]]

    def test_a_table_of_the_tab_s_own_name_replaces_it(self, tmp_path):
        # Naming the table after the sheet is what people do with one table
        # on one tab, and it would otherwise be one name for two tables. The
        # table wins: it knows its own columns and where it stops.
        path = workbook(tmp_path / "b.xlsx",
                        {"Rooms": self.ROOMS + [[], ["counted 3 Sept"]]},
                        tables={"Rooms": [Table("Rooms", "A1:B3",
                                                ["code", "seats"])]})
        table, = from_excel(path)
        assert table.name == "Rooms"
        assert table.rows == [["A1", 30], ["B2", 12]]

    def test_the_match_that_replaces_a_tab_ignores_case(self, tmp_path):
        # The catalog folds case, so ROOMS and Rooms would collide there.
        path = workbook(tmp_path / "b.xlsx", {"ROOMS": self.ROOMS},
                        tables={"ROOMS": [Table("Rooms", "A1:B3",
                                                ["code", "seats"])]})
        assert [one.name for one in from_excel(path)] == ["Rooms"]

    def test_the_log_says_why_a_tab_is_not_there(self, tmp_path, caplog):
        # Otherwise a client asks for the tab, is told there is no such
        # object, and nothing anywhere says what happened to it.
        path = workbook(
            tmp_path / "b.xlsx",
            {"S": [["code", "seats"], ["A1", 30], [], ["day", "booked"],
                   ["Mon", 4]]},
            tables={"S": [Table("Rooms", "A1:B2", ["code", "seats"]),
                          Table("Bookings", "A4:B5", ["day", "booked"])]},
        )
        with caplog.at_level("INFO", logger="pysqlbridge.workbook"):
            from_excel(path)
        assert "'Rooms', 'Bookings'" in caplog.text
        assert "no single row of headings" in caplog.text

    def test_the_log_says_when_a_table_took_its_tab_s_name(self, tmp_path,
                                                           caplog):
        path = workbook(tmp_path / "b.xlsx", {"Rooms": self.ROOMS},
                        tables={"Rooms": [Table("Rooms", "A1:B3",
                                                ["code", "seats"])]})
        with caplog.at_level("INFO", logger="pysqlbridge.workbook"):
            from_excel(path)
        assert "table of the same name" in caplog.text

    def test_the_log_says_when_a_sheet_could_not_be_read(self, tmp_path,
                                                         caplog):
        path = workbook(tmp_path / "b.xlsx",
                        {"S": [["Room bookings, Q3"], []] + self.ROOMS},
                        tables={"S": [Table("Rooms", "A3:B5",
                                            ["code", "seats"])]})
        with caplog.at_level("WARNING", logger="pysqlbridge.workbook"):
            from_excel(path)
        # The reason first, so somebody looking for the tab knows what to
        # change on it rather than only that it is gone.
        assert "past the last heading in column A" in caplog.text
        assert "Its table 'Rooms' is served instead." in caplog.text

    def test_tables_come_in_the_order_they_sit_on_the_sheet(self, tmp_path):
        # Related in the order they were made, which is not the order they
        # are read in.
        path = workbook(
            tmp_path / "b.xlsx",
            {"S": [["day", "booked"], ["Mon", 4], [], ["code", "seats"],
                   ["A1", 30]]},
            tables={"S": [Table("Rooms", "A4:B5", ["code", "seats"]),
                          Table("Bookings", "A1:B2", ["day", "booked"])]},
        )
        assert [one.name for one in from_excel(path)] == ["Bookings", "Rooms"]


class TestPickingWhatToServe:
    ROOMS = [["code", "seats"], ["A1", 30]]
    BOOKINGS = [["day", "booked"], ["Mon", 4]]

    def built(self, tmp_path):
        return workbook(
            tmp_path / "b.xlsx",
            {"Rooms sheet": self.ROOMS, "Bookings sheet": self.BOOKINGS},
            tables={"Rooms sheet": [Table("Rooms", "A1:B2",
                                          ["code", "seats"])],
                    "Bookings sheet": [Table("Bookings", "A1:B2",
                                             ["day", "booked"])]},
        )

    def test_everything_by_default(self, tmp_path):
        assert [one.name for one in from_excel(self.built(tmp_path))] == [
            "Rooms sheet", "Rooms", "Bookings sheet", "Bookings"]

    def test_a_named_table_alone(self, tmp_path):
        found = from_excel(self.built(tmp_path), table="Rooms")
        assert [one.name for one in found] == ["Rooms"]

    def test_naming_tables_serves_no_sheets(self, tmp_path):
        # Which is how to say "the tables, not the tabs they sit on".
        found = from_excel(self.built(tmp_path), table=["Rooms", "Bookings"])
        assert [one.name for one in found] == ["Rooms", "Bookings"]

    def test_a_named_sheet_alone(self, tmp_path):
        found = from_excel(self.built(tmp_path), sheet="Rooms sheet")
        assert [one.name for one in found] == ["Rooms sheet"]

    def test_both_together_serve_both(self, tmp_path):
        found = from_excel(self.built(tmp_path), sheet="Rooms sheet",
                           table="Bookings")
        assert [one.name for one in found] == ["Rooms sheet", "Bookings"]

    def test_several_sheets_by_name(self, tmp_path):
        # A workbook of twelve tabs that a person wants three of should not
        # need its path written three times.
        found = from_excel(self.built(tmp_path),
                           sheet=["Bookings sheet", "Rooms sheet"])
        assert [one.name for one in found] == ["Rooms sheet", "Bookings sheet"]

    def test_a_name_is_matched_whatever_its_case(self, tmp_path):
        found = from_excel(self.built(tmp_path), table="rOOms")
        assert [one.name for one in found] == ["Rooms"]

    def test_a_table_that_is_not_there_says_what_is(self, tmp_path):
        with pytest.raises(SourceError, match="'Rooms', 'Bookings'"):
            from_excel(self.built(tmp_path), table="Guests")

    def test_a_sheet_that_is_not_there_says_what_is(self, tmp_path):
        with pytest.raises(SourceError, match="'Rooms sheet'"):
            from_excel(self.built(tmp_path), sheet="Guests")

    def test_asking_for_a_table_where_there_are_none(self, tmp_path):
        path = workbook(tmp_path / "b.xlsx", {"S": self.ROOMS})
        with pytest.raises(SourceError, match="it has no tables at all"):
            from_excel(path, table="Rooms")

    def test_naming_a_sheet_gets_past_the_ceiling_on_sheets(self, tmp_path):
        # The ceiling used to be checked over the whole workbook before the
        # name was applied, so a document of too many sheets refused with a
        # message saying to name the one wanted, and naming it refused in
        # exactly the same way.
        many = {f"S{n}": [["id"], [n]] for n in range(MAX_SHEETS + 1)}
        path = workbook(tmp_path / "b.xlsx", many)
        with pytest.raises(SourceError, match=str(MAX_SHEETS)):
            from_excel(path)
        table, = from_excel(path, sheet="S7")
        assert table.name == "S7" and table.rows == [[7]]

    def test_a_name_nobody_can_find_is_shown_as_it_was_typed(self, tmp_path):
        # Matching folds case, so reporting the folded name would answer a
        # typo in 'Guest_List' with a complaint about 'guest_list'.
        with pytest.raises(SourceError, match="'Guest_List'"):
            from_excel(self.built(tmp_path), table="Guest_List")

    def test_a_named_sheet_is_served_though_it_holds_two_tables(self, tmp_path):
        # An explicit name beats the rule that protects the unnamed. Somebody
        # who asked for that sheet wants that sheet.
        path = workbook(
            tmp_path / "b.xlsx",
            {"S": [["code", "seats"], ["A1", 30], ["B2", 12]]},
            tables={"S": [Table("Rooms", "A1:B2", ["code", "seats"]),
                          Table("Two", "A3:B3", ["code", "seats"],
                                header_rows=0)]},
        )
        found = from_excel(path, sheet="S")
        assert [one.name for one in found] == ["S"]
        assert found[0].rows == [["A1", 30], ["B2", 12]]

    def test_a_named_sheet_that_cannot_be_read_still_raises(self, tmp_path):
        # Quietly skipping it is for the sheet nobody asked about. Somebody
        # who named it is owed the reason.
        path = workbook(tmp_path / "b.xlsx",
                        {"S": [["Title"], []] + self.ROOMS},
                        tables={"S": [Table("Rooms", "A3:B4",
                                            ["code", "seats"])]})
        with pytest.raises(SourceError, match="past the last heading"):
            from_excel(path, sheet="S")


class TestATableThisCannotRead:
    def test_a_table_part_that_is_not_in_the_package(self, tmp_path):
        path = workbook(tmp_path / "b.xlsx", {"S": [["a"], [1]]},
                        tables={"S": [Table("T", "A1:A2", ["a"])]})
        with zipfile.ZipFile(path) as archive:
            kept = {name: archive.read(name) for name in archive.namelist()
                    if "tables/" not in name}
        with zipfile.ZipFile(path, "w") as archive:
            for name, body in kept.items():
                archive.writestr(name, body)
        with pytest.raises(SourceError, match="does not contain"):
            from_excel(path)

    def test_a_table_naming_more_columns_than_it_covers(self, tmp_path):
        path = workbook(tmp_path / "b.xlsx", {"S": [["a"], [1]]},
                        tables={"S": [Table("T", "A1:A2", ["a", "b"])]})
        with pytest.raises(SourceError, match="covers 1 columns and names 2"):
            from_excel(path)

    def test_a_table_with_no_name(self, tmp_path):
        path = workbook(tmp_path / "b.xlsx", {"S": [["a"], [1]]},
                        tables={"S": [Table("", "A1:A2", ["a"])]})
        with pytest.raises(SourceError, match="table with no name"):
            from_excel(path)

    def test_a_range_that_runs_backwards(self, tmp_path):
        path = workbook(tmp_path / "b.xlsx", {"S": [["a"], [1]]},
                        tables={"S": [Table("T", "A9:A2", ["a"])]})
        with pytest.raises(SourceError, match="runs backwards"):
            from_excel(path)

    def test_a_range_naming_no_row(self, tmp_path):
        path = workbook(tmp_path / "b.xlsx", {"S": [["a"], [1]]},
                        tables={"S": [Table("T", "A:A", ["a"])]})
        with pytest.raises(SourceError, match="names no row"):
            from_excel(path)

    def test_a_count_that_is_not_a_number(self, tmp_path):
        path = workbook(tmp_path / "b.xlsx", {"S": [["a"], [1]]},
                        tables={"S": [Table("T", "A1:A2", ["a"])]})
        _rewrite(path, "xl/tables/table1.xml",
                 lambda text: text.replace('ref="A1:A2"',
                                           'ref="A1:A2" headerRowCount="one"'))
        with pytest.raises(SourceError, match="not a number"):
            from_excel(path)

    def test_a_table_that_is_all_heading_and_total(self, tmp_path):
        # One row cannot be both. Two can, and that is an empty table rather
        # than a broken one, which the test below says.
        path = workbook(tmp_path / "b.xlsx", {"S": [["a"], [1]]},
                        tables={"S": [Table("T", "A1:A1", ["a"],
                                            totals_rows=1)]})
        with pytest.raises(SourceError, match="no rows between them"):
            from_excel(path)

    def test_a_table_of_a_heading_and_a_total_is_empty_rather_than_broken(
            self, tmp_path):
        # Excel will happily leave you a table with its columns named, its
        # total showing and nothing between them.
        path = workbook(tmp_path / "b.xlsx", {"S": [["a"], ["Total"]]},
                        tables={"S": [Table("T", "A1:A2", ["a"],
                                            totals_rows=1)]})
        table, = from_excel(path, table="T")
        assert table.column_names == ["a"] and table.rows == []


def _rewrite(path, part, how):
    """Change one part of a written workbook, for shapes the builder will not
    produce because Excel does not produce them either."""
    with zipfile.ZipFile(path) as archive:
        kept = {name: archive.read(name) for name in archive.namelist()}
    kept[part] = how(kept[part].decode("utf-8")).encode("utf-8")
    with zipfile.ZipFile(path, "w") as archive:
        for name, body in kept.items():
            archive.writestr(name, body)
