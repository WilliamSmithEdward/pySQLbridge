# Changelog

Each release's notes. The Publish workflow takes the section for the
version it releases as the GitHub release's body, so a section is written
here before the version is tagged.

## [2.1.3] - 2026-10-01

Two security fixes: an HTTP source's credential goes only to the servers
its configuration names, and an XML document that declares a DTD is
refused wherever the declaration stands.

```
pip install --upgrade pysqlbridge
```

**A credential stays with its server.** A source sent its `auth` and
`headers` to every URL it fetched, including next links and pages taken
from the API's own answer, so an answer could point the next request at
another host and be handed the token. They now go only to the servers the
source's `url` names, matched on scheme, host and port. A link to any other
server is still followed, without them, and the log says so once per host.
A redirect to another server drops them, and a redirect to anything but
http or https is refused. An API that serves its pages from another host
names it in `"credential_hosts"`, over https only. A configuration that
relied on the old behaviour pages without its credential there and the log
names the setting that restores it.

**Every DTD is refused.** `parse_xml` looked for `<!DOCTYPE` in the first
4096 bytes only, so a DOCTYPE after a long comment, or one spelled in
UTF-16, was parsed and its entities expanded. It is now refused on expat's
own declaration events, wherever it stands and however the document is
encoded.

This release is the first published through the repository's new release
workflow, which carries the security and malware reports and the
distributions' signed build provenance.

## [2.1.2] - 2026-10-01

WHILE loops, temporary tables can be changed and hold their declared
types, and the startup warm reads first pages only again.

```
pip install --upgrade pysqlbridge
```

**WHILE runs.** It ran its body once and reported success, so
`WHILE @i < 3 SET @i += 1` left `@i` at 1. `BREAK` and `CONTINUE` work
from inside blocks, IFs and TRYs, and outside a loop they are msg 135 and
136, settled while compiling. A loop is stopped at its 100,000th turn. (#3)

**UPDATE, DELETE and TRUNCATE of a temporary table work.** They reported
success and changed nothing. They now change the table, set `@@ROWCOUNT`,
and refuse a missing table or column with 208, 4701 and 207. A failed
UPDATE leaves every row as it was. A form with a `FROM`, `TOP` or `OUTPUT`
is refused by name. (#2)

**A temporary table holds its declared types.** A value converts the way
a cast converts it: `'7'` into an int is 7 and `'q'` is msg 245. Text too
long for its column is 2628, and the INSERT keeps none of its rows. A
datetime column is declared and served as one.

**Smaller fixes.**

- `BEGIN DISTRIBUTED TRANSACTION` written as SQL is refused. (#4)
- An unaliased window function has no column name, as on SQL Server. (#8)
- The join ceiling is msg 50000, not 208. (#7)
- The startup warm takes one request per source again, where a ten-page
  source took eleven. (#5)
- A named table replaces a discovered one of the same name. (#6)
- A publish run started by hand publishes nothing. (#9)

`differential.ps1` now compares column headings too. All 1,243 queries
and 175 batches agree with SQL Server 2025.

## [2.1.1] - 2026-10-01

The same code as 2.1.0 under a new version number. Nothing about how the
bridge behaves changed.

```
pip install --upgrade pysqlbridge
```

## [2.1.0] - 2026-09-13

Decimals keep their places as text, five more refusals carry a real
server's number, and both batteries now run over RPC as well.

```
pip install --upgrade pysqlbridge
```

```sql
SELECT CONCAT(1234567.89, '') AS label,      -- '1234567.89', was '1.23457e+006'
       CAST(CAST(12.567 AS money) AS varchar(10)) AS price,   -- '12.57'
       NTILE((SELECT 2)) OVER (ORDER BY id) AS half
FROM people
```

**A decimal is written with the places it carries.** A float has no places
of its own, so 1.50 and 1.5 were one value here and every decimal became
text as a float's six significant digits: `CONCAT(1234567.89, '')` was
`1.23457e+006` and `LEN(1234567.89)` was 12. A number now carries the places
it was written with, so the text is `1234567.89` and the length 10.
`CONCAT(1.50, '')` keeps its trailing nought, a cast to `decimal(5,3)` shows
three places, and a variable declared as one keeps what it was declared
with. Money is the one type whose text is narrower than its value: it holds
four places and shows two, rounded, so `CAST(CAST(12.567 AS money) AS
varchar(30))` is `12.57`. Every source this serves declares its numbers
float, so a report concatenating a price into a label was reading seventeen
digits where a real server shows the two it wrote.

The places are only ever carried, never worked out. Arithmetic over two
decimals leaves a float here, which is the old divergence the README sets
out: closing it needs the declared type of every operand.

**A value where a condition belongs is msg 4145.** T-SQL has no boolean to
read one as, so `IIF(1, 'a', 'b')` is refused where this answered `'a'`; a
real server reads an `IIF` as the `CASE` it stands for. A batch cut short
inside a condition says the same, wherever it stops afterwards, so `WHERE
rank` and `WHERE rank ORDER` are 4145 while `WHERE rank = 1` stays 102 for
the statement being unfinished.

**An expression with nothing in it to take a type from is refused.** A
`CASE` whose every result is the word `NULL` is msg 8133, `IIF` with both
results `NULL` the same, `COALESCE` with every argument one is 4127, and
`NULLIF` whose first argument is one is 4151. All four answered `NULL` here.
A `NULL` that carries a type is not the constant, so a cast, a declared
variable and a column are all fine.

**A window function's argument may be a subquery.** `NTILE((SELECT 2))`,
`LAG(score, (SELECT 1))`, `FIRST_VALUE((SELECT 5))` and `SUM((SELECT 1))
OVER (...)` all answer on a real server and all were refused here, because
the arguments were never lifted the way a select list's are. An aggregate
that reduces the rows is the other way about: `SUM((SELECT 2))`,
`SUM(SUM(score))` and `SUM(score + (SELECT 2))` are msg 130, settled while
compiling, where this answered them.

**Messages name the text they were given.** A real server types `'ab'` as
varchar and `N'ab'` as nvarchar and says which, so `CAST('ab' AS int)` now
says varchar where everything here used to say nvarchar. A cast to
`varchar`, `char` or `text` makes narrow text and the next message about it
says so. Every column this server serves is nvarchar and is named that way.

**A batch of nothing but a DECLARE or a SET runs.** `DECLARE @x int = 'abc'`
on its own is msg 245, `DECLARE @x decimal(3,1) = 12345.6` is 8115 and
`DECLARE @x int = 5/0` is 8134. This answered nothing at all, because a
batch with no statement that reads was passed over. Found by the truncation
sweep, which sends the prefixes of a battery query and had never had one
that ended at a DECLARE before.

**Both batteries now also run the way a client sends a parameterised
query.** A parameterised statement arrives as an RPC call to sp_executesql
rather than as a batch, which is a different path through this server and
the one a client uses for half of what it sends: SSMS sends 50 of its 93
queries that way. `differential.ps1 -AsRpc` and `batches.ps1 -AsRpc` send
the same batteries over it, and the switch checks that an RPC really went
out before it reports agreement.

1,236 queries answer as SQL Server 2025 does, with the same column types and
the same message numbers, and so do 146 whole batches, statement by
statement, over batches and RPC alike. Of 8,211 truncated prefixes, none is
answered here that a real server refuses and none is refused here that it
answers; 73 differ by error number alone, down from 135.

A decimal is still held as a float, so `0.1 + 0.2` is `0.30000000000000004`
here and `0.3` there. The README says what closing that would take.

## [2.0.0] - 2026-09-13

A batch is compiled before it runs, transactions and TRY/CATCH work, and
text an EXEC is given runs in a scope of its own.

```
pip install --upgrade pysqlbridge
```

```sql
BEGIN TRY
    BEGIN TRAN
    DECLARE @n int = (SELECT COUNT(*) FROM people)
    EXEC sp_executesql N'SELECT @n * 2 AS twice', N'@n int', @n = @n
    COMMIT
END TRY
BEGIN CATCH
    IF @@TRANCOUNT > 0 ROLLBACK
    THROW
END CATCH
```

**A batch that will not compile is refused before any of it runs.** A real
server compiles the whole batch first, so a syntax error in the fourth
statement means the first three never ran. This used to run each statement
as it reached it and report the failure as msg 50000, which meant a client
was shown work that a real server would have thrown away, under a number
that says nothing. The pre-pass carries a real server's numbers: 102 for
syntax, 105 for an unclosed quote, 113 for an unclosed comment, 156 for a
keyword where a name belongs, 137 for a variable nothing declared, 1087 for
a table variable, 141 for a SELECT that assigns and reads at once, 173 for a
column declared with no type. What is refused while compiling and what is
refused while running are two different lists, and both were measured.

**Transactions, and the errors a batch survives.** BEGIN TRAN, COMMIT,
ROLLBACK and SAVE TRAN work from T-SQL and from a client API's own
transaction request, which used to close the connection. TRY/CATCH runs, and
`@@ERROR`, `ERROR_NUMBER()`, `ERROR_MESSAGE()`, `ERROR_SEVERITY()` and
`ERROR_STATE()` answer inside it; `ERROR_LINE()` and `ERROR_PROCEDURE()` are
NULL, because nothing here counts lines or runs in a procedure and a number
would be invented. THROW re-raises what a CATCH caught and raises what it is
given; RAISERROR
takes its format arguments, severity and state; SET XACT_ABORT ON ends the
batch where a real server ends it. Which errors end a batch and which it
carries on past is a measured list rather than a guess, and an error's
severity and state now reach the client instead of a constant 1.

**Text an EXEC runs has its own scope.** `EXEC('...')` and `sp_executesql`
used to run their text with the batch's own variables, which answered the
wrong thing quietly: an argument was evaluated with no variables at all, so
`@x = @y` passed NULL, and a value assigned inside leaked back out. The text
now gets a fresh scope holding only its declared parameters, each converted
to the type it was declared with, arguments may be positional or named,
OUTPUT copies back through the outer variable's type, and a table the text
made is dropped when the text ends. Eight message numbers came with it: 8178
for a declared parameter that was not supplied, 8144 for too many arguments,
214 for a statement that is not nvarchar, 119 for a positional argument
after a named one, 8162 for a parameter passed OUTPUT that was not declared
so, 179 for OUTPUT on a constant, 201 for sp_executesql called with nothing
at all, and 266 for text that leaves the transaction count where it did not
find it.

**A variable holds the type it was declared with.** `DECLARE @x int = 5.7`
holds 5, `varchar(3)` cuts what it is given to three characters, and a value
that will not convert is refused with the same number and words a cast gives
it. The declared type is applied on every assignment, not just the first.

**Casts and conversions measured one at a time.** A cast to `nvarchar(max)`
keeps all of what it is given rather than the 30 characters an unsized cast
takes. A cast to decimal or money rounds to its places, half away from
nought, and refuses a whole part that will not fit. A date keeps its day and
drops the rest. A bit is written as 1 or 0, and reads true and false back as
bits. A float written as text is six significant digits, scientific where
they will not reach, with a three-figure exponent: `CAST(1.0e0 / 3 AS
varchar(30))` is `0.333333`, where this used to hand over all seventeen
digits Python holds. A number too long for the text it is cast to gets the
message a real server gives, which is a different one for each of four
cases.

**Columns are declared the way a real server declares them.** Nullability,
precision, radix and scale in `INFORMATION_SCHEMA.COLUMNS` and `sys.columns`,
and a written NULL is an int with no type of its own, so the other branch of
a UNION still decides.

**A login may carry a username and a password.** Windows Authentication
through SSPI still works; a client that sends a username and a password is
now admitted rather than refused.

**Three comparisons instead of one.** `scripts/differential.ps1` answers one
query at a time, `scripts/batches.ps1` runs whole batches statement by
statement, and `scripts/truncations.ps1` sends every battery query cut short
at each word. Today: 1,168 queries identical with the same column types and
the same message numbers, 138 batches identical, and of 7,846 truncated
prefixes none is answered here that a real server refuses and none is
refused here that it answers. 135 differ by error number alone, 86 of them
one case, and the list of what is measured and not yet built is in the
README.

**What changes for a query that used to work.** Text a real server refuses
is now refused here too, before any of the batch runs, so a batch with a bad
statement at the end no longer returns the earlier ones. Error numbers,
severities and states are a real server's rather than msg 50000. A float
written as text is six digits rather than seventeen, and a bit written as
text is 1 rather than True. Nothing about reading a table changed.

A decimal is still held as a float, so `0.1 + 0.2` is `0.30000000000000004`
here and `0.3` there. Why closing that means carrying declared types through
the whole expression tree is written up in the README.

## [1.2.0] - 2026-09-10

Tables drawn on a sheet are served as tables of their own, and a connection
counts its transactions.

```
pip install --upgrade pysqlbridge
```

```json
{
  "tables": [
    { "excel": "data/budget.xlsx" },
    { "excel": "data/budget.xlsx", "table": "Headcount" },
    { "excel": "data/budget.xlsx", "sheet": ["Q1", "Q2"] }
  ]
}
```

**A table on a sheet is a table.** What Excel calls a table, and the object
model a ListObject, is a range somebody drew and named, kept in a part of its
own with its range, its name and its column names. A workbook now serves one
table per sheet and one per table on a sheet, each named after itself. A
table under a title is served where the sheet used to be refused, and two
tables on one tab are two tables: they used to arrive as one, with the second
one's headings as a record and its numbers dragged over to text. `"sheet"`
and `"table"` narrow a workbook to what they name, one name or a list, and
`"table"` takes a list beside an Access database too.

**A tab is left out where the file says its own reading would be wrong.** It
carries more than one table, a table of its own name, a table ending in a
totals row that would arrive as a record called Total, or a table with no
header row. The log says which, and naming the tab serves it anyway.

**One unreadable tab no longer refuses a workbook.** A tab that cannot be
read as one table, for a stray value past its headings or a heading that
cannot be a column name, is left out with the reason in the log, and the rest
of the workbook is served. Name it and the reason comes back as an error. A
cell that cannot be read at all still refuses the file.

**Checked against Excel itself.** Five workbooks Excel saved for other
projects, and a probe that has Excel write every layout, found three of these
before release. The probe is `scripts/workbook_probe.py` and drives Excel
over COM; nothing in the package does.

**@@TRANCOUNT counts.** It answered nought whatever a batch did. BEGIN TRAN,
COMMIT, ROLLBACK and SAVE TRAN now move it per connection, as measured on SQL
Server 2025: savepoints roll back as a stack, only the outermost transaction
is known by name, and each refusal carries SQL Server's number and words.
Nothing is written, so there is still nothing to commit or roll back.

**Three older bugs fixed with it.** An IF saw every @@ variable as null, so
`IF @@ROWCOUNT = 0` never held. `IF @@TRANCOUNT > 0 COMMIT TRAN` was refused
outright. And BEGIN TRAN, a SELECT and COMMIT written a line apiece lost the
SELECT, with nothing to say so.

983 of 984 queries in the differential answer as SQL Server 2025 does, with
the same column types and the same message numbers.

.NET's `SqlConnection.BeginTransaction()` is not supported: it sends a
transaction manager request, and the bridge closes the connection. A
transaction begun in T-SQL works.

## [1.1.0] - 2026-09-09

Point it at a workbook or an Access database, and query the sheets and
tables inside as if they were a database.

```
pip install --upgrade pysqlbridge
```

```json
{
  "tables": [
    { "excel": "data/budget.xlsx" },
    { "access": "data/club.accdb" }
  ]
}
```

Both hold more than one table, so both produce more than one: a workbook
makes a table per sheet and a database one per table and per saved query,
each named after itself. `"sheet"` and `"table"` pick one out.

**Nothing to install.** Not the Access database engine, which is what
usually reads either of these on Windows: a separate download that installs
in one bit width, refuses to load into a process of the other, and is not on
Linux at all. An `.xlsx` is a zip of XML the standard library opens, and an
`.accdb` is read by [pyOpenVBA](https://github.com/WilliamSmithEdward/pyOpenVBA),
which is pure Python with no dependencies of its own. So an Access database
is served on any machine the rest of this runs on, the suite tests it on
every one of them rather than skipping where an engine is missing, and the
single-file executable needs nothing beside it. The file is read once into
memory and never written: no handle is held, no lock file appears, and
somebody can have the same database open in Access while it is being served.

**Dates are a real type now.** A workbook stores one as a number of days,
and the only thing that makes 45306 a date rather than the number 45306 is
the number format its style points at, so the styles are read. Day 60 is
Excel's 29th of February 1900, a day that did not happen, and is refused
rather than served as some other date; a workbook saved by Excel for Mac
before 2011 counts from 1904 and is read against that epoch. An Access
database declares its types, so a text column stays text where every value
in it happens to be digits, which inference alone gets wrong.

**Saved queries are answered where they were written.** An Access query says
`IIf` and `Nz` and joins its strings with `&`, none of which is what a
client sends this, so it is run in Access SQL and its rows arrive already
worked out. Only the ones that read: an update or a delete query is a
statement rather than a table, and running one to find out what it returns
would change the file.

**Eleven wrong answers, found by having a date column at all.** Each was
measured against SQL Server 2025 and added to the differential.
`WHERE hired = '2024-01-15'` answered nothing, and `>` was right only by the
accident of an ISO date sorting like its own spelling. A join between an
integer column and the same numbers written as text answered nothing at all,
silently, because the hash it buckets rows by kept the collation and never
crossed a type. A bare column name in a join was refused as one that does
not exist. `WHERE hired > 1e18` reached a client as an internal error. A
sheet 1,044 columns wide went past the 1,024 this serves. And `@@ROWCOUNT`
answered nought whatever had just happened, which every client reads as
"nothing came back".

**And two things are quicker.** A written `IN` list evaluated every
candidate for every row, so the cost was rows times values rather than rows
plus values: 2,000 values over 3,200 rows took 452 ms and takes 10, and the
gap widens with every row. A comparison against a written date read the same
characters once a row; remembered, 40,000 rows went from 73 ms to 30.

Everything in
[1.0.2](https://github.com/WilliamSmithEdward/pySQLbridge/releases/tag/v1.0.2)
is here unaltered.

2225 tests, on Python 3.10 through 3.14, Linux and Windows. 963 queries
compared against SQL Server 2025 for the same values, the same declared
column types, and the same error numbers where both refuse.

## [1.0.2] - 2026-09-08

A version number that tells the truth about itself.

```
pip install --upgrade pysqlbridge
```

1.0.1 shipped reporting itself as 0.1.0. The number was written in
`__init__.py` as well as in `pyproject.toml`, and only one of the two was
ever kept up, so `pysqlbridge.__version__` still said what it had said
before the first release. It is read from the installed metadata now,
which leaves one place that says what version this is, and that place is
the one the release workflow already checks the release tag against. A
source tree with nothing installed says `0+unknown` rather than guessing
at a number that would then be wrong.

Found by installing the published 1.0.1 from PyPI and checking that the
seven fixes it claims were really in the wheel. They were. The version it
reported was not.

Nothing else changed. Everything in
[1.0.1](https://github.com/WilliamSmithEdward/pySQLbridge/releases/tag/v1.0.1)
is here unaltered.

2032 tests, on Python 3.10 through 3.14, Linux and Windows. 890 queries
compared against SQL Server 2025.

## [1.0.1] - 2026-09-08

A patch release. Every fix here was found by hunting rather than
reported: fuzzing the SQL, the socket and the bound parameters, sweeping
sources and misbehaving HTTP servers, and timing query shapes at two
sizes. Each was measured against SQL Server 2025 before it was changed.

```
pip install --upgrade pysqlbridge
```

**Four crashes that reached a client as an internal error.** A lone
surrogate in a JSON response took down any query that touched the row; it
now goes on the wire as the code unit, which is what SQL Server stores
and returns. A column named in a JOIN's ON condition escaped as an
internal error where the same mistake in a WHERE had always been a
refusal. A response that stops part way through, which is a proxy
truncating, raised out of the read. And text bound to `TOP (@n)` reached
int() with no message written for it.

**Two answers that were quietly wrong.** `TOP (-1)` returned every row
but the last, taken as a slice bound, where a real server says msg 127. A
column that does not exist was reported as msg 208, invalid object name,
which is the number for a missing table and sends whoever reads it
looking for the wrong thing; SQL Server says 207 in the select list, a
WHERE, an ON and an ORDER BY alike.

**A form of INSERT that did not work.** `INSERT INTO #t VALUES (1, 'x')`
came back saying it had produced no rows. Only the form taking a SELECT
worked. Single rows, several at once, named and partial column lists,
computed rows and rows holding parameters all work now, and the two
errors carry the numbers SQL Server gives them.

**Data no longer lost in silence.** A source with two columns that are
one name under this collation, or a column with no name at all, served
them and let only the first be read: refused now, in SQL Server's own
words. A number too big for a bigint kept its digits as text instead of
failing to encode. A file that is not UTF-8 names itself and the reason
rather than raising the decoder's error. A scratch table made twice, or
dropped when it was never made, said nothing where a real server says msg
2714 and msg 3701.

**Three quadratics, now linear.** A running total over a window worked
the whole frame out again for every row: 2.08 seconds over sixteen
thousand rows, 0.02 now. `IN (SELECT ...)` walked the inner column per
outer row: 0.39 seconds over two thousand against two thousand, 0.004
now. A correlated `EXISTS` read the inner table per outer row: 5.2
seconds, 0.004 now.

**Two answers brought in line with SQL Server.** Totals are added one
value at a time rather than with Python's compensated sum, because a real
server does: over 1e16, 1, 1 and -1e16 it answers 0 where sum() answers
2. Spread is worked out from running sums rather than from the mean, for
the same reason.

Also here: `GRANT`, `REVOKE` and `DENY` are refused rather than reported
as having worked; a select written in brackets is answered rather than
silently returning nothing; `OPTION (...)` is passed over; a query run
again is logged again; and a CSV can say what separates it.

2028 tests, on Python 3.10 through 3.14, Linux and Windows. 890 queries
compared against SQL Server 2025.

## [1.0.0] - 2026-09-08

pySQLbridge answers SQL Server's wire protocol convincingly enough that
Excel, Power BI and SSMS connect to a CSV file, a JSON file or an HTTP API
and see a database. Read-only, so only the SELECT surface has to hold up.

```
pip install pysqlbridge
pysqlbridge --config examples/tables.json
```

Point it at the base URL of an API and it crawls it: against pokeapi.co that
is 27 tables, against dummyjson.com 8. Or name sources one at a time.

**What it answers.** Joins, GROUP BY with HAVING, DISTINCT, TOP with PERCENT
and WITH TIES, OFFSET/FETCH, CTEs, derived tables, subqueries including
correlated ones, CROSS and OUTER APPLY, UNION, EXCEPT, INTERSECT, window
functions with frames, STRING_AGG, 86 scalar functions and 10 aggregates.
Windows Authentication through SSPI, TLS tunnelled in TDS packets, and the
catalog views a client reads before it will draw a table list.

**How the semantics were settled.** Not chosen: compared. 882 queries run
against both this and SQL Server 2025 and are checked for the same values,
the same declared column types, and the same error numbers where both
refuse. That is how trailing spaces in a comparison, the sign of a
remainder, what ROUND does with a half, how MAX orders text, and which
direction a window aggregate adds its values in were all settled, each of
them wrong until it was measured.

**What it will not do.** Nothing writes. An INSERT, UPDATE, DELETE, MERGE,
TRUNCATE, DROP, ALTER, GRANT, REVOKE or DENY is refused and says so, because
passing one over would report that it worked. Anything else it cannot answer
is refused by name rather than answered wrongly.

1949 tests, run on Python 3.10 through 3.14 on Linux and Windows.
