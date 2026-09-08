import struct

import pytest

from pysqlbridge.tds import (
    Column,
    Float,
    Integer,
    NVarChar,
    QueryError,
    QueryResult,
    TokenType,
    col_metadata,
    result_set,
    row,
)
from pysqlbridge.tds.result import SELECT_COMMAND

from . import captured as C

# The four columns of the captured result set, in its order.
REFERENCE_COLUMNS = [
    Column("answer", Integer(4)),
    Column("greeting", NVarChar(20)),
    Column("ratio", Float(8)),
    Column("missing", Integer(4)),
]
REFERENCE_VALUES = [42, "hello", 1.5, None]


class TestReproducesTheReference:
    def test_col_metadata(self):
        assert col_metadata(REFERENCE_COLUMNS) == C.COL_METADATA

    def test_row(self):
        assert row(REFERENCE_COLUMNS, REFERENCE_VALUES) == C.RESULT_ROW

    def test_whole_result_set(self):
        assert result_set(REFERENCE_COLUMNS, [REFERENCE_VALUES]) == (
            C.COL_METADATA + C.RESULT_ROW + C.RESULT_DONE
        )

    def test_done_reports_the_row_count_and_select_command(self):
        status, command = struct.unpack_from("<HH", C.RESULT_DONE, 1)
        rows = struct.unpack_from("<Q", C.RESULT_DONE, 5)[0]
        assert command == SELECT_COMMAND == 0x00C1
        assert rows == 1
        assert status & 0x0010  # COUNT: the row count is meaningful


class TestNullEncoding:
    """NULL is not spelled the same way by every type."""

    def test_integer_null_is_a_zero_length(self):
        assert Integer(4).encode(None) == b"\x00"

    def test_float_null_is_a_zero_length(self):
        assert Float(8).encode(None) == b"\x00"

    def test_nvarchar_null_is_ffff_not_zero(self):
        # A zero length here is a legitimate empty string, so the two-byte
        # types spend the whole field on the null marker instead.
        assert NVarChar(10).encode(None) == b"\xff\xff"
        assert NVarChar(10).encode("") == b"\x00\x00"

    def test_null_and_empty_string_are_distinguishable(self):
        assert NVarChar(10).encode(None) != NVarChar(10).encode("")


class TestValueEncoding:
    def test_integer_is_little_endian_and_signed(self):
        assert Integer(4).encode(42) == b"\x04" + struct.pack("<i", 42)
        assert Integer(4).encode(-1) == b"\x04" + b"\xff\xff\xff\xff"

    def test_float_is_ieee754_little_endian(self):
        assert Float(8).encode(1.5) == b"\x08" + struct.pack("<d", 1.5)

    def test_nvarchar_length_counts_bytes_not_characters(self):
        encoded = NVarChar(10).encode("hello")
        assert struct.unpack_from("<H", encoded)[0] == 10  # five characters
        assert encoded[2:] == "hello".encode("utf-16-le")

    def test_nvarchar_metadata_size_is_twice_the_characters(self):
        assert struct.unpack_from("<H", NVarChar(20).type_info(), 1)[0] == 40

    def test_non_ascii_survives(self):
        encoded = NVarChar(10).encode("naiveé")
        assert encoded[2:].decode("utf-16-le") == "naiveé"

    # A byte holds 0 to 255 rather than -128 to 127: one byte of INTN is
    # tinyint, and tinyint is unsigned.
    @pytest.mark.parametrize("width,value", [(1, 256), (2, 70000), (4, 2**31)])
    def test_integer_too_wide_is_refused(self, width, value):
        with pytest.raises(ValueError, match="does not fit"):
            Integer(width).encode(value)

    @pytest.mark.parametrize("width", [0, 3, 5, 16])
    def test_impossible_integer_widths_are_refused(self, width):
        with pytest.raises(ValueError, match="width must be"):
            Integer(width)

    def test_impossible_float_width_is_refused(self):
        with pytest.raises(ValueError, match="width must be"):
            Float(2)


class TestRowShape:
    def test_value_count_must_match_the_columns(self):
        with pytest.raises(ValueError, match="2 values but 4 columns"):
            row(REFERENCE_COLUMNS, [1, 2])

    def test_zero_rows_still_produces_metadata_and_done(self):
        # An empty answer is not a missing one: the client needs the shape.
        stream = result_set(REFERENCE_COLUMNS, [])
        assert stream.startswith(bytes([TokenType.COL_METADATA]))
        assert struct.unpack_from("<Q", stream, len(stream) - 8)[0] == 0

    def test_many_rows_are_counted(self):
        stream = result_set([Column("n", Integer(4))], [[i] for i in range(5)])
        assert stream.count(bytes([TokenType.ROW])) >= 5
        assert struct.unpack_from("<Q", stream, len(stream) - 8)[0] == 5


class TestQueryResult:
    def test_encodes_through_the_same_path(self):
        result = QueryResult(columns=REFERENCE_COLUMNS, rows=[REFERENCE_VALUES])
        assert result.encode() == result_set(REFERENCE_COLUMNS, [REFERENCE_VALUES])


class TestQueryError:
    def test_defaults_to_the_user_defined_range(self):
        # An error raised here is this project's, not SQL Server's.
        assert QueryError("nope").number == 50000

    def test_carries_a_chosen_number(self):
        assert QueryError("no such table", number=208).number == 208


class TestStatementsWithNoResultSet:
    """SET, USE and friends complete without declaring any shape.

    Clients open a session with setup batches before anything the user typed.
    Answering one of those with COLMETADATA makes the client report an invalid
    cursor state on the query it was actually waiting for, which is what
    sqlcmd did when the demo handler answered SET QUOTED_IDENTIFIER OFF with
    four columns of rows.
    """

    def test_no_columns_encodes_as_a_bare_done(self):
        from pysqlbridge.tds import done

        assert result_set([], []) == done()

    def test_no_columns_emits_no_metadata_token(self):
        assert bytes([TokenType.COL_METADATA]) not in result_set([], [])

    def test_no_columns_emits_no_rows(self):
        assert bytes([TokenType.ROW]) not in result_set([], [])

    def test_this_differs_from_a_query_returning_no_rows(self):
        # A SELECT matching nothing still declares its columns.
        empty_select = result_set([Column("n", Integer(4))], [])
        assert empty_select.startswith(bytes([TokenType.COL_METADATA]))
        assert empty_select != result_set([], [])

    def test_a_query_result_with_no_columns_round_trips(self):
        from pysqlbridge.tds import done

        assert QueryResult(columns=[], rows=[]).encode() == done()


class TestColumnNameLength:
    """The wire writes a column name length in one byte."""

    def test_a_long_name_is_refused_by_name(self):
        with pytest.raises(ValueError, match="length field holds 255"):
            col_metadata([Column("x" * 300, Integer(4))])

    def test_255_still_fits(self):
        assert col_metadata([Column("x" * 255, Integer(4))])


class TestTinyIntIsUnsigned:
    """One byte of INTN is tinyint, which runs 0 to 255 rather than -128 up.

    sys.databases reports a compatibility level of 170 in a tinyint column,
    and writing it signed overflowed and took the connection with it.
    """

    def test_a_value_above_a_signed_byte_still_fits(self):
        assert Integer(1).encode(200) == b"\x01\xc8"

    def test_the_whole_range_fits(self):
        assert Integer(1).encode(0) == b"\x01\x00"
        assert Integer(1).encode(255) == b"\x01\xff"

    def test_a_negative_one_does_not(self):
        with pytest.raises(ValueError, match="1-byte integer"):
            Integer(1).encode(-1)

    def test_and_the_wider_ones_are_still_signed(self):
        assert Integer(2).encode(-5) == b"\x02\xfb\xff"
        assert Integer(4).encode(-5) == b"\x04\xfb\xff\xff\xff"

class TestHalfOfASurrogatePair:
    """A code unit with no partner, which nvarchar is allowed to hold.

    nvarchar is a string of UTF-16 code units, not of characters, and JSON
    is allowed to spell half a pair: "\\ud800" decodes to a lone surrogate,
    and an API building its JSON out of UTF-16 by hand serves them. Encoding
    one raised UnicodeEncodeError, which took down the answer to any query
    over a row that held one.

    Measured: SQL Server stores NCHAR(0xD800) as the code unit D800, hands it
    back unchanged, and UNICODE() reads it back as 55296. It neither refuses
    the value nor replaces it, so neither does this.
    """

    def test_the_code_unit_goes_out_as_itself(self):
        # 0200 is the length; 00d8 is D800 little-endian, which is the same
        # two bytes SQL Server stored.
        assert NVarChar(10).encode("\ud800").hex() == "020000d8"

    def test_a_whole_pair_is_unchanged(self):
        assert NVarChar(10).encode("\U0001f600").hex() == "04003dd800de"

    def test_ordinary_text_is_unchanged(self):
        assert NVarChar(10).encode("ab").hex() == "040061006200"

    def test_the_max_form_takes_one_too(self):
        assert NVarChar(None).encode("\ud800")

    def test_and_so_does_a_column_name(self):
        # A name comes from a query or from a source and can hold one.
        assert Column("\ud800", NVarChar(10)).metadata()

    @pytest.mark.parametrize("held", ["\ud800", "\udfff", "a\ud800b"])
    def test_a_row_holding_one_can_be_sent(self, held):
        column = NVarChar(10)
        assert column.encode(held)
