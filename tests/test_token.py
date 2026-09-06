import struct

import pytest

from pysqlbridge.tds import token as T

from . import captured as C


class TestReproducesTheReference:
    """Each encoder against the matching slice of the captured login response.

    Byte equality is the standard because the goal is not a token the
    specification permits, it is the token SQL Server 2025 actually sent.
    """

    def test_env_change_database(self):
        assert T.env_change(
            T.EnvChangeType.DATABASE, "master", "master"
        ) == C.ENVCHANGE_DATABASE

    def test_env_change_collation_is_bytes_not_text(self):
        # Collation is the one ENVCHANGE whose value is opaque bytes, length
        # prefixed by byte count rather than character count.
        assert T.env_change(
            T.EnvChangeType.SQL_COLLATION, T.DEFAULT_COLLATION
        ) == C.ENVCHANGE_COLLATION

    def test_env_change_language(self):
        assert T.env_change(
            T.EnvChangeType.LANGUAGE, "us_english"
        ) == C.ENVCHANGE_LANGUAGE

    def test_env_change_packet_size(self):
        assert T.env_change(
            T.EnvChangeType.PACKET_SIZE, "4096", "4096"
        ) == C.ENVCHANGE_PACKET_SIZE

    def test_info_5701(self):
        assert T.info(
            5701,
            "Changed database context to 'master'.",
            state=2,
            server="WORKSTATION1",
        ) == C.INFO_5701

    def test_info_5703(self):
        assert T.info(
            5703,
            "Changed language setting to us_english.",
            server="WORKSTATION1",
        ) == C.INFO_5703

    def test_login_ack(self):
        assert T.login_ack(version=(17, 0, 1000)) == C.LOGINACK

    def test_done(self):
        assert T.done() == C.DONE


class TestEncodingTraps:
    """The details that produce a token which is nearly right."""

    def test_loginack_writes_the_tds_version_big_endian(self):
        # LOGIN7 carries the same value the other way round. A server that
        # echoed back the bytes it parsed would send 04 00 00 74 here.
        body = C.LOGINACK[3:]
        assert body[1:5] == bytes([0x74, 0x00, 0x00, 0x04])
        assert struct.unpack(">I", body[1:5])[0] == T.TDS_74 == 0x74000004
        assert C.CLIENT_LOGIN7[4:8] == bytes([0x04, 0x00, 0x00, 0x74])

    def test_program_name_carries_two_trailing_nulls(self):
        # The B_VARCHAR counts 22 characters for a 20-character name.
        assert T.SERVER_PROGRAM_NAME == "Microsoft SQL Server\x00\x00"
        assert len(T.SERVER_PROGRAM_NAME) == 22
        assert C.LOGINACK[3 + 5] == 22

    def test_trimming_the_nulls_produces_a_shorter_token(self):
        trimmed = T.login_ack(version=(17, 0, 1000), program="Microsoft SQL Server")
        assert trimmed != C.LOGINACK
        assert len(trimmed) == len(C.LOGINACK) - 4

    def test_token_lengths_are_little_endian(self):
        # The packet header wrapping these is big-endian, so the two live
        # side by side and are easy to confuse.
        length = struct.unpack_from("<H", C.LOGINACK, 1)[0]
        assert length == len(C.LOGINACK) - 3

    def test_done_row_count_is_sixty_four_bit(self):
        assert len(T.done()) == 13  # token + status + curcmd + 8-byte count
        assert T.done(row_count=2**33)[5:] == struct.pack("<Q", 2**33)

    def test_refuses_a_body_too_large_for_the_length_field(self):
        with pytest.raises(ValueError, match="exceeds"):
            T.sspi_token(b"\x00" * 0x10000)


class TestLoginResponse:
    def test_token_order_matches_the_reference(self):
        stream = T.login_response(version=(17, 0, 1000), server_name="WORKSTATION1")
        for piece in (
            C.ENVCHANGE_DATABASE,
            C.INFO_5701,
            C.ENVCHANGE_COLLATION,
            C.ENVCHANGE_LANGUAGE,
            C.INFO_5703,
            C.LOGINACK,
            C.ENVCHANGE_PACKET_SIZE,
            C.DONE,
        ):
            assert piece in stream
        assert stream.index(C.LOGINACK) < stream.index(C.DONE)

    def test_reproduces_the_reference_stream_apart_from_feature_ack(self):
        # The reference also carried FEATUREEXTACK, acknowledging session
        # recovery, data classification and UTF-8. None of those are
        # implemented, so claiming them would be a lie the client acts on.
        stream = T.login_response(version=(17, 0, 1000), server_name="WORKSTATION1")
        reference = (
            C.ENVCHANGE_DATABASE + C.INFO_5701 + C.ENVCHANGE_COLLATION
            + C.ENVCHANGE_LANGUAGE + C.INFO_5703 + C.LOGINACK
            + C.ENVCHANGE_PACKET_SIZE + C.DONE
        )
        assert stream == reference

    def test_ends_with_done(self):
        stream = T.login_response(version=(17, 0, 1000), server_name="host")
        assert stream.endswith(T.done())

    def test_database_appears_in_the_messages_the_client_shows(self):
        stream = T.login_response(
            version=(17, 0, 1000), server_name="host", database="sales"
        )
        assert "Changed database context to 'sales'.".encode("utf-16-le") in stream
