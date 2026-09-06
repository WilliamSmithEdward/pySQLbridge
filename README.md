# pySQLbridge

Answer SQL Server's wire protocol convincingly enough that Excel and Power BI
connect to a JSON file, a CSV, or an HTTP API and see a database. Read-only, so
only the SELECT surface has to hold up.

## Status

A real SQL Server client lists the tables, then selects from a CSV file, a JSON
file or a live HTTP API over the wire, with inferred column types, NULLs, WHERE
and TOP. Parameterised queries work, which matters because clients send those as
RPC calls to sp_executesql rather than as SQL batches.

The SQL is still small: a column list, TOP, a table name and a WHERE. ORDER BY
and joins are refused by name rather than parsed and ignored.

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
| CSV and JSON sources with type inference | done |
| SELECT with a column list, TOP and WHERE | done |
| RPC, so parameterised queries work | done |
| INFORMATION_SCHEMA tables, columns, schemata | done |
| HTTP API sources, cached with a TTL | done |
| Configuration file | done |
| Single-file Windows executable | done |
| ORDER BY, joins, aggregates | not started |
| System stored procedures | not started |

```
$ python -m pysqlbridge.server --config examples/tables.json
serving 2 table(s): cities, people
listening on 127.0.0.1:1337
connection from 127.0.0.1:52434
127.0.0.1:52434 logged in as DOMAIN\user (app '.Net SqlClient Data Provider', database 'master')
127.0.0.1:52434 query: SELECT id, name, score, retired FROM people
```

```
> SELECT TOP 3 name FROM pokemon
name
----------
bulbasaur
ivysaur
venusaur

> SELECT name, score FROM people
name            score
--------------- ------------------------
Ada Lovelace                        99.5
Grace Hopper                       87.25
Edsger Dijkstra                     78.0
Barbara Liskov                     93.75
(4 rows affected)
```

`scripts/run_dev.ps1` serves the example tables and prints the connection
strings for sqlcmd, Excel and Power BI.

## Building the executable

```powershell
.\scripts\build_exe.ps1
```

Runs the suite, builds `dist\pysqlbridge.exe` with PyInstaller, then stages the
result in a directory with no source tree and drives a real client through it:
log in, run a query, list the catalog. About 13 MB, no Python needed on the
target, and it takes the same arguments the module does.

The smoke test is not politeness. PyInstaller cannot see an import that happens
inside a function, so a build can start, listen and load its tables and still
fail the instant a client authenticates. That is exactly what happened: `sspi`
is imported inside a function in `auth.py`, and the first build died on
`win32timezone`, which `sspi` reaches at runtime. Only running the executable
found it. Those names are listed in `pysqlbridge.spec` with a note saying why.

## Configuration

```json
{
  "tables": [
    { "name": "people", "csv":  "data/people.csv" },
    { "name": "cities", "json": "data/cities.json" },
    {
      "name": "pokemon",
      "http": {
        "url": "https://pokeapi.co/api/v2/pokemon?limit=25",
        "path": "results",
        "ttl": 300,
        "timeout": 20
      }
    }
  ]
}
```

Paths resolve against the configuration file, so a config and its data move
together. `name` is optional for files and defaults to the stem; an HTTP source
must be named, because a URL has no obvious table name.

An HTTP source fetches JSON and shapes it with exactly the same rules as a JSON
file. `path` walks a dotted route into the response, because most APIs wrap
their array in an envelope and selecting from the envelope would give one row of
metadata. `ttl` is how long a response is reused: refetching per query would
turn one table scan into a burst of identical requests at somebody else's API,
and never refetching would serve the first response forever. `timeout` is
finite and always sent, because a source that hangs holds the connection thread
that asked for it.

A source that cannot be reached is listed in the catalog with no columns rather
than failing the whole table list, and selecting from it reports why it could
not be loaded instead of claiming the table does not exist.

A column's type is inferred from every value in it, not per row, because
COLMETADATA declares it once and every row is encoded against that declaration.
One non-integer drops the whole column to float, one non-number drops it to
text. Empty CSV cells are NULL. JSON objects are unioned across records, so a
missing key gives NULL rather than shifting the row, and a nested object or
array is refused rather than stringified into something that looks like data
and cannot be queried.

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
- Clients do not send everything as a SQL batch. Anything parameterised, and
  every catalog query, arrives as an RPC call to sp_executesql with the
  statement as its first parameter.
- An RPC parameter's value is not self-describing: both its width and its
  length prefix come from TYPE_INFO, so an unreadable type has to stop parsing
  rather than be skipped.
- A table picker does not ask for a list of tables. It selects from
  INFORMATION_SCHEMA.TABLES with a clause like
  `(TABLE_NAME = @Name or (@Name is null))`, which needs SQL's three-valued
  logic to work: with the parameter null the comparison is unknown, not false,
  and only the IS NULL beside it makes the clause true. Treating unknown as
  false returns nothing and looks like an empty database.
- The schema part of a name cannot be discarded the way the database part can.
  INFORMATION_SCHEMA.TABLES and a user table called TABLES are different
  tables.
- The PRELOGIN encryption option is a negotiation, not a server setting. A
  client that asked for ENCRYPT_ON will not read cleartext afterwards, and
  answering OFF does not fail loudly: it completes the handshake, authenticates,
  then times out in its post-login phase waiting for bytes that never come.
- No columns means no result set, which is not the same as a query returning no
  rows. SET and USE produce nothing, and a client sent COLMETADATA for one of
  those reports an invalid cursor state on the query it was really waiting for.

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

For SSMS, Azure Data Studio and sqlcmd the separator is a **comma**:

```
127.0.0.1,1337
```

A colon is not a syntax error, which is what makes it worth stating. The client
reads the whole string as a host name, never sees a port, and fails over to
Named Pipes, so the error it reports mentions pipes and a missing network path
rather than anything about the port.

Those clients also default to `Encrypt=Mandatory` and will reject a self-signed
certificate, so tick Trust Server Certificate. Encryption itself is fine: the
bridge agrees to ENCRYPT_ON and keeps the tunnel up for the whole session.

## Development

```bash
pip install -e ".[dev]"
python -m pytest
```

```powershell
.\scripts\run_dev.ps1            # demo table on 127.0.0.1:1337
.\scripts\run_dev.ps1 -NoDemo    # no data source; every query errors
```

`scripts\run_dev.bat` is the same thing for cmd or a double-click, which
avoids PowerShell's execution policy refusing an unsigned script.

Python 3.10 or newer. The suite needs neither SQL Server nor Excel. The
authentication and query tests need Windows, because they run a real SSPI
client against a real SSPI acceptor rather than a mock.

`scripts/capture_login.ps1` regenerates the reference capture. It needs
Wireshark, a reachable SQL Server and desktop Excel, and drives Excel through
[pyVBAharness](https://github.com/WilliamSmithEdward/pyVBAharness). Captures are
gitignored: they carry an NTLMv2 challenge and response, which is crackable
offline against a weak password.
