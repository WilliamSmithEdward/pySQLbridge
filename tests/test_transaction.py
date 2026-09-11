"""The transaction-manager request: parsing it, and the answer it earns.

A client's BeginTransaction/Commit/Rollback/Save arrive as packet type 0x0E
rather than as SQL. These cover reading one down to its type and name, the
statement it is turned into, and the ENVCHANGE token the reply carries. The
count each moves is checked through the catalog, which already keeps it for
the batch forms, so the two ways of beginning a transaction cannot drift.

The wire layouts built here are from [MS-TDS] 2.2.6.8.
"""

import struct

import pytest

from pysqlbridge import certificate
from pysqlbridge.catalog import Catalog
from pysqlbridge.tds import (
    Connection,
    TdsProtocolError,
    TransactionRequestType,
    parse_transaction_request,
)
from pysqlbridge.tds.connection import _transaction_statement
from pysqlbridge.tds.result import Query
from pysqlbridge.tds.token import EnvChangeType, TokenType

# A minimal ALL_HEADERS block: four bytes declaring only its own length. A real
# client sends a 22-byte one holding a transaction descriptor, but the parser
# skips the block by its declared length, so the shortest legal one exercises
# the same path.
HEADERS = struct.pack("<I", 4)

# Made once: a self-signed certificate is expensive and nothing here inspects it.
CERTIFICATE = certificate.self_signed("pysqlbridge.test")


def _b_varbyte(text: str) -> bytes:
    raw = text.encode("utf-16-le")
    return bytes([len(raw)]) + raw


def tm(request_type: int, rest: bytes = b"") -> bytes:
    """A transaction-manager payload: headers, a request type, then its body."""
    return HEADERS + struct.pack("<H", request_type) + rest


BEGIN = tm(5, b"\x00" + _b_varbyte(""))
BEGIN_NAMED = tm(5, b"\x00" + _b_varbyte("work"))
COMMIT = tm(7, _b_varbyte("") + b"\x00")
ROLLBACK = tm(8, _b_varbyte("") + b"\x00")
ROLLBACK_TO = tm(8, _b_varbyte("sp") + b"\x00")
SAVE = tm(9, _b_varbyte("sp"))


class TestParsingATransactionRequest:
    def test_a_begin_with_no_name(self):
        request = parse_transaction_request(BEGIN)
        assert request.kind is TransactionRequestType.BEGIN_XACT
        assert request.name is None

    def test_a_begin_with_no_payload_at_all(self):
        # BeginTransaction() with no isolation and no name sends only the type.
        request = parse_transaction_request(tm(5))
        assert request.kind is TransactionRequestType.BEGIN_XACT
        assert request.name is None

    def test_a_named_begin(self):
        assert parse_transaction_request(BEGIN_NAMED).name == "work"

    def test_a_commit_drops_its_name_field(self):
        request = parse_transaction_request(COMMIT)
        assert request.kind is TransactionRequestType.COMMIT_XACT

    def test_a_plain_rollback_has_no_name(self):
        assert parse_transaction_request(ROLLBACK).name is None

    def test_a_rollback_to_a_savepoint_keeps_the_name(self):
        request = parse_transaction_request(ROLLBACK_TO)
        assert request.kind is TransactionRequestType.ROLLBACK_XACT
        assert request.name == "sp"

    def test_a_save_keeps_its_name(self):
        request = parse_transaction_request(SAVE)
        assert request.kind is TransactionRequestType.SAVE_XACT
        assert request.name == "sp"

    def test_a_distributed_request_is_recognised(self):
        assert parse_transaction_request(tm(0)).kind is (
            TransactionRequestType.GET_DTC_ADDRESS
        )

    def test_an_unknown_type_is_refused(self):
        with pytest.raises(TdsProtocolError, match="unknown transaction"):
            parse_transaction_request(tm(99))

    def test_a_name_that_runs_past_the_packet_is_refused(self):
        # A length byte claiming ten bytes with two behind it.
        with pytest.raises(TdsProtocolError, match="run past"):
            parse_transaction_request(tm(9, b"\x0a" + b"ab"))

    def test_a_name_of_an_odd_number_of_bytes_is_refused(self):
        with pytest.raises(TdsProtocolError, match="whole number of UTF-16"):
            parse_transaction_request(tm(9, b"\x03" + b"abc"))

    def test_a_request_with_no_room_for_a_type_is_refused(self):
        with pytest.raises(TdsProtocolError, match="no room for a request type"):
            parse_transaction_request(HEADERS + b"\x05")


class TestTheStatementForARequest:
    """Each request becomes the statement that already keeps the count."""

    def statement(self, payload):
        return _transaction_statement(parse_transaction_request(payload))

    def test_begin(self):
        assert self.statement(BEGIN) == "BEGIN TRANSACTION"

    def test_a_named_begin_keeps_the_name(self):
        assert self.statement(BEGIN_NAMED) == "BEGIN TRANSACTION work"

    def test_commit(self):
        assert self.statement(COMMIT) == "COMMIT"

    def test_rollback(self):
        assert self.statement(ROLLBACK) == "ROLLBACK"

    def test_a_rollback_to_a_savepoint_returns_to_it(self):
        assert self.statement(ROLLBACK_TO) == "ROLLBACK TRANSACTION sp"

    def test_a_save_marks_the_point(self):
        assert self.statement(SAVE) == "SAVE TRANSACTION sp"

    def test_a_distributed_request_has_no_statement(self):
        assert self.statement(tm(0)) is None


class TestTheEnvChangeAReplyCarries:
    """The token a completed request sends, and the descriptor it carries.

    Built with a bare connection: the ENVCHANGE decision is about the request
    and the descriptor the connection holds, and needs no login.
    """

    def connection(self):
        return Connection(CERTIFICATE)

    def envchange(self, connection, payload):
        return connection._transaction_envchange(parse_transaction_request(payload))

    def test_a_begin_announces_a_transaction_with_a_descriptor(self):
        connection = self.connection()
        token = self.envchange(connection, BEGIN)
        assert token[0] == TokenType.ENV_CHANGE
        # Type byte, then the new value: a length-8 descriptor, then an empty
        # old value.
        body = token[3:]
        assert body[0] == EnvChangeType.BEGIN_TRAN
        assert body[1] == 8 and body[2:10] == connection._xact_descriptor
        assert body[10] == 0
        assert connection._xact_descriptor != b"\x00" * 8

    def test_a_commit_returns_the_descriptor_and_clears_it(self):
        connection = self.connection()
        self.envchange(connection, BEGIN)
        descriptor = connection._xact_descriptor
        token = self.envchange(connection, COMMIT)
        assert token[3] == EnvChangeType.COMMIT_TRAN
        # An empty new value, then the descriptor as the old value.
        assert token[4] == 0
        assert token[5] == 8 and token[6:14] == descriptor
        assert connection._xact_descriptor is None

    def test_a_plain_rollback_ends_the_transaction(self):
        connection = self.connection()
        self.envchange(connection, BEGIN)
        token = self.envchange(connection, ROLLBACK)
        assert token[3] == EnvChangeType.ROLLBACK_TRAN
        assert connection._xact_descriptor is None

    def test_a_rollback_to_a_savepoint_sends_no_stream(self):
        # The transaction lives on, so there is nothing to announce, and the
        # descriptor is kept for the commit that will end it.
        connection = self.connection()
        self.envchange(connection, BEGIN)
        assert self.envchange(connection, ROLLBACK_TO) == b""
        assert connection._xact_descriptor is not None

    def test_a_save_sends_no_stream(self):
        connection = self.connection()
        self.envchange(connection, BEGIN)
        assert self.envchange(connection, SAVE) == b""

    def test_each_begin_gets_its_own_descriptor(self):
        connection = self.connection()
        self.envchange(connection, BEGIN)
        first = connection._xact_descriptor
        self.envchange(connection, COMMIT)
        self.envchange(connection, BEGIN)
        assert connection._xact_descriptor != first


class TestCarriedThroughTheCatalog:
    """The count each request moves, the same counter the batch forms use."""

    def setup_method(self):
        self.catalog = Catalog()
        self.session: dict = {}

    def run(self, payload):
        statement = _transaction_statement(parse_transaction_request(payload))
        return self.catalog.answer(Query(sql=statement, session=self.session))

    def count(self):
        return self.catalog.answer(
            Query(sql="SELECT @@TRANCOUNT AS n", session=self.session)
        ).rows[0][0]

    def test_begin_and_commit_move_the_count(self):
        assert self.count() == 0
        self.run(BEGIN)
        assert self.count() == 1
        self.run(BEGIN)  # a nested begin, as a second API-level begin cannot
        assert self.count() == 2
        self.run(COMMIT)
        assert self.count() == 1

    def test_a_savepoint_rollback_keeps_the_transaction(self):
        self.run(BEGIN)
        self.run(SAVE)
        self.run(ROLLBACK_TO)
        assert self.count() == 1

    def test_a_plain_rollback_ends_it(self):
        self.run(BEGIN)
        self.run(BEGIN)
        self.run(ROLLBACK)
        assert self.count() == 0

    def test_a_commit_with_nothing_open_raises_3902(self):
        from pysqlbridge.tds.result import QueryError

        with pytest.raises(QueryError) as caught:
            self.run(COMMIT)
        assert caught.value.number == 3902
