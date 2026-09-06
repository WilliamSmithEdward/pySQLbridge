import pytest

from pysqlbridge.tds import (
    HEADER_SIZE,
    PacketStatus,
    PacketType,
    TdsProtocolError,
    build_message,
    build_packet,
    iter_packets,
    parse_header,
    reassemble,
)

from .captured import CLIENT_PRELOGIN, RESULT_HEADER, SERVER_PRELOGIN


class TestParseCaptured:
    def test_client_prelogin_header(self):
        header = parse_header(CLIENT_PRELOGIN)
        assert header.type is PacketType.PRELOGIN
        assert header.status is PacketStatus.END_OF_MESSAGE
        assert header.length == len(CLIENT_PRELOGIN) == 88
        assert header.payload_length == 80
        assert header.spid == 0
        assert header.packet_id == 1

    def test_server_prelogin_header(self):
        header = parse_header(SERVER_PRELOGIN)
        assert header.type is PacketType.TABULAR_RESULT
        assert header.length == len(SERVER_PRELOGIN) == 48

    def test_spid_is_big_endian_at_offset_four(self):
        # The server reported spid 77 for this session, so anything but 77
        # means the field is being read at the wrong offset or byte order.
        assert parse_header(RESULT_HEADER).spid == 77


class TestParseErrors:
    def test_short_buffer(self):
        with pytest.raises(TdsProtocolError, match="got 3"):
            parse_header(b"\x12\x01\x00")

    def test_unknown_type(self):
        with pytest.raises(TdsProtocolError, match="unknown TDS packet type 0x99"):
            parse_header(b"\x99\x01\x00\x08\x00\x00\x01\x00")

    def test_length_below_header_size(self):
        with pytest.raises(TdsProtocolError, match="smaller than"):
            parse_header(b"\x12\x01\x00\x03\x00\x00\x01\x00")


class TestBuild:
    def test_round_trips_a_captured_packet(self):
        rebuilt = build_packet(
            PacketType.PRELOGIN,
            CLIENT_PRELOGIN[HEADER_SIZE:],
            status=PacketStatus.END_OF_MESSAGE,
        )
        assert rebuilt == CLIENT_PRELOGIN

    def test_refuses_an_oversized_payload(self):
        with pytest.raises(ValueError, match="build_message"):
            build_packet(PacketType.SQL_BATCH, b"\x00" * 0x10000)


class TestBuildMessage:
    def test_single_packet_carries_end_of_message(self):
        packets = build_message(PacketType.SQL_BATCH, b"hello")
        assert len(packets) == 1
        assert parse_header(packets[0]).is_end_of_message

    def test_empty_payload_still_produces_one_packet(self):
        # A message with no packets would never be terminated, so the peer
        # would wait forever.
        packets = build_message(PacketType.SQL_BATCH, b"")
        assert len(packets) == 1
        assert parse_header(packets[0]).is_end_of_message

    def test_splits_on_the_negotiated_size(self):
        payload = bytes(range(256)) * 40  # 10240 bytes
        packets = build_message(PacketType.SQL_BATCH, payload, packet_size=4096)
        assert len(packets) == 3
        assert [len(p) for p in packets] == [4096, 4096, 10240 - 2 * 4088 + HEADER_SIZE]

    def test_only_the_last_packet_ends_the_message(self):
        packets = build_message(PacketType.SQL_BATCH, b"x" * 9000, packet_size=4096)
        flags = [parse_header(p).is_end_of_message for p in packets]
        assert flags == [False, False, True]

    def test_packet_ids_count_from_one(self):
        packets = build_message(PacketType.SQL_BATCH, b"x" * 9000, packet_size=4096)
        assert [parse_header(p).packet_id for p in packets] == [1, 2, 3]

    @pytest.mark.parametrize("size", [0, HEADER_SIZE, 0x10000])
    def test_rejects_impossible_packet_sizes(self, size):
        with pytest.raises(ValueError):
            build_message(PacketType.SQL_BATCH, b"x", packet_size=size)


class TestReassemble:
    def test_joins_a_split_message(self):
        payload = bytes(range(256)) * 40
        stream = b"".join(build_message(PacketType.SQL_BATCH, payload, packet_size=512))
        message = reassemble(stream)
        assert message.type is PacketType.SQL_BATCH
        assert message.payload == payload
        assert message.consumed == len(stream)

    def test_returns_none_until_the_message_ends(self):
        packets = build_message(PacketType.SQL_BATCH, b"x" * 9000, packet_size=4096)
        assert reassemble(b"".join(packets[:2])) is None

    def test_returns_none_on_a_partial_packet(self):
        assert reassemble(CLIENT_PRELOGIN[:40]) is None

    def test_ignores_bytes_beyond_the_first_message(self):
        stream = CLIENT_PRELOGIN + SERVER_PRELOGIN
        message = reassemble(stream)
        assert message.type is PacketType.PRELOGIN
        assert message.payload == CLIENT_PRELOGIN[HEADER_SIZE:]
        # consumed stops at the first message so the caller keeps the rest.
        assert message.consumed == len(CLIENT_PRELOGIN)


class TestIterPackets:
    def test_walks_every_complete_packet(self):
        stream = CLIENT_PRELOGIN + SERVER_PRELOGIN
        types = [header.type for header, _ in iter_packets(stream)]
        assert types == [PacketType.PRELOGIN, PacketType.TABULAR_RESULT]

    def test_stops_before_an_incomplete_tail(self):
        stream = CLIENT_PRELOGIN + SERVER_PRELOGIN[:20]
        assert len(list(iter_packets(stream))) == 1
