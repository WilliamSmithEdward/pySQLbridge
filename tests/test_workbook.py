"""Reading a workbook, against what Excel actually writes into one."""

import datetime
import zipfile

import pytest

from pysqlbridge.source import MAX_COLUMNS, SourceError, from_excel
from pysqlbridge.tds.result import DateTime, Float, Integer
from pysqlbridge.workbook import _is_a_date, _letters, sheets

from .workbooks import Error, Formula, Inline, Raw, serial, workbook


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
