import struct

import pytest

from pysqlbridge.tds import TdsProtocolError, build_packet, parse_sql_batch
from pysqlbridge.tds.packet import HEADER_SIZE, PacketType

from . import captured as C

PAYLOAD = C.SQL_BATCH[HEADER_SIZE:]


def batch_payload(query: str, headers: bytes = b"\x16\x00\x00\x00" + b"\x00" * 18) -> bytes:
    return headers + query.encode("utf-16-le")


class TestParseCaptured:
    def test_extracts_the_query(self):
        assert parse_sql_batch(PAYLOAD).startswith("SELECT CAST(42 AS int) AS answer")

    def test_the_reference_client_sent_a_22_byte_header_block(self):
        assert struct.unpack_from("<I", PAYLOAD)[0] == 22

    def test_the_packet_is_typed_as_a_batch(self):
        from pysqlbridge.tds import parse_header

        assert parse_header(C.SQL_BATCH).type is PacketType.SQL_BATCH


class TestHeaderBlock:
    def test_skips_by_the_declared_length_not_a_constant(self):
        # A client sending a different header set would otherwise have its
        # bytes decoded as part of the query.
        headers = struct.pack("<I", 30) + b"\xaa" * 26
        assert parse_sql_batch(batch_payload("SELECT 1", headers)) == "SELECT 1"

    def test_a_minimal_header_block_is_accepted(self):
        assert parse_sql_batch(batch_payload("SELECT 1", struct.pack("<I", 4))) == "SELECT 1"

    def test_an_empty_query_is_not_an_error(self):
        assert parse_sql_batch(batch_payload("", struct.pack("<I", 4))) == ""


class TestParseErrors:
    def test_too_short_for_a_length(self):
        with pytest.raises(TdsProtocolError, match="too short"):
            parse_sql_batch(b"\x16\x00")

    def test_header_block_longer_than_the_payload(self):
        with pytest.raises(TdsProtocolError, match="does not fit"):
            parse_sql_batch(struct.pack("<I", 900) + b"\x00" * 10)

    def test_header_block_shorter_than_its_own_length_field(self):
        with pytest.raises(TdsProtocolError, match="does not fit"):
            parse_sql_batch(struct.pack("<I", 2) + b"\x00" * 10)

    def test_odd_byte_count_is_not_utf16(self):
        with pytest.raises(TdsProtocolError, match="not a whole number"):
            parse_sql_batch(struct.pack("<I", 4) + b"\x41\x00\x42")

    def test_unpaired_surrogate_is_rejected(self):
        with pytest.raises(TdsProtocolError, match="not valid UTF-16"):
            parse_sql_batch(struct.pack("<I", 4) + b"\x00\xd8\x41\x00")
