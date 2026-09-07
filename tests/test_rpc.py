import struct

import pytest

from pysqlbridge.tds import ProcId, TdsProtocolError, parse_rpc
from pysqlbridge.tds.rpc import _read_value

from . import captured as C


class TestCapturedTableList:
    """What .NET's GetSchema('Tables') actually sends."""

    def test_is_sp_executesql_by_well_known_id(self):
        call = parse_rpc(C.RPC_TABLE_LIST)
        assert call.proc_id is ProcId.EXECUTE_SQL
        assert call.procedure == "sp_executesql"
        assert call.is_execute_sql

    def test_the_statement_is_the_first_unnamed_parameter(self):
        call = parse_rpc(C.RPC_TABLE_LIST)
        assert call.parameters[0].name == ""
        assert call.sql.startswith("select TABLE_CATALOG")
        assert "INFORMATION_SCHEMA.TABLES" in call.sql

    def test_the_second_parameter_declares_the_rest(self):
        call = parse_rpc(C.RPC_TABLE_LIST)
        assert call.parameters[1].value.startswith("@Catalog nvarchar(4000)")

    def test_the_named_parameters_are_all_null(self):
        call = parse_rpc(C.RPC_TABLE_LIST)
        named = {p.name: p.value for p in call.parameters if p.name}
        assert named == {
            "@Catalog": None, "@Owner": None, "@Name": None, "@TableType": None,
        }


class TestCapturedParameterisedSelect:
    def test_reads_the_statement(self):
        assert parse_rpc(C.RPC_PARAMETERISED_SELECT).sql == (
            "SELECT * FROM people WHERE id = @id"
        )

    def test_reads_an_integer_parameter(self):
        named = {p.name: p.value for p in parse_rpc(C.RPC_PARAMETERISED_SELECT).parameters if p.name}
        assert named == {"@id": 1}

    def test_declares_the_parameter_type(self):
        assert parse_rpc(C.RPC_PARAMETERISED_SELECT).parameters[1].value == "@id int"


class TestErrors:
    def test_too_short_for_a_header_length(self):
        with pytest.raises(TdsProtocolError, match="too short"):
            parse_rpc(b"\x16\x00")

    def test_header_block_longer_than_the_payload(self):
        with pytest.raises(TdsProtocolError, match="does not fit"):
            parse_rpc(struct.pack("<I", 900) + b"\x00" * 10)

    def test_an_unknown_well_known_id(self):
        payload = struct.pack("<I", 4) + struct.pack("<HHH", 0xFFFF, 999, 0)
        with pytest.raises(TdsProtocolError, match="unknown well-known procedure id 999"):
            parse_rpc(payload)

    def test_an_unreadable_parameter_type_stops_parsing(self):
        # The value's length comes from its type, so an unknown type leaves no
        # way to know where the next parameter starts.
        payload = (
            struct.pack("<I", 4)
            + struct.pack("<HHH", 0xFFFF, ProcId.EXECUTE_SQL, 0)
            + bytes([0])          # unnamed
            + bytes([0])          # status
            + bytes([0x99])       # a type this project does not read
        )
        with pytest.raises(TdsProtocolError, match="type 0x99"):
            parse_rpc(payload)


class TestProcedureName:
    def test_a_named_procedure_is_read_as_text(self):
        name = "my_proc"
        payload = (
            struct.pack("<I", 4)
            + struct.pack("<H", len(name)) + name.encode("utf-16-le")
            + struct.pack("<H", 0)
        )
        call = parse_rpc(payload)
        assert call.procedure == "my_proc"
        assert call.proc_id is None
        assert call.is_execute_sql is False
        assert call.sql is None


class TestLongParameters:
    """text, ntext and image, which a client still sends.

    Deprecated for twenty years and not gone: SSMS passes a filter as ntext
    while opening Object Explorer, and a parameter this could not read ended
    the connection rather than the call.
    """

    def test_an_ntext_parameter_from_a_real_client(self):
        call = parse_rpc(C.NTEXT_RPC)
        assert call.sql == "SELECT @p AS v"
        assert call.parameters[-1].name == "@p"
        assert call.parameters[-1].value == "policy"

    def test_the_value_is_a_length_and_its_bytes(self):
        # No pointer and no timestamp: those belong to a column, not to a
        # parameter, and reading them consumed twenty-five bytes that were
        # part of the text.
        value, at = _read_value(
            struct.pack("<I", 0x7FFFFFFF) + bytes(5)
            + struct.pack("<I", 6) + "abc".encode("utf-16-le"),
            0, 0x63,
        )
        assert value == "abc" and at == 19

    def test_text_reads_as_text(self):
        payload = (struct.pack("<I", 0x7FFFFFFF) + bytes(5)
                   + struct.pack("<I", 3) + b"abc")
        assert _read_value(payload, 0, 0x23) == ("abc", len(payload))

    def test_image_reads_as_bytes_and_carries_no_collation(self):
        payload = struct.pack("<I", 0x7FFFFFFF) + struct.pack("<I", 2) + b"\x01\x02"
        assert _read_value(payload, 0, 0x22) == (b"\x01\x02", len(payload))

    def test_a_null_one_is_a_length_of_all_ones(self):
        payload = struct.pack("<I", 0x7FFFFFFF) + bytes(5) + struct.pack("<I", 0xFFFFFFFF)
        assert _read_value(payload, 0, 0x63) == (None, len(payload))

    def test_the_whole_value_is_consumed_so_the_next_one_reads(self):
        # What made this worth fixing: a length read at the wrong offset
        # takes the rest of the call with it.
        one = (struct.pack("<I", 0x7FFFFFFF) + bytes(5)
               + struct.pack("<I", 2) + "a".encode("utf-16-le"))
        _, at = _read_value(one + b"leftover", 0, 0x63)
        assert (one + b"leftover")[at:] == b"leftover"
