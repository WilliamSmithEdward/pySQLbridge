# pySQLbridge

Answer SQL Server's wire protocol convincingly enough that Excel and Power BI
connect to a JSON file, a CSV, or an HTTP API and see a database. Read-only, so
only the SELECT surface has to hold up.

## Status

A real SQL Server client lists the tables, then selects from a CSV file, a JSON
file or a live HTTP API over the wire, with inferred column types, NULLs, WHERE
and TOP. Parameterised queries work, which matters because clients send those as
RPC calls to sp_executesql rather than as SQL batches.

The SQL is still small: a column list with aliases, TOP, WHERE, ORDER BY, and
COUNT, SUM, MIN, MAX and AVG over a whole table. Joins and GROUP BY are refused
by name rather than parsed and ignored.

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
| SELECT with a column list, TOP, WHERE and ORDER BY | done |
| Column aliases, and whole-table aggregates | done |
| RPC, so parameterised queries work | done |
| INFORMATION_SCHEMA tables, columns, schemata | done |
| HTTP API sources: nested, paged, raced, cached | done |
| Configuration file | done |
| Single-file Windows executable | done |
| Joins, GROUP BY, expressions in the select list | not started |
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
> SELECT COUNT(*) AS n, MIN(score) AS lo, MAX(score) AS hi FROM people
n           lo                       hi
----------- ------------------------ ------------------------
          4                     78.0                     99.5

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

The shortest useful config is a base URL:

```json
{
  "discover": [
    { "url": "https://pokeapi.co/api/v2/" }
  ]
}
```

That crawls the surface and serves what it finds. Against the PokeAPI it
produces 27 tables; against dummyjson.com, 8. Naming tables one at a time is
still there for the cases discovery cannot reach:

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

Both keys may appear. A named table wins over a discovered one of the same
name, because a person who wrote a name meant it.

Paths resolve against the configuration file, so a config and its data move
together. `name` is optional for files and defaults to the stem; an HTTP source
must be named, because a URL has no obvious table name.

An HTTP source fetches JSON and shapes it with the same rules as a JSON file.
Fifty-four public endpoints were surveyed to decide what those rules are; see
[docs/api-shapes.md](docs/api-shapes.md).

| Key | What it does |
| --- | --- |
| `url` | one URL, or a list of them for a load-balanced set |
| `path` | a dotted route to the rows; a numeric segment indexes a list |
| `records` | `array`, `single`, `values`, `entries`, `columns` or `scalars` |
| `flatten` | nested objects become dotted columns, on by default |
| `columns` | which columns to keep, for a record that is too wide |
| `next` | a dotted route to the next page's URL |
| `max_pages`, `max_rows` | bounds on following it |
| `ttl`, `timeout`, `headers` | reuse, deadline, and anything an API needs |
| `auth` | a credential, described below |

`records` defaults to `auto`, which scores the readings of the document and
refuses a weak winner rather than guessing. Naive detection is the trap here:
over 85 public endpoints, a detector that simply looked for an array found one
for 84 of them and was frequently wrong, serving paging links, a nested field,
or a rejected request's `errors` list as the table. Scored detection is right
on all 40 endpoints whose correct answer was written down first, and costs
0.01 to 0.06 ms. Name `records` explicitly to turn detection off.

### Discovering an API

`discover` takes the base of an API and works out what is on it. Three routes,
tried in that order:

1. **A description document.** OpenAPI at `openapi.json`, `swagger.json`,
   `v3/api-docs`, `swagger/v1/swagger.json`, `.well-known/openapi.json` or
   `api-docs`, under the base and then at the origin. Every GET path without a
   parameter in it becomes a candidate.
2. **A link index.** Many APIs answer their own root with a map of name to
   URL. HAL `_links` and JSON:API `links` count too.
3. **Conventional names**, only when the first two found nothing at all. Some
   real APIs publish neither an index nor a description, and asking for the
   names such an API probably uses is the move left.

The crawl runs even when a description was found, because a description is
authoritative about what it names and silent about what it omits.

| Key | What it does |
| --- | --- |
| `url` | the base to crawl |
| `prefix` | put in front of every discovered table name |
| `max_requests`, `max_depth`, `concurrency` | bounds on the walk |
| `guess` | try conventional names as a last resort, on by default |
| `auth`, `headers`, `ttl`, `timeout` | passed to every source it produces |

Links inside row data are not followed. A collection of 20 characters holds 20
links to 20 individual characters, and following them produces 20 more one-row
tables named after the one you already had. URI templates are not fetched:
`https://api.github.com/repos/{owner}/{repo}` is an invitation to substitute,
not an address.

### Pagination

A collection served a page at a time is read whole. Discovery works out how,
from the response rather than from a list of API names: a `next` link where
there is one (`next`, `info.next`, `links.next`, `_links.next.href`), and
otherwise the position the envelope reports about itself. dummyjson answers
with `skip: 0, limit: 30` beside a total of 194, GBIF with `offset` and
`limit`, Algolia with `page`. Each of them is echoing back the parameter it
was given, which is what makes advancing it general rather than a guess.

Pages are then fetched in parallel. Two consecutive links show which parameter
moves and by how much, so the rest can be written down instead of asked for
one at a time; measured against a server held at 40 ms, 200 rows in 20 pages
went from 0.99s to 0.33s. The end of a collection is still the API saying so,
not a guess from a row count: an empty page, a repeated one, or one with no
next link of its own.

If an API ignores the parameter and answers with page one every time, the
second page is identical to the first and the read stops there rather than
serving twenty copies of it. `max_pages` (50) and `max_rows` (100,000) bound
the rest, and stopping at `max_pages` with more available is logged, because a
partial collection served silently is the one failure a person querying the
table cannot see.

Requests to one host are capped at four at a time. Sources load in parallel
and each may be paging, so without that a catalog of fifty PokeAPI tables
opens several hundred connections to one server: enough, measured, for an API
to start refusing.

### Table lists are cheap, queries are complete

`INFORMATION_SCHEMA` and the startup warm read only each source's first page,
because they want to know what exists and what its columns are. Reading every
page of every source to answer that took 23 seconds on a catalog of 65
discovered tables, nearly all of it spent paginating collections nobody had
asked for; it now takes under a second. A query reads the whole table.

The one thing the two can differ on is a column type, since types are inferred
from the values present and a later page can hold a float in a column whose
first page was whole numbers. The query is unaffected: it infers over
everything it read.

### XML and HTML

Neither gets its own pipeline. Both become lists and dicts and then go through
exactly the same detection, flattening and typing as JSON, so an RSS feed and
a JSON envelope with rows under `items` end up as the same table.

```json
{
  "tables": [
    { "name": "headlines", "http": "https://feeds.bbci.co.uk/news/rss.xml" },
    { "name": "elements",
      "http": "https://en.wikipedia.org/wiki/List_of_chemical_elements" },
    { "name": "catalog", "xml": "data/catalog.xml" }
  ]
}
```

The format is sniffed from the first bytes. Content-Type is wrong often
enough to matter, and a feed served as `text/html` would be unreadable if the
header were believed. Set `"format"` to `json`, `xml` or `html` to say
outright.

In XML, attributes become `@`-prefixed columns so `<link href="...">` and
`<link><href>` do not collide, a repeated tag becomes rows, and namespaces are
stripped: `{http://www.w3.org/2005/Atom}title` cannot be typed into a query. A
DOCTYPE is refused rather than parsed, because a DTD can define entities that
expand a small document into gigabytes inside the parser.

From HTML you get every `<table>` on the page, keyed by its caption or its
position, plus any JSON-LD the page publishes. Layout tables with a single row
are dropped, `<style>` blocks inside cells are ignored, and the header is the
widest-by-distinct-values row in the run of `<th>` rows at the top, which is
what separates a real header both from a colspan sub-header below it and from
a spanning title above it. Page layout is not scraped: a `<table>` is the one
thing on a page that is already a table.

### Credentials

```json
{ "auth": { "bearer": "${GITHUB_TOKEN}" } }
{ "auth": { "header": "X-API-Key", "value": "${WEATHER_KEY}" } }
{ "auth": { "header": "Authorization", "prefix": "Token", "value": "${PAT}" } }
{ "auth": { "query": "api_key", "value": "${NASA_KEY}" } }
{ "auth": { "basic": { "username": "someone", "password": "${PASSWORD}" } } }
```

`${VAR}` and `${env:VAR}` read the environment. A config file gets committed
and a token in a committed file is a leaked token, so the config holds the name
and the value stays outside it. A literal is accepted for local testing. The
secret never reaches a repr, a log line, or an error message: a failure names
the variable it came from, not what was in it.

Several URLs are raced, and the first **successful** one wins: a replica that
fails fast should not beat one that succeeds slowly. Sources load in parallel
and the server warms them at startup, so the first client waits for none of it.
Once something is cached, an expiry serves the previous answer immediately and
refreshes behind it.

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

No client credential is handled here. The login carries a SPNEGO token and
SSPI's `AcceptSecurityContext` validates it against the local account database
or the domain. Credentials for the APIs this bridge reads from are separate,
and are described above.

## The SQL it answers

Measured rather than chosen: thirty queries a client or a person would
plausibly send were run through the whole stack, and the sixteen that were
refused set the order of work. Twenty-nine now answer.

```sql
SELECT p.name, COUNT(*) AS posts
FROM people p
JOIN posts o ON o.userId = p.id
WHERE p.name LIKE 'C%'
GROUP BY p.name
HAVING COUNT(*) > 5
ORDER BY posts DESC
OFFSET 0 ROWS FETCH NEXT 10 ROWS ONLY
```

| | |
| --- | --- |
| select list | columns, `*`, `*` beside columns, aliases with or without `AS` |
| expressions | arithmetic, `+` on text, `CASE` in both forms, `CAST`, `CONVERT` |
| functions | `LEN` `UPPER` `LOWER` `LTRIM` `RTRIM` `TRIM` `LEFT` `RIGHT` `SUBSTRING` `REPLACE` `REVERSE` `CHARINDEX` `CONCAT` `SPACE` `STR` `ISNULL` `COALESCE` `NULLIF` `IIF` `ABS` `SIGN` `FLOOR` `CEILING` `ROUND` `POWER` `SQRT` |
| aggregates | `COUNT` `SUM` `MIN` `MAX` `AVG`, whole-table or per group |
| where | `=` `<>` `<` `<=` `>` `>=`, `LIKE` with `ESCAPE`, `IN`, `BETWEEN`, `IS NULL`, `AND` `OR` `NOT` |
| joins | `INNER`, `LEFT`, `CROSS`, with table aliases |
| grouping | `GROUP BY`, `HAVING` naming an aggregate or its alias |
| rest | `DISTINCT`, `TOP`, `ORDER BY`, `OFFSET`/`FETCH`, `WITH`, derived tables, `IN`/`EXISTS`/scalar subqueries, `@@VERSION` and friends |

`UNION` is not supported. Neither is anything that writes.

### Measured against SQL Server 2025

The semantics are not chosen, they are compared. `scripts/differential.py`
writes a fixture twice, once as JSON for this and once as INSERT statements
for SQL Server, and `scripts/differential.ps1` runs 144 queries against both
and reports where the answers differ. The rows sit on the edges rather than
the middle: NULL in every position that treats it specially, text differing
only in case, an empty string, a zero, a negative, and a key that matches
nothing.

Thirteen differences turned up that way, every one of them wrong here:

| | SQL Server | was |
| --- | --- | --- |
| `-7 / 2` | `-3`, truncated toward zero | `-4`, floored |
| `-7 % 3` | `-1`, the dividend's sign | `2`, the divisor's |
| `'1' + 2` | `3`, int outranks varchar | `'12'` |
| `rank IN (1, '2')` | matches, same rule | did not |
| `'ada' = 'ada  '` | true, trailing spaces are padded | false |
| `ROUND(2.5, 0)` | `3`, halves go away from zero | `2`, to even |
| `SUBSTRING('abc', 0, 2)` | `'a'`, a start before the string spends length | `'ab'` |
| `REPLACE('abc','b',NULL)` | `NULL`, strict in every argument | `'ac'` |
| `MAX(name)` | `Grace`, ordered by the collation | `barbara`, by code point |
| `ORDER BY name` | `ada, alan, barbara, Edsger, Grace` | capitals first |
| `1 / 0` | an error | `NULL` |
| `POWER(2.0, 0.5)` | `1.4`, the argument's scale is kept | `1.4142...` |
| `GROUP BY a, b` | groups on both | read only the first |

`COUNT(DISTINCT x)` and aggregates over an expression came out of the same
run, as things a real server answers and this refused.

Text compares case-insensitively, because every column here is declared
`SQL_Latin1_General_CP1_CI_AS` and a client told one thing and given another
has no way to notice. That applies to `=`, `LIKE`, `IN`, `DISTINCT`,
`GROUP BY`, `MIN`, `MAX`, `ORDER BY` and a join's matching alike.

A join is a hash join on whatever equalities its `ON` offers, falling back to
comparing every pair when it offers none. Two tables of a thousand rows is a
million comparisons that way and two thousand with a hash, and these tables
arrive from APIs that hand over everything they have. What one would build is
capped, so a condition matching everything against everything fails with a
message rather than by exhausting memory.

Named queries, derived tables and subqueries are one mechanism: a `SELECT`
evaluated to a table and then used where a table or a value was expected. A
subquery inside a condition is lifted out before the condition is parsed and
replaced with a parameter, so the expression layer never learns what a catalog
is. Nesting is bounded, which is what catches a `WITH` that names itself.

Integer division truncates and division by zero is NULL. SQL Server raises on
the second, but a query that dies partway through a scan leaves a client with
neither an answer nor the rows it already had, and this only ever reads.

## Table discovery from a client

No client finds tables by reading INFORMATION_SCHEMA. Measured against this
server:

| Client | What it calls |
| --- | --- |
| ODBC Driver 17 and 18, used by Excel and Power Query | `sys.sp_tables`, then `sys.sp_columns_100` |
| .NET SqlClient | `INFORMATION_SCHEMA.TABLES`, then `sys.sp_columns_managed` |

Without those a client connects, authenticates, and shows an empty table
picker, which looks like an empty server rather than a missing procedure.
`sp_tables`, `sp_columns` and its version-suffixed variants, and `sp_databases`
are implemented in ODBC's documented layout, because drivers read those
columns by position as much as by name: one that finds SCALE where it expects
RADIX does not report a mismatch, it reports the wrong type.

They arrive as RPC calls rather than as SQL text, so which procedures exist is
decided by the catalog rather than by the protocol layer. Anything else still
answers 2812 rather than completing silently, because a client that asks for a
result set and receives nothing raises a NullReferenceException with nothing to
explain it.

MSOLEDBSQL asks for a different family. OLE DB defines its own schema rowsets,
so `sp_tables_rowset2`, `sp_columns_100_rowset2` and a dozen others exist
alongside the ODBC ones with different layouts and a different type system:
nvarchar is -9 to ODBC and 130 to OLE DB, and -9 is not a DBTYPE at all. Every
layout was read off SQL Server 2025 by running the procedure and taking its
result metadata, because a provider reading a column of the wrong type does
not report a mismatch.

### Talking to an old client

TDS 7.2 widened three fields and added a header block, and a server that
ignores the version a client asked for sends the modern shape to everyone.
The modern shape is unreadable to an older client: two spare bytes between
tokens put every following read at the wrong offset.

| | before 7.2 | 7.2 and later |
| --- | --- | --- |
| line number in INFO and ERROR | 2 bytes | 4 bytes |
| user type in COLMETADATA | 2 bytes | 4 bytes |
| row count in DONE | 4 bytes | 8 bytes |
| ALL_HEADERS before a request | absent | present |

The login is answered with the version the client offered, never a newer one.
Four other differences from a real server turned up alongside: PRELOGIN
answers only the options the client asked about, the SSPI token produced on
the completing step is not sent, the session id goes in the packet header from
the login response onward, and TLS session tickets are off.

All of that came from one symptom. The legacy "SQL Server" ODBC driver
reported a server older than 6.5, then a 7,536,649-byte header block inside a
116-byte packet, then a protocol error in the stream, each error appearing
only once the one before it was fixed. Reading a real SQL Server's answer to
the same driver through a proxy said what it had done differently each time.

Verified end to end against a catalog of five tables: the legacy ODBC driver,
ODBC 17, ODBC 18, .NET SqlClient and MSOLEDBSQL all list 5 tables and 49
columns with types intact, and MSOLEDBSQL answers all 15 of its schema
rowsets.

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
- Text past 4000 characters cannot declare a size, so it takes the MAX form: the
  column declares 0xffff and the value arrives as an 8-byte total length, then
  length-prefixed chunks, then a zero terminator. An API array flattened to JSON
  reaches this routinely.
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
- TOP applies after ORDER BY, not before. `SELECT TOP 3 ... ORDER BY score DESC`
  means the three highest scores, not three arbitrary rows put in order.
- NULLs sort first ascending and last descending, which is what SQL Server does
  and not what a naive sort does.
- AVG over an integer column returns a truncated integer. That is what SQL
  Server does, and it is matched rather than improved so a client computing
  against both gets the same number. SUM over an integer column is the one
  deliberate deviation: it widens to 64 bits, because SQL Server's overflow
  would surface here as an encoding failure partway through a result set rather
  than as a SQL error.
- An un-aliased aggregate has no column name at all. SQL Server leaves it
  unnamed and clients render a blank heading, so an empty string is the
  faithful answer rather than an invented one.
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
