"""TRANSACTION_MANAGER, the packet a client's transaction API sends.

A .NET client's BeginTransaction, Commit, Rollback and Save do not travel as
SQL. They arrive as packet type 0x0E: an ALL_HEADERS block, then a two-byte
request type, then a payload that depends on the type. This reads the request
type and the one field the server above needs, the transaction or savepoint
name a request carries.

What is read past but not kept: the isolation level a begin asks for, and the
opaque buffers of the distributed-transaction requests. A read-only bridge
serves one session and never writes, so the isolation level changes nothing it
can observe, and there is no coordinator for it to enlist in.

Wire format from [MS-TDS] 2.2.6.8 Transaction Manager Request. The request
types 5 through 9 arrived in TDS 7.2, the same version that put the ALL_HEADERS
block in front of a request, so a transaction-manager packet always carries
one.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

from .batch import skip_all_headers
from .packet import TDS_74, TdsProtocolError

_USHORT = struct.Struct("<H")


class TransactionRequestType(IntEnum):
    """The request-type field, a USHORT. Values from [MS-TDS] 2.2.6.8.

    The three distributed-transaction requests are named so a reader sees what
    is being declined, but nothing here acts on them.
    """

    GET_DTC_ADDRESS = 0
    PROPAGATE_XACT = 1
    BEGIN_XACT = 5
    PROMOTE_XACT = 6
    COMMIT_XACT = 7
    ROLLBACK_XACT = 8
    SAVE_XACT = 9


@dataclass(frozen=True)
class TransactionRequest:
    """One parsed transaction-manager request.

    name is the transaction name a begin was given, the savepoint a save
    marks, or the savepoint a rollback returns to; None when the request
    carried no name, which is what BeginTransaction() and a plain Rollback()
    send.
    """

    kind: TransactionRequestType
    name: str | None = None


def _b_varbyte(payload: bytes, offset: int) -> tuple[bytes, int]:
    """A one-byte length counting bytes, then that many bytes.

    Returns the bytes and the offset past them.
    """
    if offset >= len(payload):
        raise TdsProtocolError(
            "transaction-manager request ends before a length byte"
        )
    length = payload[offset]
    start = offset + 1
    end = start + length
    if end > len(payload):
        raise TdsProtocolError(
            f"transaction-manager request names {length} bytes, which run "
            f"past its {len(payload)}-byte packet"
        )
    return payload[start:end], end


def _name(raw: bytes) -> str | None:
    """A transaction or savepoint name, decoded, or None when it was empty.

    The name is UTF-16LE like every other string on the wire, and the length
    that preceded it counted its bytes.
    """
    if not raw:
        return None
    if len(raw) % 2:
        raise TdsProtocolError(
            f"transaction name is {len(raw)} bytes, not a whole number of "
            f"UTF-16 characters"
        )
    try:
        return raw.decode("utf-16-le")
    except UnicodeDecodeError as exc:
        raise TdsProtocolError(
            f"transaction name is not valid UTF-16: {exc}"
        ) from exc


def parse_transaction_request(
    payload: bytes, tds_version: int = TDS_74
) -> TransactionRequest:
    """Read a transaction-manager request down to the type and its name.

    Raises TdsProtocolError for a type this project does not know, which is
    what a real server does with an unknown one; the connection above turns a
    known-but-unsupported distributed request into an error the client can
    read rather than a dropped connection.
    """
    offset = skip_all_headers(payload, tds_version)
    if offset + _USHORT.size > len(payload):
        raise TdsProtocolError(
            "transaction-manager request has no room for a request type"
        )
    (raw_type,) = _USHORT.unpack_from(payload, offset)
    offset += _USHORT.size

    try:
        kind = TransactionRequestType(raw_type)
    except ValueError:
        raise TdsProtocolError(
            f"unknown transaction-manager request type {raw_type}"
        ) from None

    name: str | None = None
    if kind is TransactionRequestType.BEGIN_XACT:
        # An isolation-level byte, then the transaction name. A begin with no
        # payload at all is tolerated: it means the default isolation and no
        # name, which is what BeginTransaction() with no argument sends.
        if offset < len(payload):
            offset += 1  # isolation level, not kept
            raw, offset = _b_varbyte(payload, offset)
            name = _name(raw)
    elif kind in (
        TransactionRequestType.COMMIT_XACT,
        TransactionRequestType.ROLLBACK_XACT,
    ):
        # The name comes first, then flags and an optional new transaction to
        # chain into. Only the name is read: chaining is a write-side feature
        # a read-only server has nothing to do with.
        raw, offset = _b_varbyte(payload, offset)
        name = _name(raw)
    elif kind is TransactionRequestType.SAVE_XACT:
        raw, offset = _b_varbyte(payload, offset)
        name = _name(raw)

    return TransactionRequest(kind=kind, name=name)
