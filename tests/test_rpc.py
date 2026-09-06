import struct

import pytest

from pysqlbridge.tds import ProcId, TdsProtocolError, parse_rpc

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
