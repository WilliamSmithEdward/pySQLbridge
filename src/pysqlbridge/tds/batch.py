"""SQL_BATCH, the packet carrying a query.

The payload is not just text. It opens with an ALL_HEADERS block whose first
four bytes give its own total length, and the query follows as UTF-16LE. The
reference client sent a 22-byte block holding one header, a transaction
descriptor, before every batch.

Skipping the block by its declared length rather than by a constant matters:
the header set is extensible, and a client that sends a different one would
otherwise put its bytes into the query string.
"""

from __future__ import annotations

import struct

from .packet import ALL_HEADERS_ADDED_IN, TDS_74, TdsProtocolError

_ULONG = struct.Struct("<I")

# The header block declares its own length in its first four bytes, so it
# cannot be shorter than that.
MIN_ALL_HEADERS = _ULONG.size


def skip_all_headers(payload: bytes, tds_version: int = TDS_74) -> int:
    """The offset in payload past the ALL_HEADERS block.

    The block declares its own total length in its first four bytes, so it is
    skipped by that length rather than by a constant: the set is extensible,
    and a client that sends a different header would otherwise leak its bytes
    into whatever follows. It arrived in TDS 7.2; before that a request opens
    with its own content, so the offset is nought. A SQL batch and a
    transaction-manager request both begin with this block, so both skip it
    the same way.
    """
    if tds_version < ALL_HEADERS_ADDED_IN:
        return 0
    if len(payload) < MIN_ALL_HEADERS:
        raise TdsProtocolError(
            f"request is {len(payload)} bytes, too short to hold an "
            f"ALL_HEADERS length"
        )
    (headers_length,) = _ULONG.unpack_from(payload, 0)
    if headers_length < MIN_ALL_HEADERS or headers_length > len(payload):
        raise TdsProtocolError(
            f"request declares a {headers_length}-byte ALL_HEADERS block, "
            f"which does not fit in {len(payload)} bytes"
        )
    return headers_length


def parse_sql_batch(payload: bytes, tds_version: int = TDS_74) -> str:
    """Pull the query text out of a SQL_BATCH payload.

    The ALL_HEADERS block in front of the text arrived in TDS 7.2. A client
    negotiated down to 7.1 sends the text and nothing else, and reading its
    first four characters as a header length gets a nonsense number.
    """
    text = payload[skip_all_headers(payload, tds_version):]
    if len(text) % 2:
        raise TdsProtocolError(
            f"SQL batch query is {len(text)} bytes, which is not a whole "
            f"number of UTF-16 characters"
        )

    try:
        return text.decode("utf-16-le")
    except UnicodeDecodeError as exc:
        raise TdsProtocolError(f"SQL batch query is not valid UTF-16: {exc}") from exc
