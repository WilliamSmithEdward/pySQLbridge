# pySQLbridge

Answer SQL Server's wire protocol convincingly enough that Excel and Power BI
connect to a JSON file, a CSV, a workbook, an Access database or an HTTP API
and see a database. Read-only, so only the SELECT surface has to hold up.

## Status

A real SQL Server client lists the tables, then selects from a CSV file, a JSON
file, an Excel workbook, an Access database or a live HTTP API over the wire,
with column types, NULLs, WHERE and TOP. Parameterised queries work, which
matters because clients send those as RPC calls to sp_executesql rather than as
SQL batches.

The SQL covers what a client and a person actually send: joins, GROUP BY with
HAVING, DISTINCT, OFFSET/FETCH, CTEs, subqueries and derived tables, CASE, CAST,
expressions and aliases in the select list, scalar subqueries, UNION, EXCEPT,
INTERSECT, correlated subqueries, window functions, and 86 scalar functions
over 10 aggregates. All 941 queries in `scripts/differential.py` answer
identically to SQL Server 2025, declare the same kind of column for each
answer, and where both refuse, refuse with the same message number. Anything
it cannot answer is refused by name rather than answered wrongly.

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
| Result set encoding: int, nvarchar, float, datetime, null | done |
| CSV and JSON sources with type inference | done |
| Excel workbooks, one table per sheet | done |
| Access databases, one table per table and saved query | done |
| SELECT with a column list, TOP, WHERE and ORDER BY | done |
| Column aliases, and whole-table aggregates | done |
| RPC, so parameterised queries work | done |
| INFORMATION_SCHEMA tables, columns, schemata | done |
| HTTP API sources: nested, paged, raced, cached | done |
| Configuration file | done |
| Single-file Windows executable | done |
| Joins, GROUP BY, HAVING, DISTINCT, OFFSET/FETCH | done |
| CTEs, subqueries, derived tables, CASE, CAST, functions | done |
| ORDER BY an alias, an expression or a position | done |
| XML, HTML and CSV sources, over HTTP or off a disk | done |
| Nested arrays expanded into child tables | done |
| System stored procedures, ODBC and OLE DB | done |
| Scalar subqueries, in any clause that takes a value | done |
| Multi-statement batches, variables, IF, EXEC of a string | done |
| Temp tables a session makes, fills and drops | done |
| The session reset a pooled client asks for | done |
| CROSS APPLY over a table written out with VALUES | done |
| A table written out with VALUES in a FROM | done |
| Subqueries that read the row around them | done |
| UNION, UNION ALL, EXCEPT, INTERSECT | done |

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

## Installing

```bash
pip install pysqlbridge
```

That puts a `pysqlbridge` command on the path, which takes the same arguments
the module does:

```bash
pysqlbridge --config examples/tables.json
```

Python 3.10 or newer. `cryptography` comes with it, for the self-signed
certificate the login tunnel needs; `pyopenvba` comes with it, for reading
Access databases, and is pure Python with no dependencies of its own;
`pywin32` comes with it on Windows, for Windows Authentication. Only the last
is platform-bound, and none of them is needed to read a CSV or a JSON file: a
bridge serving those over SQL authentication runs anywhere Python does.

The executable below needs no Python at all on the machine it runs on, which
is the reason it exists.

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
    { "name": "sales",  "csv":  "data/sales.csv", "delimiter": ";" },
    { "name": "cities", "json": "data/cities.json" },
    { "excel": "data/budget.xlsx" },
    { "access": "data/club.accdb" },
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

`"delimiter"` is told rather than sniffed. Half of Europe writes a CSV with
semicolons because the comma is its decimal point, and read with commas such
a file comes back as one column called `id;name` holding `1;ada`: no error,
no missing rows, and nothing to act on. Guessing from the look of a file is
wrong on one in fifty and silent about which, so it is a setting. One
character, and only on a `"csv"` table.

Paths resolve against the configuration file, so a config and its data move
together. `name` is optional for files and defaults to the stem; an HTTP source
must be named, because a URL has no obvious table name.

An HTTP source fetches JSON and shapes it with the same rules as a JSON file.
Fifty-four public endpoints were surveyed to decide what those rules are; see
[docs/api-shapes.md](docs/api-shapes.md). `scripts/api_survey.py` grades the
whole pipeline against 282 of them, through to the SQL answers agreeing with
the data they came from.

| Key | What it does |
| --- | --- |
| `url` | one URL, or a list of them for a load-balanced set |
| `path` | a dotted route to the rows; a numeric segment indexes a list |
| `records` | `array`, `single`, `values`, `entries`, `columns` or `scalars` |
| `flatten` | nested objects become dotted columns, on by default |
| `columns` | which columns to keep, for a record that is too wide |
| `next` | a dotted route to the next page's URL |
| `max_pages`, `max_rows` | bounds on following it |
| `format` | `json`, `xml`, `html` or `csv`; sniffed by default |
| `expand` | a nested array becomes a table of its own, on by default |
| `paging` | a position to advance, for an API that reports one |
| `ttl`, `timeout`, `headers` | reuse, deadline, and anything an API needs |
| `auth` | a credential, described below |

These are the only keys read here, and a key that is not one of them is
refused rather than ignored, naming the nearest one that is: an option written
beside `"http"` instead of inside it leaves a config that looks right and a
source that behaves as though the line were absent.

A value written as text is read as a number only when nothing it spelled is
lost by doing so. CSV and XML have no types at all, so a number there can only
arrive as text and has to be recognised; JSON has types per value, and a string
of digits is a string the source chose to write. One rule serves both: believe
the source unless the conversion is exact. Measured over 239 public API
responses, the old rule typed 791 columns numeric from text and 377 of them
lost something. Coinbase quotes rates to 19 significant digits as JSON strings
and a float holds 17; ipapi writes `utc_offset` as `"-0700"`, and -700 is a
different thing.

`records` defaults to `auto`, which scores the readings of the document and
refuses a weak winner rather than guessing. Naive detection is the trap here:
over 85 public endpoints, a detector that simply looked for an array found one
for 84 of them and was frequently wrong, serving paging links, a nested field,
or a rejected request's `errors` list as the table. Scored detection is right
on all 40 endpoints whose correct answer was written down first, and costs
0.01 to 0.06 ms. Name `records` explicitly to turn detection off.

The hard case is a map of same-typed scalars. Frankfurter answers with 29
numbers under `USD`, `GBP` and `SEK`; sunrise-sunset answers with 10 strings
under `sunrise`, `solar_noon` and `day_length`. Both are a map of scalars, so
neither the value types nor the number of keys separates them. One rule does:
a key that identifies a row was made by whatever makes that domain, so every
key in the map shares a shape, and a key that names a field was chosen by a
person writing a schema, so they share nothing but being words.

Agreement only counts once there are enough keys for it to be unlikely. Two
keys sharing a shape is a coincidence that happens constantly: across those
116 responses there were 44 distinct two-key objects, 11 of them with keys of
one shape, and not one was rows. `lat` and `lng` are three lower-case letters
each; so are `sha` and `url`, `svg` and `png`. From three keys up, the same
corpus had 7 agreements and every one was rows.

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

CSV is the exception: it announces itself nowhere in its bytes, so it is asked
for rather than sniffed. `"format": "csv"` reads a response whose first line is
its header, which is what an open data portal serves, and from there it is the
same table as any other source. A byte order mark is stripped, a quoted field
may hold a comma, and a line with the wrong number of fields is refused with
its line number rather than padded with NULLs.

```json
{ "name": "passengers", "format": "csv",
  "http": "https://example.org/titanic.csv" }
```

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

### Excel and Access

Both hold more than one table, so both produce more than one. A workbook makes
a table per sheet and one per table drawn on a sheet, and a database makes one
per table and per saved query, each named after itself:

```json
{
  "tables": [
    { "excel": "data/budget.xlsx" },
    { "excel": "data/budget.xlsx", "sheet": "Q1", "name": "first_quarter" },
    { "excel": "data/budget.xlsx", "table": "Headcount" },
    { "excel": "data/budget.xlsx", "sheet": ["Q1", "Q2"] },
    { "access": "data/club.accdb" },
    { "access": "data/club.accdb", "table": ["Members", "Rooms"] }
  ]
}
```

Point at a file and you get everything in it. `"sheet"` and `"table"` narrow
that to what they name, one name or a list of them, and naming tables alone
serves no sheets, which is how to say "the tables, not the tabs they sit on".
`"name"` renames what is left, and is refused where more than one thing is,
because there is one name and four tables and three of them would end up
called something invented.

What Excel calls a table, and the object model calls a ListObject, is a range
somebody drew and named. It is stored in a part of its own rather than in the
sheet, so it carries three things a sheet cannot: a name, its own column
names, and where it stops. A tab and a table on it are both served, and where
the tab holds only that table the two are the same rows under two names.

A tab is not served beside its tables when the file says its own reading
would be wrong, and it says so four ways. A table named after its own tab
replaces it, because naming the table after the sheet is what people do and
one name cannot mean two tables. A tab carrying more than one table has more
than one header row, so no reading of it as a single table is right: serving
one would put the second table's headings in as a record and drag its numbers
over to text with them, a wrong answer with nothing on the wire to show it. A
table ending in a totals row would hand the tab a record called Total, counted
by every count and added into every sum. And a table with no header row leaves
the tab nothing to name its columns with, so the tab read whole would take its
first record for the headings. The log says which reason applied, and naming
the tab with `"sheet"` serves it regardless.

A workbook is read directly. An `.xlsx` is a zip of XML and the standard
library opens both, so nothing has to be installed to read one. That matters
more than it sounds: the usual way to read a workbook on Windows is the Access
database engine, which is a separate download that installs in one bit width
and refuses to load into a process of the other, and a bridge whose promise is
"point it at a file" cannot begin by asking for a driver. Neither reader below
touches it, or COM, or ODBC. An `.xls` saved by an old Excel is a different
format and is refused with a message saying so.

The first row of a sheet names the columns. That is a rule rather than a
reading: a sheet with a title above its headings gets the title, which is
visible at once and fixed by drawing a table over the headings and the rows
under them, where guessing which row looked most like headings would be wrong
occasionally and silently. A table is the answer to that layout, because it
says where the headings are instead of leaving it to be inferred.

A tab that cannot be read as one table is left out, and the rest of the
workbook is served. That covers a value to the right of the last heading,
which nothing could name, a heading that cannot be a column name, and more
columns than a result can carry. The log names the tab and the cell, and says
what was served in its place: a tab that carries a table is served as that
table. Name the tab with `"sheet"` and the reason comes back as an error
instead, and a workbook left with nothing to serve is refused with every
reason. Only the layout is forgiven. A cell that cannot be read at all, or a
file that is not a workbook, still refuses the whole file.

A cell that is empty is NULL, a row that is not in the file is not a row, a
row of nothing but `#DIV/0!` is a row of NULLs, and a formula arrives as the
value Excel last worked out for it.

Dates are the part of the format worth knowing about. Excel stores a date as a
number of days and the only thing that makes 45306 a date rather than the
number 45306 is the number format its style points at, so the styles are read
to find out. Day 60 is Excel's 29th of February 1900, a day that did not
happen, and is refused rather than served as some other date; a workbook saved
by Excel for Mac before 2011 counts from 1904 and is read against that epoch,
because a date read against the wrong one is out by four years and a day with
nothing looking wrong.

An Access database is read in pure Python too. The `.mdb` and `.accdb` formats
are undocumented and page-structured, so the reading is not done here:
[pyOpenVBA](https://github.com/WilliamSmithEdward/pyOpenVBA) implements the
Jet storage engine and this maps what it hands back onto the columns it
serves. It is a dependency with no dependencies of its own and the same Python
floor as this, so nothing else comes with it.

The alternative was the database engine Microsoft ships, through COM or ODBC,
and the reasons against it are the reasons against it for the workbook: a
separate download, installed in one bit width and refusing to load into a
process of the other, and absent from Linux entirely. Reading the file instead
means an Access database is served on any machine the rest of this runs on,
that the suite tests it on every one of them rather than skipping where an
engine is missing, and that the single-file executable needs nothing installed
beside it.

The file is read once into memory and never written. That is stronger than
opening it read-only: there is no handle held, no lock file beside it, and
somebody can have the same database open in Access while it is being served.

Saved queries are served alongside the tables, and run where they were
written. An Access query says `IIf` and `Nz` and joins its strings with `&`,
none of which is what a client sends this, so the query is answered in Access
SQL and its rows arrive here already worked out. Only the ones that read: an
update or a delete query is a statement rather than a table, and running one to
find out what it returns would change the file. A query has no stored schema,
so its columns are read off its rows, and one that answers nothing has no
columns to be described by and is passed over.

Types come from the database, because unlike a CSV it has some. A text column
stays text where every value in it happens to be digits, which inference alone
gets wrong, and Access's two-byte Integer stays two bytes. Jet declares a text
length in bytes, two to a character, so `TEXT(50)` arrives as 100 and taking
the number at face value would give a column half the width it should be.
Where there is no type here that is theirs the values decide instead, which
covers Yes/No, currency and the GUID: a currency column becomes a float where
every value survives one exactly and text where one of them would lose digits,
which is the rule a number arriving as text already goes through. A column
holding an OLE Object is refused by name, because there is no column type here
that carries an embedded file and answering NULL would be a quieter lie.

`scripts/office_probe.py` reads a workbook or a database with these readers and
prints what came out: the tables, the type each column was given, and the first
few rows. For a file that will not serve, or serves something unexpected; if it
prints what you expect and a client does not see it, the problem is past the
reader.

### Nesting

A row cannot hold a list. Over 116 public API responses, a third of the tables
built from them had an array in every row: a character has episodes, a cart
has products, a recipe has ingredients. Serving those as JSON text makes a
column nobody can query, and dropping them loses the data.

An array becomes a table of its own, named `parent_column`, with the parent's
key beside every element:

```
characters              20 rows   id, name, status, species
characters_episode     242 rows   characters_id, episode_index, value
```

```sql
SELECT c.name, COUNT(*) AS episodes
FROM characters c JOIN characters_episode e ON e.characters_id = c.id
GROUP BY c.name ORDER BY episodes DESC
```

It recurses. A cart holds products and a product holds reviews, so there is a
`carts_products` and a `carts_products_reviews`, and every level carries the
identity of the levels above it: a review row has `carts_id`,
`products_index` and `reviews_index`, so it can be joined straight back to
the cart as well as to the product. Four levels deep and 64 tables per source
are the bounds; the corpus produced 123 child tables from 114 parents, 67 of
them two or more levels down.

The key is found by testing, not by naming: the first column whose values are
present in every row and never repeat identifies those rows, which is what a
key is. A column called `id` that repeats is not one, and a `slug` that does
not repeat is. It is carried under the parent table's name, because an
element usually has an `id` of its own and writing both as `id` would lose
the parent and make the obvious join match the wrong thing.

Nothing that became a table is also left behind as JSON text. Set
`"expand": false` on a source to keep the old behaviour.

Depth was never the problem. Nothing in that corpus nested deeper than the
flattener already goes.

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

A window function is worked out over the rows the `WHERE` kept, before the
sort and before `TOP`, and answers once per row. Where a query does not name a frame it gets the one SQL Server uses: the
whole partition where the `OVER` clause says no order, and everything up to
and including this row's ties where it does. That is why
`SUM(x) OVER (ORDER BY id)` is a running total and
`SUM(x) OVER (ORDER BY team)` is not. A query may name one instead, as
`ROWS` or `RANGE`, which is how `LAST_VALUE` is told to look at the whole
partition rather than stopping at this row. A window in the `ORDER BY`
rather than named in the select list, and a window beside a `GROUP BY`, are
each refused by name rather than answered differently. `NTILE`'s count may
be a variable and must be an integer above nought. `LAG` and `LEAD` read
their offset and their default from the row asking, so `LAG(x, @n)` and
`LAG(x, rank)` both work, a null offset gives the default, and a negative
one is refused.

A cast to an integer type is held to the range of that type, so
`CAST(300 AS tinyint)` is an error rather than 300, and `TRY_CAST` and
`TRY_CONVERT` answer NULL wherever `CAST` refuses. That pair matters more
here than on a real server: a source read off a CSV or an API holds whatever
it holds, and one value that will not convert should not cost the answer.
A cast to text keeps as many characters as its size says: thirty when it
gives none, 128 for `sysname`, and all of them for `nvarchar(max)`,
`varchar(max)`, `text` and `ntext`. A cast to `decimal(p,s)` rounds to its
places, half away from nought, and refuses a whole part too long for it;
`money` keeps four places; a `date` keeps the day and drops the time. A
bit becomes the text `1` or `0`, and the words `true` and `false` become
bits.

The date functions are measured the same way, and most of what they do is
not guessable. `DATEDIFF` counts the boundaries between two moments rather
than the time between them, so a minute either side of midnight is one day
and a whole day inside one date is none. `DATEADD` holds a month back rather
than letting it spill, so a month after the 31st of January is the 28th of
February. Weeks start on Sunday and week one is whichever week holds the 1st
of January, so 2026 runs to week 53. `GETDATE()` is taken once for the whole
statement, because a filter comparing each row against its own slightly later
now would keep different rows for no reason.

Where a `UNION` puts two columns together, the result gets one type, chosen
across every branch by SQL Server's data type precedence and measured against
it pair by pair. A union of an integer column and a float one is float and
keeps the fraction rather than truncating it to the first branch's type, and a
value that will not convert is refused with the number and wording a real
server refuses it with.

No client credential is handled here. The login carries a SPNEGO token and
SSPI's `AcceptSecurityContext` validates it against the local account database
or the domain. Credentials for the APIs this bridge reads from are separate,
and are described above.

### Logins

Windows Authentication is what a client uses by default and needs nothing
configured. A username and a password is the other way in, and it is off
until a configuration names the accounts that may use it:

```json
{
  "logins": [
    { "user": "reader", "password": "${env:BRIDGE_READER_PW}" },
    { "user": "excel",  "password_hash": "pbkdf2_sha256$210000$c2FsdA==$..." }
  ],
  "tables": [
    { "name": "people", "json": "data/people.json" }
  ]
}
```

This is the opposite direction from "Credentials" above. That one is this
bridge proving itself to an API it reads from; this one is a client proving
itself to this bridge.

A bridge with no `"logins"` refuses every username and password there is.
What a client sees is `Login failed for user 'x'.` with message number
18456, which is the number and the words a real server gives, measured
against SQL Server 2025. An unknown name and a wrong password get that same
answer and take the same time to get it, so a refusal never says which of
the two it was, and the server log names the login that was refused and
never what was offered for it.

Write `"password_hash"` rather than `"password"` anywhere the file will be
committed. `pysqlbridge --hash-password` asks for a password and prints the
hash to paste in; it asks rather than taking an argument because a password
written on a command line is in the shell's history and, while it runs, in
the process list. A `"password"` is read from the environment through
`${env:NAME}`, and a variable that is not set stops the server at startup
naming the variable, rather than refusing logins later for a reason nobody
can see. Usernames are matched without regard to case, passwords exactly.

None of this makes the connection private on its own; see the note on the
password field in the protocol notes below.

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
| expressions | arithmetic, `+` on text, `CASE` in both forms, `CAST`, `CONVERT` with its style, `TRY_CAST`, `TRY_CONVERT` |
| functions | `LEN` `UPPER` `LOWER` `LTRIM` `RTRIM` `TRIM` `LEFT` `RIGHT` `SUBSTRING` `REPLACE` `REVERSE` `CHARINDEX` `PATINDEX` `CONCAT` `CONCAT_WS` `SPACE` `STR` `STUFF` `REPLICATE` `TRANSLATE` `ASCII` `CHAR` `UNICODE` `NCHAR` `ISNULL` `COALESCE` `NULLIF` `IIF` `CHOOSE` `GREATEST` `LEAST` `ABS` `SIGN` `FLOOR` `CEILING` `ROUND` `POWER` `SQRT` `SQUARE` `EXP` `LOG` `LOG10` `PI`; `TRIM`, `LTRIM` and `RTRIM` take the characters to take off, as `TRIM(chars FROM x)` with `BOTH`/`LEADING`/`TRAILING` or as a second argument |
| dates | `GETDATE` `GETUTCDATE` `SYSDATETIME` `SYSUTCDATETIME` `CURRENT_TIMESTAMP` `DATEADD` `DATEDIFF` `DATEPART` `DATENAME` `YEAR` `MONTH` `DAY` `EOMONTH` |
| aggregates | `COUNT` `COUNT_BIG` `SUM` `MIN` `MAX` `AVG` `STDEV` `STDEVP` `VAR` `VARP`, whole-table or per group, and inside a larger expression: `MAX(a) - MIN(a)`, `SUM(a) / COUNT(*)`; `STRING_AGG` with `WITHIN GROUP` |
| windows | `ROW_NUMBER` `RANK` `DENSE_RANK` `NTILE` `LAG` `LEAD` `FIRST_VALUE` `LAST_VALUE`, and the aggregates, over `OVER (PARTITION BY ... ORDER BY ... ROWS/RANGE ...)` |
| where | `=` `<>` `<` `<=` `>` `>=`, `LIKE` with `ESCAPE`, `IN`, `BETWEEN`, `IS NULL`, `AND` `OR` `NOT` |
| joins | `INNER`, `LEFT`, `RIGHT`, `FULL`, `CROSS`, `CROSS`/`OUTER APPLY` of values or of a select, tables listed with a comma, table aliases, and `WITH (NOLOCK)` and its like ignored |
| grouping | `GROUP BY` a column or an expression over one, a select list and an `ORDER BY` reading anything the grouping fixes (`rank + 1` or `UPPER(team)` beside `GROUP BY` the column, `(rank % 2) * 10` beside `GROUP BY rank % 2`, `ORDER BY` a grouped column that is not selected), `HAVING` naming an aggregate or its alias |
| rest | `DISTINCT`, `TOP` with `PERCENT` or `WITH TIES`, `ORDER BY`, `OFFSET`/`FETCH`, `WITH`, derived tables, `IN`/`EXISTS`/`ANY`/`ALL`/scalar subqueries, `UNION`/`EXCEPT`/`INTERSECT` with either part in brackets, `OPTION (...)` ignored, `@@VERSION` and friends |
| batches | several statements in one send, `DECLARE` of one variable or several, `SET` and `SELECT` into variables with `=` or `+=` and the other compound operators, `IF`/`ELSE` with `BEGIN` blocks, `EXEC` of a string and `sp_executesql` with its values |

Nothing that writes is supported, apart from the temporary tables a
connection builds for itself: a client makes one, or has a `SELECT ... INTO`
make it out of the answer, fills it naming the columns or taking them in
order, reads it back and drops it, and nothing a source holds is touched.
An INSERT, UPDATE, DELETE, MERGE, TRUNCATE, DROP or ALTER naming anything
else is refused and says so, and so is a GRANT, REVOKE or DENY: there are no
permissions here to change and every source is read-only for everyone who
can reach it. Passing any of them over would report that it worked, and a
person told their DELETE succeeded has been told something untrue about
their data. The statements a client sends to open a session, SET and USE and
the rest, are still passed over.

Transactions are accepted and counted. `BEGIN TRANSACTION`, `COMMIT`,
`ROLLBACK` and `SAVE TRANSACTION` move `@@TRANCOUNT`, which is kept per
connection: nested begins count up, a rollback to a savepoint releases that
savepoint and every one marked after it, and a commit or rollback with
nothing open is refused with SQL Server's own number and words. A client's
transaction API reaches the same count as the statements do, because it is
answered by them: a .NET `BeginTransaction()` arrives as its own packet
rather than as SQL, and is turned into `BEGIN TRANSACTION` here. What a
transaction does not do is undo anything, because nothing here is written.
A rollback restores the count, not the rows a batch put in a temporary
table, and a distributed transaction is refused rather than joined.

A client that pools its connections asks for the session to be reset on the
first message it sends over one it has taken back out, and that is answered
now. Everything the last user of that connection left behind goes with it:
the temp tables it made, the transaction it left open, and the SET options
it changed. Measured on SQL Server 2025 by closing a pooled connection and
opening another with the pool held to one, so the same physical connection
comes back. Until this the request was read and ignored, so one user's
scratch tables and settings were still there for whoever had the connection
next, which is the wrong answer and somebody else's data. The other form of
the request, which keeps the transaction and clears the rest, is answered
too; a client only sends that one for a distributed transaction, and those
are refused here.

A batch does not always stop at its first error, because a real server
does not. Which happens depends on the error, and each number was measured
on its own: after 3902, 8134, 2812 and ten others only the failing
statement ends and the rest of the batch runs, with the error arriving
among the answers rather than in place of them; after 208, 245, 628 and
five others the batch stops, and what the statements before it answered is
still returned. A client reads results, then the error, then more results,
in the order they happened. An error outside both sets ends the batch with
nothing kept, which is what a real server does with one it cannot compile.
An error raised inside an `EXEC` of text ends the text and no more, and the
batch that ran it carries on; 241, 245, 281, 628, 3623, 8114 and 8169 are
the ones that reach out of the text and end that batch too.

A batch that cannot be T-SQL at all runs none of its statements, because a
real server compiles the whole batch before it runs any of it. Measured:
`CREATE TABLE #t (a int); SELECT * FROM` is msg 102 and leaves no `#t`,
where this made the table and then failed, and a batch cut off after
`DECLARE @p`, or a `SET` with a `SELECT` after it, used to answer as though
it had worked. The text is checked before anything is parsed, and only
where the text alone settles it. A quote or a bracketed name left open is
105 and then 102 near what it left open, a comment left open is 113
(comments nest), and a keyword such as `FROM` where a name or a value
belongs is 156 near it. Text that stops after `WHERE`, a comma, an open
bracket or a `BEGIN` with no `END` is 102 near its last word, quoted as it
was written. A `SET` that begins its statement and stops short is 156 near
`SET`, and one belonging to an `UPDATE` is 102. The first error is the one
a client raises, and a second goes out behind it the way a real server
sends one. Every rule and every word treated as wanting more was measured,
and each construct that looked like it might trip one, such as `IS DISTINCT
FROM`, `FOR SYSTEM_TIME ALL WHERE` or a cursor's trailing `FOR UPDATE`, was
parsed by a real server under `SET PARSEONLY ON` and is left alone. A real
server that has found one error reads on and can report another from later
in the batch. This reports the first, and the lexer's error behind it where
the text ends inside a quote or a comment.

A SELECT with no FROM that reads a column says so in SQL Server's words
rather than this project's: 207, "Invalid column name", naming the first
column it cannot find, 4104 where the column is qualified, 263 for a star
and 107 for a star with a prefix, all measured. The functions T-SQL writes
with no brackets, `CURRENT_TIMESTAMP`, `SYSTEM_USER`, `USER`,
`CURRENT_USER` and `SESSION_USER`, are values rather than columns there
and everywhere else, unless bracketed as `[user]`; they were refused.

A variable nothing declared is settled at the same time and the same way:
msg 137, or 1087 where a table goes, and none of the batch runs. It used to
read as null, so a variable name spelt wrong came back as an empty answer
instead of an error. Declaration follows the text rather than what runs,
as it does on a real server, so `IF 1 = 0 BEGIN DECLARE @x int END; SELECT
@x AS v` answers. A DECLARE's own variables are not declared inside it, so
`DECLARE @a int = 1, @b int = @a` is 137 on `@a`. Each statement that reads
one reports it, an IF's condition and the statement it guards separately.
The values a client sends with a parameterised statement declare their
names, and so does a procedure's header. An EXEC's `@p = 1` names the
procedure's parameter rather than a variable of the batch, while
`EXEC @rc = p` reads `@rc`. All of this was measured. `scripts/replay.py`
supplies a parameter the log did not keep as null, because the log records
a parameterised statement without its values, and it says how many queries
needed that.

A SELECT that assigns from a table assigns once for every row, and each
row reads what the one before it left, so `SELECT @s = @s + name + ','
FROM people ORDER BY id` builds the list up. It used to read every row with
the value from before the statement and keep the last row's answer, which
gave only the last name. Several variables in one SELECT are assigned left
to right, so `SELECT @a = 1, @b = @a + 1` leaves `@b` at 2. A TOP applies
before any row assigns, a WHERE that keeps no row leaves every variable as
it was, and an assignment made before a failure part way through is kept.
`SET @a += 2` and the other compound operators are the variable with the
operator applied to all of what follows, and `DECLARE @a int = 1, @b int =
2` gives each its value in turn. Both used to be wrong: the first was
passed over as a SET option and left `@a` alone, and the second refused,
or where the first variable had no value, left the rest null. A SELECT
that assigns and reads in one list is msg 141, settled while compiling.
The one form still refused is a GROUP BY whose items read a variable they
assign, because each group would need what the group before it assigned.

Text an `EXEC` runs is a batch of its own, in a scope of its own. It sees
none of the variables of the batch that ran it and leaves none behind; what
it is told is the parameters `sp_executesql`'s declarations name, given by
name or by place and held as the type declared for each; and a parameter
marked `OUTPUT` goes back into the variable it came from once the text has
run. The text itself may be worked out rather than written out, so
`EXEC(@sql)` and `EXEC('SELECT ' + @what)` run what they build. What it is
handed is checked the way a real server checks it: a parameter nothing
supplied is msg 8178, one argument too many 8144, a value after a
`@name = value` 119, `OUTPUT` on a constant 179, `OUTPUT` for a parameter
not declared as one 8162, and a statement that is not `nvarchar` 214.

A variable holds the type it was declared with. Every value given to it,
by `DECLARE`, `SET` or `SELECT`, is converted the way a cast converts, so
an `int` given 5.7 holds 5, a `varchar(3)` given a name holds its first
three letters, a `varchar` with no size holds one, and a `decimal(5,2)`
rounds. Each row of a SELECT that assigns is held as the type before the
next row reads it, so a running sum into an `int` drops the fractions as
it goes, the way a real server's does. A value too big for the type is
refused with the error the cast gives and the variable keeps what it had.
A variable's name is matched without regard to case.

A read that fails while working out a row, a divide by zero or a conversion
that will not go, has already had its column shape sent by the time it
fails, so a real server puts an empty result set naming those columns in
front of the error. That shape is worked out from the select list without
running the query and goes out ahead of the error here too, whether the
read failed on its own or among a batch's answers. A read that fails while
binding, an invalid object or an unknown column, sends none, because it
never got that far: the fourteen numbers that keep the shape are the ones
that come from evaluating a row, measured one at a time.

`RAISERROR` raises the error a client asked for: message number 50000 with
the text, the severity and the state it was given, catchable, readable
through `@@ERROR`, and the batch carries on past it the way a real server's
does. It was passed over in silence before, so a client that raised an
error deliberately was told everything had gone well. Severity 10 and under
is a message rather than an error on a real server, and there is no way to
send one from here, so those go over the way `PRINT` does.

`THROW` raises one too, and differs in what the batch does next: a real
server stops there, where it carries on past a `RAISERROR`. What the
statements before it answered is still returned, the number is the one it
was given rather than 50000, the severity is always 16, and the state is
its third argument. A bare `THROW` inside a CATCH re-raises what was
caught, whole, and ends the batch even where that error on its own would
not have, which is why the error carries a mark and the number is not
asked. A number outside 50000 to 2147483647 is refused with 35100 as a
real server refuses it, and a bare `THROW` with no CATCH around it is
10704, which a real server settles while compiling, so nothing in the
batch answers. Inside an `EXEC` of text it ends the batch that ran the
text as well, where a `RAISERROR` in one does not.

`SET XACT_ABORT ON` changes where a batch stops rather than what it keeps.
Measured with the setting on, one number at a time: twelve of the thirteen
errors a batch would otherwise run on past end it instead, and what the
statements before them answered is still returned. The exception is 3701,
dropping a table that is not there, which is the one of them a real server
reports at severity 11 rather than 16. An error a client asked for is not
the setting's to change: a `RAISERROR` still lets the batch run on and a
`THROW` still ends it. A CATCH still catches. The setting belongs to the
connection rather than the batch and survives into the next one, and
`SET XACT_ABORT OFF` puts it back.

A batch that stops at an error leaves no transaction open, and takes
every nesting level at once, so `@@TRANCOUNT` reads nought afterwards
rather than what it read before the failure. A batch that carries on
past its error keeps the transaction. Two kinds of error do not roll one
back: a compile error, 102, 105, 113 or 156, each measured with the setting
off and on, because nothing compiled and so nothing ran to undo; and 208
with `SET XACT_ABORT` off, which is the one number the setting changes the
answer for. A refusal of this server's own keeps the
transaction too; a real server has no counterpart for one, so that is
chosen rather than measured, and it is the safer way round, because a
client committing a transaction this had thrown away would read 3902.

`@@ERROR` reads the number of the last statement that failed, and nought
where it worked, so `IF @@ERROR <> 0` does what a client means by it. It
answered NULL before, and nothing is ever unequal to NULL, so that check
quietly never fired. A DECLARE with no value leaves it standing, the same
exception `@@ROWCOUNT` makes for one; an IF and an `EXEC` of text leave it
to the statements they ran; a TRY clears it once its CATCH is done, and a
CATCH reads the number that sent it there. What a TRY answered before it
failed is kept too, because a real server has already sent those rows by
the time it reaches the failure.

Inside a CATCH, `ERROR_NUMBER()`, `ERROR_MESSAGE()`, `ERROR_SEVERITY()` and
`ERROR_STATE()` answer what sent it there, and go on answering it for the
whole block where `@@ERROR` does not: a statement running inside the CATCH
clears `@@ERROR` and leaves these alone. Outside a CATCH every one of them
is NULL. `ERROR_PROCEDURE()` is always NULL because nothing here runs in a
procedure, and `ERROR_LINE()` is as well: a real server answers the line
within the batch, nothing here counts lines, and a number would be invented.

### Measured against SQL Server 2025

The semantics are not chosen, they are compared. `scripts/differential.py`
writes a fixture twice, once as JSON for this and once as INSERT statements
for SQL Server, and `scripts/differential.ps1` runs 941 queries against both
and reports where the answers differ. Where both refuse, it compares the
number as well as the words: a client shows it, and a divide by zero
reported as msg 208, invalid object name, sends whoever reads it looking
for a table that was never the problem. The rows sit on the edges rather than
the middle: NULL in every position that treats it specially, text differing
only in case, an empty string, a zero, a negative, and a key that matches
nothing. A third table holds values far enough apart that adding them in one
order and the other give different floats, which is how the order the
arithmetic runs in gets compared at all. A fourth is written as a workbook
rather than as JSON, because a workbook is the source that can hand over a
real moment, and there is no other way to put a datetime column on this side
of the comparison.

Adding the workbook table found four things, every one of them wrong here.
`WHERE hired = '2024-01-15'` answered nothing, because Python calls a datetime
and a string unequal rather than refusing to compare them, so the row simply
did not match; `WHERE hired > '2024-01-15'` was right only by the accident of
an ISO date sorting the same way as its own spelling, and stopped being right
at `'2024-6-1'`, which a real server reads and this refused. `WHERE hired IN
('2024-01-15')` matched nothing for the same reason as the first.
`CAST(hired AS int)` refused a conversion a real server answers with 45304.
None of the four was reachable before, because no source produced a datetime
column: JSON has no dates and a CSV's are text.

A query named `mine-only-` is one this answers where a real server refuses,
on purpose and with the reason written beside it. The harness reports those
separately rather than counting them as agreement, and complains if one
stops being answered here or starts being answered there, so that a
divergence nobody decided on cannot hide among the ones somebody did.

`scripts/replay.py` asks a different question. It reads a log written by
`--log` while a real client was connected and answers every query in it
again, so what comes back is not whether an answer is right but whether
there is one, over what a client actually sends rather than what anyone
thought to write down. SSMS sends 93 distinct queries before it will draw a
tree, all of which are answered, and any that refuse are the work list.

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

`scripts/truncations.ps1` asks about text a real server refuses. It sends
every query in the battery cut short at each word, 6,862 prefixes, to both
servers, and sorts each prefix into one of four outcomes. Both refusing with
the same number is agreement. Both refusing with different numbers is a
client shown the wrong message. A real server answering what this refuses
is usually something not supported here. This answering what a real server
refuses is the worst of the four, because it is a truncated query given an
answer that nothing about it admits. Its first run found 175 of those,
among them a CREATE TABLE cut off after a comma that made a table, and
5,447 prefixes refused with the wrong number, nearly all of them syntax
errors reported as 50000. Checking a batch for syntax before running any of
it brought those to 18 and 975, and left the 36 a real server answers and
the 880 both answer exactly where they were. The 18 are bind errors, a
qualifier that names no table in the query and a variable never declared,
and most of the 975 are bind and type errors too. The syntax among them
needs the grammar's context, such as a JOIN with no ON or a TOP with
nothing after its count.

One divergence is known and open. A literal written with a decimal point is
`decimal` on a real server and a float here, so arithmetic over one is
binary rather than exact:

| | SQL Server | here |
| --- | --- | --- |
| `0.1 + 0.2` | `0.3` | `0.30000000000000004` |
| `1.005 * 100` | `100.500` | `100.49999999999999` |
| `100.0 / 6` | `16.666666` | `16.666666666666668` |

Closing it needs more than exact arithmetic. A decimal division's scale is
`max(6, s1 + p2 + 1)`, where `p2` is the *declared* precision of the divisor
rather than anything about its value: `1.0 / 3` gives six decimal places and
`1.0 / an int column` gives twelve, because an `int` is `decimal(10, 0)`
whatever it holds. Getting that right means carrying declared types through
the expression tree, which nothing here does; `result_kind` says what it is
sure of and stops. Half of it, exact for literals and wrong for columns,
would be the only approximate thing in the project, so it is left whole and
written down instead.

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

A table can also be written out in the FROM with `VALUES`, as
`FROM (VALUES (1, 'a'), (2, 'b')) AS t(a, b)`. Those values carry no names
of their own, so the alias names them, and each column's type comes from
what its rows hold. Every bracket after `FROM` used to be read as a derived
`SELECT`, which this is not, so a client asking for one was told the whole
query was unsupported. The three ways of getting the column list wrong
answer with SQL Server's own numbers: 8155 where the alias names no
columns, 8158 where a row is wider than the list, and 8159 where it is
narrower. Either bracketed form can be joined as well as read from, so
`JOIN (VALUES ...)`, `JOIN (SELECT ...)` and the comma form all work; a
`JOIN` used to read a table by name alone and refused both, while both
worked in the `FROM`. Rows of different types settle on one type per
column and the rest convert to it, by precedence and not by which row was
written first: `(VALUES (1), ('2'))` reads as two numbers, and
`(VALUES (1), ('x'))` is msg 245 either way round rather than a column of
text. A moment outranks text the same way, so text that is not a date
beside one is msg 241. What still differs is the type a column is
declared as rather than the rows in it: a whole number beside a decimal
is `numeric(2,1)` on a real server and a float here, and a column of only
NULLs is `int` there. Neither comparison can see that, because the batch
one reads rows and the query one never writes a constructor.

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

A failed query was the one answer still sent in the modern shape whatever
the client asked for. A single error happened to get through the legacy
driver, but the two a batch sends for an unclosed quote did not: it
reported an unknown token. Both errors now arrive the way a real server
sends them to that driver, and the next query on the connection answers.

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
- MARS is answered "off", and that answer is honest. A client that asked for
  multiple active result sets connects and runs; its second open reader is
  refused by its own driver, which is what happens against a real server
  with MARS off. Advertising it without the session-multiplex framing that
  goes with it would be worse than declining, because the client would then
  wrap every packet in a header this server does not read.
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
- A client's transaction API does not send SQL. `BeginTransaction`, `Commit`,
  `Rollback` and `Save` arrive as packet type 0x0E carrying the same
  ALL_HEADERS block, then a two-byte request type and a name. The reply is an
  ENVCHANGE holding an eight-byte descriptor the client stamps on every batch
  it sends inside the transaction. A server that answers that packet with
  anything else drops the connection, so a .NET client failed at
  `BeginTransaction()` before it had sent a query at all.
- The password in a login is not encrypted, it is scrambled: every byte has
  its nibbles swapped and is then XORed with 0xA5, a constant the
  specification publishes. Reading one back is the reverse order, XOR and
  then swap, and doing it in the client's order returns plausible-looking
  rubbish rather than failing. What keeps a password private on the wire is
  the tunnel, which covers the login packet even when the connection agreed
  to encrypt nothing else.
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
