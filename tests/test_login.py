import dataclasses
import struct

import pytest

from pysqlbridge.tds import Login7, TdsProtocolError
from pysqlbridge.tds.login import VARIABLE_DATA_START

from .captured import CLIENT_LOGIN7


class TestParseCaptured:
    def test_length_field_matches_the_message(self):
        assert struct.unpack_from("<I", CLIENT_LOGIN7)[0] == len(CLIENT_LOGIN7) == 428

    def test_tds_version_matches_the_servers_loginack(self):
        # The server's LOGINACK in the reference capture carried the same
        # value, so client and server agree on TDS 7.4.
        assert Login7.parse(CLIENT_LOGIN7).tds_version == 0x74000004

    def test_reads_the_strings(self):
        login = Login7.parse(CLIENT_LOGIN7)
        assert login.host_name == "WORKSTATION1"
        assert login.app_name == ".Net SqlClient Data Provider"
        assert login.server_name == "tcp:127.0.0.1,1337"
        assert login.database == "master"
        assert login.client_interface_name == ".Net SqlClient Data Provider"

    def test_windows_auth_sends_no_username_or_password(self):
        login = Login7.parse(CLIENT_LOGIN7)
        assert login.user_name == ""
        assert login.password == b""

    def test_reads_the_numeric_header(self):
        login = Login7.parse(CLIENT_LOGIN7)
        assert login.packet_size == 8000
        assert login.client_pid == 17700

    def test_carries_an_sspi_blob(self):
        login = Login7.parse(CLIENT_LOGIN7)
        assert len(login.sspi) == 129
        assert login.uses_integrated_auth is True

    def test_the_sspi_blob_is_spnego_not_bare_ntlm(self):
        # 0x60 is an ASN.1 application tag and the OID that follows is
        # 1.3.6.1.5.5.2, SPNEGO. Code that looked for "NTLMSSP" at byte 0
        # would find nothing.
        sspi = Login7.parse(CLIENT_LOGIN7).sspi
        assert sspi[0] == 0x60
        assert sspi[2:10] == bytes([0x06, 0x06, 0x2B, 0x06, 0x01, 0x05, 0x05, 0x02])
        assert not sspi.startswith(b"NTLMSSP")

    def test_variable_data_starts_where_the_table_ends(self):
        # The fixed header plus the offset table comes to 94 bytes, and the
        # first string sits exactly there with no gap.
        first_offset = struct.unpack_from("<H", CLIENT_LOGIN7, 36)[0]
        assert first_offset == VARIABLE_DATA_START == 94


class TestPasswordHandling:
    def test_password_is_kept_out_of_the_repr(self):
        assert "password" not in repr(
            Login7.parse(CLIENT_LOGIN7)
        ).replace("change_password=", "")

    def test_a_real_secret_does_not_leak_through_the_repr(self):
        login = dataclasses.replace(
            Login7.parse(CLIENT_LOGIN7), password=b"not-in-the-repr"
        )
        assert "not-in-the-repr" not in repr(login)


class TestParseErrors:
    def test_too_short_to_hold_a_header(self):
        with pytest.raises(TdsProtocolError, match="at least 94 bytes"):
            Login7.parse(b"\x00" * 40)

    def test_length_field_disagreeing_with_the_message(self):
        forged = bytearray(CLIENT_LOGIN7)
        struct.pack_into("<I", forged, 0, 999)
        with pytest.raises(TdsProtocolError, match="says it is 999 bytes"):
            Login7.parse(bytes(forged))

    def test_offset_pointing_past_the_message(self):
        # Offsets come from the client, so a hostile one must be refused
        # rather than slicing whatever happens to follow in memory.
        forged = bytearray(CLIENT_LOGIN7)
        struct.pack_into("<HH", forged, 36, 9000, 12)
        with pytest.raises(TdsProtocolError, match="past the 428-byte message"):
            Login7.parse(bytes(forged))

    def test_sspi_offset_pointing_past_the_message(self):
        forged = bytearray(CLIENT_LOGIN7)
        struct.pack_into("<HH", forged, 78, 400, 500)
        with pytest.raises(TdsProtocolError, match="SSPI blob"):
            Login7.parse(bytes(forged))
