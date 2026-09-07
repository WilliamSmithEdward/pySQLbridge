import pytest

from pysqlbridge.tds import (
    HEADER_SIZE,
    Encryption,
    Prelogin,
    PreloginOption,
    TdsProtocolError,
    Version,
    server_response,
)

from .captured import CLIENT_PRELOGIN, SERVER_PRELOGIN

CLIENT_PAYLOAD = CLIENT_PRELOGIN[HEADER_SIZE:]
SERVER_PAYLOAD = SERVER_PRELOGIN[HEADER_SIZE:]


class TestParseClient:
    def test_reads_the_driver_version(self):
        # MSOLEDBSQL reported 18.7.5 in the capture.
        assert str(Prelogin.parse(CLIENT_PAYLOAD).version) == "18.7.5"

    def test_encryption_is_off(self):
        assert Prelogin.parse(CLIENT_PAYLOAD).encryption is Encryption.OFF

    def test_mars_is_off(self):
        assert Prelogin.parse(CLIENT_PAYLOAD).mars is False

    def test_no_instance_requested(self):
        assert Prelogin.parse(CLIENT_PAYLOAD).instance == ""

    def test_keeps_options_in_wire_order(self):
        tokens = [token for token, _ in Prelogin.parse(CLIENT_PAYLOAD).options]
        assert tokens == [
            PreloginOption.VERSION,
            PreloginOption.ENCRYPTION,
            PreloginOption.INSTOPT,
            PreloginOption.THREADID,
            PreloginOption.MARS,
            PreloginOption.TRACEID,
        ]

    def test_traceid_carries_its_thirty_six_bytes(self):
        assert len(Prelogin.parse(CLIENT_PAYLOAD).get(PreloginOption.TRACEID)) == 36


class TestParseServer:
    def test_reads_the_server_version(self):
        assert str(Prelogin.parse(SERVER_PAYLOAD).version) == "17.0.1000"

    def test_version_fields_decode_individually(self):
        assert Prelogin.parse(SERVER_PAYLOAD).version == Version(17, 0, 1000, 0)

    def test_empty_options_are_present_not_absent(self):
        # The server sends THREADID and TRACEID with length 0. Treating them
        # as missing would drop them from a rebuilt response.
        prelogin = Prelogin.parse(SERVER_PAYLOAD)
        assert prelogin.get(PreloginOption.THREADID) == b""
        assert prelogin.get(PreloginOption.TRACEID) == b""

    def test_absent_option_reads_as_none(self):
        assert Prelogin.parse(SERVER_PAYLOAD).get(PreloginOption.NONCEOPT) is None


class TestRoundTrip:
    @pytest.mark.parametrize(
        "payload", [CLIENT_PAYLOAD, SERVER_PAYLOAD], ids=["client", "server"]
    )
    def test_rebuilds_the_captured_bytes(self, payload):
        assert Prelogin.parse(payload).build() == payload


class TestServerResponse:
    def test_matches_the_captured_response_byte_for_byte(self):
        # The whole point of the module: what pysqlbridge emits is what SQL
        # Server 2025 emitted, not merely something the specification allows.
        assert server_response().build() == SERVER_PAYLOAD

    def test_defaults_to_the_reference_version(self):
        assert str(Prelogin.parse(server_response().build()).version) == "17.0.1000"

    def test_mars_can_be_advertised(self):
        assert Prelogin.parse(server_response(mars=True).build()).mars is True

    def test_encryption_choice_survives_the_round_trip(self):
        response = server_response(encryption=Encryption.REQUIRED)
        assert Prelogin.parse(response.build()).encryption is Encryption.REQUIRED


class TestParseErrors:
    def test_missing_terminator(self):
        # One in-bounds entry and then the payload simply stops. The entry has
        # to be in bounds or the offset check fires first and this stops
        # testing what it says it does.
        with pytest.raises(TdsProtocolError, match="no terminator"):
            Prelogin.parse(b"\x00\x00\x05\x00\x00")

    def test_entry_runs_past_the_payload(self):
        with pytest.raises(TdsProtocolError, match="runs past"):
            Prelogin.parse(b"\x00\x00")

    def test_offset_points_outside_the_payload(self):
        with pytest.raises(TdsProtocolError, match="past the"):
            Prelogin.parse(b"\x00\xff\xff\x00\x06\xff")

    def test_unknown_option_token(self):
        with pytest.raises(TdsProtocolError, match="unknown PRELOGIN option 0x7f"):
            Prelogin.parse(b"\x7f\x00\x06\x00\x00\xff")

    def test_version_with_the_wrong_length(self):
        with pytest.raises(TdsProtocolError, match="VERSION option needs 6"):
            Prelogin.parse(b"\x00\x00\x06\x00\x02\xff\xaa\xbb").version


class TestMirroringTheClient:
    """A server answers the options it was asked about and no others.

    Measured against SQL Server 2025 through a proxy: a modern driver asks
    about six options and is answered with six; the legacy ODBC driver asks
    about four and is answered with four. This project always sent six, which
    put two options in front of a client that had not asked for them.
    """

    def four(self):
        return Prelogin(options=[
            (PreloginOption.VERSION, bytes(6)),
            (PreloginOption.ENCRYPTION, b"\x00"),
            (PreloginOption.INSTOPT, b"\x00"),
            (PreloginOption.THREADID, b"\x01\x02\x03\x04"),
        ])

    def test_it_answers_only_what_was_asked(self):
        answer = server_response(asked=self.four())
        assert [token for token, _ in answer.options] == [
            PreloginOption.VERSION,
            PreloginOption.ENCRYPTION,
            PreloginOption.INSTOPT,
            PreloginOption.THREADID,
        ]

    def test_mars_and_traceid_are_dropped_when_unasked(self):
        answer = server_response(asked=self.four())
        assert answer.get(PreloginOption.MARS) is None
        assert answer.get(PreloginOption.TRACEID) is None

    def test_version_and_encryption_go_out_regardless(self):
        # A client cannot proceed without either, whatever it asked about.
        asked = Prelogin(options=[(PreloginOption.INSTOPT, b"\x00")])
        answer = server_response(asked=asked)
        assert answer.get(PreloginOption.VERSION) is not None
        assert answer.get(PreloginOption.ENCRYPTION) is not None

    def test_with_nothing_to_mirror_all_six_go_out(self):
        assert len(server_response().options) == 6

    def test_the_captured_client_still_gets_the_captured_answer(self):
        # The reference capture is the six-option case, and it has to stay
        # byte for byte what it was.
        asked = Prelogin.parse(CLIENT_PAYLOAD)
        assert len(server_response(asked=asked).options) == 6
