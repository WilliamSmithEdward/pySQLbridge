"""What changes when a client negotiates an older protocol.

TDS 7.2 widened three fields and added a header block. A server that ignores
the negotiated version sends the modern shape to everyone, and the modern
shape is unreadable to an older client: extra bytes between tokens put every
following read at the wrong offset.

Each of these was found by pointing the legacy "SQL Server" ODBC driver at
this server and following the error it gave, one layer at a time:

    the login response      a version older than 6.5 is not supported
    the RPC it then sent    a 7,536,649-byte header block in a 116-byte packet
    the answer to it        protocol error in TDS stream

and then reading a real SQL Server's answer to the same driver to see what it
had done differently.
"""

import pytest

from pysqlbridge.tds import batch, rpc
from pysqlbridge.tds import token as T
from pysqlbridge.tds.packet import TDS_71, TDS_72, TDS_74
from pysqlbridge.tds.result import Column, Integer, NVarChar, col_metadata


class TestLineNumberWidth:
    """INFO and ERROR carry a two-byte line number before TDS 7.2."""

    def one_info(self, version):
        return T.info(5701, "Changed database context to 'master'.",
                      server="HOST", tds_version=version)

    def test_the_older_form_is_two_bytes_shorter(self):
        assert len(self.one_info(TDS_74)) - len(self.one_info(TDS_71)) == 2

    def test_the_boundary_is_seven_two(self):
        assert len(self.one_info(TDS_72)) == len(self.one_info(TDS_74))

    def test_error_carries_the_same_field(self):
        older = T.error(208, "invalid object name", tds_version=TDS_71)
        newer = T.error(208, "invalid object name", tds_version=TDS_74)
        assert len(newer) - len(older) == 2

    def test_the_declared_length_matches_what_follows(self):
        # The token declares its own length, and a client that trusts it and
        # finds two spare bytes reads the next token from the wrong place.
        import struct

        for version in (TDS_71, TDS_74):
            written = self.one_info(version)
            declared = struct.unpack_from("<H", written, 1)[0]
            assert declared == len(written) - 3

    def test_the_login_response_uses_the_negotiated_width(self):
        older = T.login_response(version=(17, 0, 1000), server_name="HOST",
                                 tds_version=TDS_71)
        newer = T.login_response(version=(17, 0, 1000), server_name="HOST",
                                 tds_version=TDS_74)
        # Two INFO tokens at two bytes each, and four more on the row count.
        assert len(newer) - len(older) == 8


class TestUserTypeWidth:
    """COLMETADATA carries a two-byte user type before TDS 7.2."""

    def columns(self):
        return [Column(name="id", type=Integer(4)),
                Column(name="name", type=NVarChar(128))]

    def test_the_older_form_is_two_bytes_shorter_per_column(self):
        older = col_metadata(self.columns(), TDS_71)
        newer = col_metadata(self.columns(), TDS_74)
        assert len(newer) - len(older) == 4

    def test_the_boundary_is_seven_two(self):
        assert len(col_metadata(self.columns(), TDS_72)) == \
            len(col_metadata(self.columns(), TDS_74))


class TestAllHeaders:
    """The block in front of a request arrived in TDS 7.2."""

    def test_an_older_batch_is_the_text_alone(self):
        payload = "SELECT 1".encode("utf-16-le")
        assert batch.parse_sql_batch(payload, TDS_71) == "SELECT 1"

    def test_a_modern_batch_still_has_its_header(self):
        header = (22).to_bytes(4, "little") + bytes(18)
        payload = header + "SELECT 1".encode("utf-16-le")
        assert batch.parse_sql_batch(payload, TDS_74) == "SELECT 1"

    def test_an_older_rpc_starts_at_the_procedure_name(self):
        name = "sp_tables"
        payload = (len(name).to_bytes(2, "little")
                   + name.encode("utf-16-le")
                   + (0).to_bytes(2, "little"))
        assert rpc.parse_rpc(payload, TDS_71).procedure == "sp_tables"

    def test_reading_an_older_rpc_as_a_modern_one_fails_loudly(self):
        # The name length and option flags come out as a header size in the
        # millions, which is what the legacy driver produced.
        name = "sp_tables"
        payload = (len(name).to_bytes(2, "little")
                   + name.encode("utf-16-le")
                   + (0).to_bytes(2, "little"))
        with pytest.raises(Exception, match="ALL_HEADERS"):
            rpc.parse_rpc(payload, TDS_74)
