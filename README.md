# pySQLbridge

Answer SQL Server's wire protocol convincingly enough that Excel and Power BI
connect to a JSON file, a CSV, or an HTTP API and see a database. Read-only, so
only the SELECT surface has to hold up.

## Status

A real SQL Server client logs in, runs a query and reads typed rows back,
resolving them to Int32, String and Double with NULLs intact. What it cannot do
yet is mean anything by the query: there is no SQL parsing and no data source,
so a handler either answers every batch the same way or refuses.

| Piece | State |
| --- | --- |
| Packet framing, split and reassembly | done |
| PRELOGIN | done |
| TLS handshake tunneled in TDS packets | done |
| Self-signed certificate | done |
| Login state machine and listener | done |
| LOGIN7 parse | done |
| Windows Authentication through SSPI | done |
| LOGINACK token stream | done |
| SQL batch parse | done |
| Result set encoding: int, nvarchar, float, null | done |
| SQL parsing and query planning | not started |
| Data source mapping | not started |

```
$ python -m pysqlbridge.server
listening on 127.0.0.1:1337
connection from 127.0.0.1:52434
127.0.0.1:52434 logged in as DOMAIN\user (app '.Net SqlClient Data Provider', database 'master')
127.0.0.1:52434 query: SELECT id, name, score, retired FROM people
```

`scripts/run_dev.ps1` starts it with a demo table for hands-on testing and
prints the connection strings for sqlcmd, Excel and Power BI.

No credential is handled here. The login carries a SPNEGO token and SSPI's
`AcceptSecurityContext` validates it against the local account database or the
domain.

## Protocol notes

Measured against SQL Server 2025 (17.0.1000.7) with Wireshark while desktop
Excel connected over Windows Authentication. The captured bytes are the test
fixtures, and every token the bridge emits is asserted byte-equal to the one the
reference server sent.

Details a client notices and the specification does not make obvious:

- The TLS handshake travels inside packets typed PRELOGIN (0x12). The
  application records that follow do not; they go on the wire bare.
- Encryption negotiated "off" still encrypts LOGIN7, then reverts to cleartext.
  The tunnel exists to carry one packet.
- The TLS context is pinned to 1.2. That is what the reference negotiated, and
  1.3 sends NewSessionTicket after the TDS framing has already stopped, where
  the protocol defines no way to carry it.
- Windows Authentication arrives as SPNEGO wrapping NTLM, offering four
  mechanisms with NTLM first. The NTLMSSP signature sits 61 bytes into the
  blob, not at its start.
- The TDS version is stored little-endian in LOGIN7 and big-endian in LOGINACK.
  Same value, reversed bytes, so echoing back what was parsed is wrong.
- LOGINACK's program name counts 22 characters for "Microsoft SQL Server". The
  last two are nulls.
- Token lengths are little-endian inside a big-endian packet header.
- The SSPI exchange is asymmetric: the server frames its blob as a token, 0xED
  plus a little-endian length, while the client sends its blob raw.
- A SQL batch is not just text. It opens with an ALL_HEADERS block that declares
  its own length, and skipping that by a constant rather than by the declared
  length puts header bytes into the query string.
- NULL is spelled differently per type. The one-byte-length types say it with a
  zero length; nvarchar cannot, because zero is a legitimate empty string, so it
  spends its whole two-byte length on 0xffff.
- Result columns use the nullable type forms, INTN and FLTN rather than INT4 and
  FLT8, because only those carry the length prefix a NULL needs.

[docs/tds-login-handshake.md](docs/tds-login-handshake.md) has the
packet-by-packet breakdown and the implementation order it implies.

## Port

Defaults to 1337, not 1433, so it can run beside the SQL Server instance it is
checked against.

```
Data Source=tcp:127.0.0.1,1337
```

The `tcp:` prefix is required for a local target. Without it the client selects
shared memory and never reaches the socket.

## Development

```bash
pip install -e ".[dev]"
python -m pytest
```

```powershell
.\scripts\run_dev.ps1            # demo table on 127.0.0.1:1337
.\scripts\run_dev.ps1 -NoDemo    # no data source; every query errors
```

Python 3.10 or newer. The suite needs neither SQL Server nor Excel. The
authentication and query tests need Windows, because they run a real SSPI
client against a real SSPI acceptor rather than a mock.

`scripts/capture_login.ps1` regenerates the reference capture. It needs
Wireshark, a reachable SQL Server and desktop Excel, and drives Excel through
[pyVBAharness](https://github.com/WilliamSmithEdward/pyVBAharness). Captures are
gitignored: they carry an NTLMv2 challenge and response, which is crackable
offline against a weak password.
