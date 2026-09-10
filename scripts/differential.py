"""Write the fixture and the query list for the differential comparison.

The comparison runs the same queries against SQL Server and against this
project, with the same rows in both, and reports where the answers differ.
That is how the semantics here were settled: trailing spaces in a comparison,
the sign of a remainder, what ROUND does with a half, how MAX orders text.
Every one of those was wrong until it was measured.

    python scripts/differential.py
    python -m pysqlbridge --config <the config it names> --port 1371
    pwsh scripts/differential.ps1 -Port 1371

The rows are written twice, once as JSON for this and once as INSERTs for SQL
Server, from one list, so the two sides cannot drift apart. The tables are
temporary and live on the connection the comparison opens, so nothing is left
behind on the server.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).parent
OUT = HERE / "differential"

# The workbook writer the test suite uses, rather than a second one here.
# There is one shape a workbook has and keeping two writers in step with it is
# how they stop being in step.
sys.path.insert(0, str(HERE.parent))
from tests.workbooks import workbook            # noqa: E402

# Rows chosen for their edges rather than their middle: NULL in every column
# that treats it specially, text differing only in case, an empty string, a
# zero, a negative, and a key in one table that matches nothing in the other.
PEOPLE = [
    {"id": 1, "name": "ada", "team": "red", "score": 10.5, "rank": 3},
    {"id": 2, "name": "Grace", "team": "RED", "score": 20.0, "rank": 1},
    {"id": 3, "name": "alan", "team": "blue", "score": None, "rank": 2},
    {"id": 4, "name": "Edsger", "team": None, "score": 40.25, "rank": None},
    {"id": 5, "name": "", "team": "blue", "score": -5.5, "rank": 2},
    {"id": 6, "name": "barbara", "team": "green", "score": 0.0, "rank": 0},
]

TASKS = [
    {"tid": 100, "owner": 1, "state": "open", "hours": 3},
    {"tid": 101, "owner": 1, "state": "done", "hours": None},
    {"tid": 102, "owner": 2, "state": "open", "hours": 7},
    {"tid": 103, "owner": 9, "state": "done", "hours": 2},
]

# Values far enough apart that adding them in one order and the other gives
# different floats: 1e16 has a gap of 2 between it and the next float up, so
# adding 1 to it changes nothing and adding it last changes everything. What
# a window aggregate answers over these says which order it worked in, which
# is not something the rows above can show. The text differs only in case so
# that MIN and MAX have to choose between two values the collation calls
# equal.
WIDE = [
    {"at": 1, "big": 1e16, "word": "a"},
    {"at": 2, "big": 1.0, "word": "A"},
    {"at": 3, "big": 1.0, "word": "b"},
    {"at": 4, "big": -1e16, "word": "B"},
]

# Moments, which only a source with real types can produce: JSON has no date
# and a date in a CSV is text. A workbook has one, and it is the reason this
# table is written as a workbook rather than as JSON like the other three.
# Midnight and a time of day, two dates in one year so a grouping has
# something to group, a NULL, and one far enough back to be outside anything
# a default would land on.
MOMENTS = [
    {"mid": 1, "when": datetime.datetime(2024, 1, 15)},
    {"mid": 2, "when": datetime.datetime(2024, 6, 1, 13, 30)},
    {"mid": 3, "when": datetime.datetime(2023, 12, 31, 23, 59, 59)},
    {"mid": 4, "when": None},
    {"mid": 5, "when": datetime.datetime(1965, 3, 2)},
]

COLUMNS = {
    "people": ("id", "name", "team", "score", "rank"),
    "tasks": ("tid", "owner", "state", "hours"),
    "wide": ("at", "big", "word"),
    "moments": ("mid", "when"),
}

TYPES = {
    "people": "id int, name nvarchar(50), team nvarchar(50), score float, rank int",
    "tasks": "tid int, owner int, state nvarchar(50), hours int",
    "wide": "[at] int, big float, word nvarchar(50)",
    "moments": "mid int, [when] datetime",
}

QUERIES = [
    # --- three-valued logic ----------------------------------------------
    ("null-eq", "SELECT COUNT(*) AS n FROM people WHERE score = NULL"),
    ("null-ne", "SELECT COUNT(*) AS n FROM people WHERE score <> 10.5"),
    ("null-is", "SELECT COUNT(*) AS n FROM people WHERE score IS NULL"),
    ("null-not-in", "SELECT COUNT(*) AS n FROM people WHERE rank NOT IN (1, 2)"),
    ("null-in-list", "SELECT COUNT(*) AS n FROM people WHERE rank IN (1, NULL)"),
    ("null-not-in-list", "SELECT COUNT(*) AS n FROM people WHERE rank NOT IN (1, NULL)"),
    ("null-and", "SELECT COUNT(*) AS n FROM people WHERE score > 1 AND team = 'red'"),
    ("null-or", "SELECT COUNT(*) AS n FROM people WHERE score > 1 OR team = 'red'"),
    ("null-not", "SELECT COUNT(*) AS n FROM people WHERE NOT (score > 1)"),
    ("null-between", "SELECT COUNT(*) AS n FROM people WHERE score BETWEEN 0 AND 20"),
    ("null-like", "SELECT COUNT(*) AS n FROM people WHERE team LIKE 'r%'"),
    ("null-not-like", "SELECT COUNT(*) AS n FROM people WHERE team NOT LIKE 'r%'"),
    ("not-null", "SELECT COUNT(*) AS n FROM people WHERE NOT (team = 'red')"),
    ("double-not", "SELECT COUNT(*) AS n FROM people WHERE NOT NOT (rank = 2)"),

    # --- collation ---------------------------------------------------------
    ("case-eq", "SELECT COUNT(*) AS n FROM people WHERE name = 'ADA'"),
    ("case-like", "SELECT COUNT(*) AS n FROM people WHERE name LIKE 'GR%'"),
    ("case-in", "SELECT COUNT(*) AS n FROM people WHERE team IN ('red')"),
    ("case-distinct",
     "SELECT COUNT(*) AS n FROM (SELECT DISTINCT team FROM people) AS d"),
    ("case-group", "SELECT team, COUNT(*) AS n FROM people GROUP BY team ORDER BY team"),
    ("trailing-space-eq", "SELECT COUNT(*) AS n FROM people WHERE name = 'ada  '"),
    ("trailing-space-lit", "SELECT COUNT(*) AS n FROM people WHERE 'a' = 'a  '"),
    ("trailing-space-like", "SELECT COUNT(*) AS n FROM people WHERE name LIKE 'ada '"),
    ("text-gt", "SELECT COUNT(*) AS n FROM people WHERE name > 'B'"),
    ("text-order-gt", "SELECT name FROM people WHERE name > 'ada' ORDER BY name"),
    ("min-text", "SELECT MIN(name) AS lo, MAX(name) AS hi FROM people"),
    ("order-text", "SELECT name FROM people ORDER BY name"),
    ("group-order-text", "SELECT name FROM people GROUP BY name ORDER BY name"),

    # --- aggregates ---------------------------------------------------------
    ("count-star", "SELECT COUNT(*) AS n FROM people"),
    ("count-col", "SELECT COUNT(score) AS n FROM people"),
    ("count-distinct", "SELECT COUNT(DISTINCT team) AS n FROM people"),
    ("count-expression", "SELECT COUNT(score + 1) AS n FROM people"),
    ("sum-null", "SELECT SUM(score) AS s FROM people"),
    ("sum-int", "SELECT SUM(rank) AS s FROM people"),
    ("sum-expression", "SELECT SUM(rank + 1) AS s FROM people"),
    ("sum-negative", "SELECT SUM(score) AS s FROM people WHERE score < 0"),
    ("avg-int", "SELECT AVG(rank) AS a FROM people"),
    ("avg-float", "SELECT AVG(score) AS a FROM people"),
    ("avg-trunc", "SELECT AVG(rank) AS a FROM people WHERE rank IS NOT NULL"),
    ("min-max", "SELECT MIN(score) AS lo, MAX(score) AS hi FROM people"),
    ("min-max-int", "SELECT MIN(rank) AS lo, MAX(rank) AS hi FROM people"),
    ("agg-all-null", "SELECT SUM(score) AS s FROM people WHERE id = 3"),
    ("agg-empty", "SELECT COUNT(*) AS n, SUM(score) AS s FROM people WHERE id = 999"),

    # --- grouping ------------------------------------------------------------
    ("group-null-key",
     "SELECT team, COUNT(*) AS n FROM people GROUP BY team ORDER BY n DESC, team"),
    ("group-two", "SELECT team, rank, COUNT(*) AS n FROM people "
                  "GROUP BY team, rank ORDER BY team, rank"),
    ("having", "SELECT team, COUNT(*) AS n FROM people GROUP BY team "
               "HAVING COUNT(*) > 1 ORDER BY team"),
    ("having-plain",
     "SELECT team, COUNT(*) AS n FROM people GROUP BY team HAVING team = 'red'"),
    ("distinct-null",
     "SELECT COUNT(*) AS n FROM (SELECT DISTINCT rank FROM people) AS d"),
    ("distinct-multi",
     "SELECT COUNT(*) AS n FROM (SELECT DISTINCT team, rank FROM people) AS d"),

    # --- ordering and paging --------------------------------------------------
    ("order-null-asc", "SELECT id FROM people ORDER BY score ASC, id"),
    ("order-null-desc", "SELECT id FROM people ORDER BY score DESC, id"),
    ("order-desc-null", "SELECT id FROM people ORDER BY team DESC, id"),
    ("order-two-dir", "SELECT id FROM people ORDER BY team ASC, id DESC"),
    ("offset-fetch",
     "SELECT id FROM people ORDER BY id OFFSET 2 ROWS FETCH NEXT 2 ROWS ONLY"),
    ("top", "SELECT TOP 2 id FROM people ORDER BY id DESC"),
    ("top-ties", "SELECT TOP 3 rank FROM people ORDER BY rank"),

    # --- arithmetic and coercion -----------------------------------------------
    ("int-div", "SELECT 7 / 2 AS q"),
    ("int-div-neg", "SELECT -7 / 2 AS q"),
    ("modulo", "SELECT 7 % 3 AS m, -7 % 3 AS n"),
    ("mixed-div", "SELECT 7 / 2.0 AS q"),
    ("float-int-div", "SELECT 7.0 / 2 AS q"),
    ("neg-mod-float", "SELECT -7.5 % 3 AS m"),
    ("div-zero", "SELECT 1 / 0 AS q"),
    ("mod-zero", "SELECT 1 % 0 AS q"),
    ("null-arith", "SELECT 1 + NULL AS n"),
    ("concat-null", "SELECT 'a' + NULL AS c"),
    ("concat-fn-null", "SELECT CONCAT('a', NULL, 'b') AS c"),
    ("concat-number", "SELECT CONCAT(1, 2) AS s"),
    ("num-to-text", "SELECT '1' + 2 AS n"),
    ("text-plus-bad", "SELECT 'a' + 1 AS n"),
    ("in-mixed", "SELECT COUNT(*) AS n FROM people WHERE rank IN (1, '2')"),
    ("int-float-eq", "SELECT COUNT(*) AS n FROM people WHERE score = 20"),
    ("dec-div", "SELECT 1.0 / 3 AS n"),
    ("dec-add", "SELECT 1.1 + 2.2 AS n"),
    ("dec-mul", "SELECT 1.5 * 1.5 AS n"),
    ("dec-sub", "SELECT 2.0 - 0.5 AS n"),
    ("float-literal", "SELECT 1.0E0 / 3 AS n"),

    # --- string functions --------------------------------------------------------
    ("len-trailing", "SELECT LEN('ada  ') AS n"),
    ("len-empty", "SELECT LEN('') AS n"),
    ("len-null", "SELECT LEN(NULL) AS n"),
    ("substring-past", "SELECT SUBSTRING('abc', 2, 99) AS s"),
    ("substring-zero", "SELECT SUBSTRING('abc', 0, 2) AS s"),
    ("substring-neg", "SELECT SUBSTRING('abcdef', -1, 4) AS s"),
    ("left-more", "SELECT LEFT('abc', 99) AS s"),
    ("left-null", "SELECT LEFT(NULL, 2) AS s"),
    ("right-zero", "SELECT RIGHT('abc', 0) AS s"),
    ("charindex-miss", "SELECT CHARINDEX('z', 'abc') AS n"),
    ("charindex-case", "SELECT CHARINDEX('B', 'abc') AS n"),
    ("charindex-start", "SELECT CHARINDEX('a', 'banana', 3) AS n"),
    ("replace-null", "SELECT REPLACE('abc', 'b', NULL) AS s"),
    ("upper-null", "SELECT UPPER(NULL) AS s"),
    ("upper-mixed", "SELECT UPPER('aBc') AS s"),
    ("reverse", "SELECT REVERSE('abc') AS s"),
    ("space", "SELECT '[' + SPACE(3) + ']' AS s"),
    ("trim", "SELECT '[' + TRIM('  a  ') + ']' AS s"),
    ("ltrim-rtrim", "SELECT '[' + LTRIM(RTRIM('  a  ')) + ']' AS s"),
    ("isnull", "SELECT ISNULL(NULL, 'x') AS s"),
    ("coalesce", "SELECT COALESCE(NULL, NULL, 'third') AS s"),
    ("nullif-same", "SELECT NULLIF(1, 1) AS n"),
    ("nullif-diff", "SELECT NULLIF(1, 2) AS n"),
    ("iif", "SELECT IIF(1 = 2, 'y', 'n') AS s"),

    # --- maths ---------------------------------------------------------------------
    ("abs", "SELECT ABS(-3) AS n"),
    ("abs-float", "SELECT ABS(-2.5) AS n"),
    ("round-half", "SELECT ROUND(2.5, 0) AS n, ROUND(3.5, 0) AS m"),
    ("round-neg", "SELECT ROUND(-2.5, 0) AS n"),
    ("round-down", "SELECT ROUND(2.44, 1) AS n"),
    ("round-places", "SELECT ROUND(123.456, -1) AS n"),
    ("floor-neg", "SELECT FLOOR(-2.1) AS n"),
    ("ceiling-neg", "SELECT CEILING(-2.9) AS n"),
    ("sign", "SELECT SIGN(-4) AS a, SIGN(0) AS b, SIGN(9) AS c"),
    ("power", "SELECT POWER(2, 10) AS n"),
    ("power-float", "SELECT POWER(2.0, 0.5) AS n"),
    ("power-float-e", "SELECT POWER(2.0E0, 0.5) AS n"),
    ("sqrt", "SELECT SQRT(16) AS n"),
    ("sqrt-dec", "SELECT SQRT(2.0) AS n"),

    # --- case and cast -------------------------------------------------------------
    ("case-no-else", "SELECT CASE WHEN 1 = 2 THEN 'x' END AS s"),
    ("case-null-operand",
     "SELECT COUNT(*) AS n FROM people "
     "WHERE CASE team WHEN 'red' THEN 1 ELSE 0 END = 1"),
    ("nested-case",
     "SELECT CASE WHEN 1 = 1 THEN CASE WHEN 2 = 2 THEN 'in' END END AS s"),
    ("case-types", "SELECT CASE WHEN 1 = 1 THEN 1 ELSE 'x' END AS s"),
    ("cast-text-int", "SELECT CAST('42' AS int) AS n"),
    ("cast-float-int", "SELECT CAST(2.9 AS int) AS n"),
    ("cast-neg-int", "SELECT CAST(-2.9 AS int) AS n"),
    ("cast-int-text", "SELECT CAST(42 AS nvarchar(10)) + '!' AS s"),
    ("cast-null", "SELECT CAST(NULL AS int) AS n"),
    ("bad-cast", "SELECT CAST('abc' AS int) AS n"),

    # --- LIKE -------------------------------------------------------------------------
    ("like-underscore", "SELECT COUNT(*) AS n FROM people WHERE name LIKE '_da'"),
    ("like-bracket", "SELECT COUNT(*) AS n FROM people WHERE name LIKE '[ab]%'"),
    ("like-not-bracket", "SELECT COUNT(*) AS n FROM people WHERE name LIKE '[^ab]%'"),
    ("like-escape",
     "SELECT COUNT(*) AS n FROM people WHERE name LIKE 'a!%' ESCAPE '!'"),
    ("like-percent-only", "SELECT COUNT(*) AS n FROM people WHERE name LIKE '%'"),
    ("like-empty", "SELECT COUNT(*) AS n FROM people WHERE name LIKE ''"),
    ("empty-vs-null", "SELECT COUNT(*) AS n FROM people WHERE name = ''"),

    # --- joins ---------------------------------------------------------------------------
    ("inner-join",
     "SELECT COUNT(*) AS n FROM people p JOIN tasks t ON t.owner = p.id"),
    ("left-join",
     "SELECT COUNT(*) AS n FROM people p LEFT JOIN tasks t ON t.owner = p.id"),
    ("join-null-key",
     "SELECT COUNT(*) AS n FROM people p JOIN tasks t ON t.hours = p.rank"),
    ("cross-join", "SELECT COUNT(*) AS n FROM people CROSS JOIN tasks"),
    ("self-join",
     "SELECT COUNT(*) AS n FROM people a JOIN people b ON a.rank = b.rank"),
    ("two-joins",
     "SELECT COUNT(*) AS n FROM people p JOIN tasks t ON t.owner = p.id "
     "JOIN people q ON q.id = p.id"),
    ("join-group",
     "SELECT p.team, COUNT(*) AS n FROM people p JOIN tasks t ON t.owner = p.id "
     "GROUP BY p.team ORDER BY p.team"),

    # --- nested queries -------------------------------------------------------------------
    ("in-subquery",
     "SELECT COUNT(*) AS n FROM people WHERE id IN (SELECT owner FROM tasks)"),
    ("not-in-subquery",
     "SELECT COUNT(*) AS n FROM people WHERE id NOT IN (SELECT owner FROM tasks)"),
    ("not-in-subquery-null",
     "SELECT COUNT(*) AS n FROM people WHERE id NOT IN (SELECT hours FROM tasks)"),
    ("exists",
     "SELECT COUNT(*) AS n FROM people p "
     "WHERE EXISTS (SELECT 1 FROM tasks WHERE owner = 1)"),
    ("cte",
     "WITH busy AS (SELECT owner, COUNT(*) AS n FROM tasks GROUP BY owner) "
     "SELECT COUNT(*) AS n FROM busy WHERE n > 1"),

    # --- a subquery that reads the row around it ----------------------------
    ("correlated-count",
     "SELECT p.name, (SELECT COUNT(*) FROM tasks t WHERE t.owner = p.id) AS n "
     "FROM people p ORDER BY p.id"),
    ("correlated-by-table-name",
     "SELECT name, (SELECT COUNT(*) FROM tasks WHERE owner = people.id) AS n "
     "FROM people ORDER BY id"),
    ("correlated-sum-with-no-match",
     "SELECT p.name, (SELECT SUM(hours) FROM tasks t WHERE t.owner = p.id) AS n "
     "FROM people p ORDER BY p.id"),
    ("correlated-exists",
     "SELECT name FROM people p WHERE EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.owner = p.id) ORDER BY p.id"),
    ("correlated-not-exists",
     "SELECT name FROM people p WHERE NOT EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.owner = p.id) ORDER BY p.id"),
    ("correlated-in",
     "SELECT name FROM people p WHERE p.id IN "
     "(SELECT t.owner FROM tasks t WHERE t.hours > p.id) ORDER BY p.id"),
    ("correlated-comparison",
     "SELECT name FROM people p WHERE "
     "(SELECT COUNT(*) FROM tasks t WHERE t.owner = p.id) > 1 ORDER BY p.id"),
    ("correlated-order-by",
     "SELECT p.name FROM people p ORDER BY "
     "(SELECT COUNT(*) FROM tasks t WHERE t.owner = p.id) DESC, p.id"),
    ("correlated-on-text",
     "SELECT p.name, (SELECT COUNT(*) FROM tasks t WHERE t.state = p.team) AS n "
     "FROM people p ORDER BY p.id"),
    ("correlated-twice-in-one-statement",
     "SELECT p.name, (SELECT COUNT(*) FROM tasks t WHERE t.owner = p.id) AS n "
     "FROM people p WHERE EXISTS (SELECT 1 FROM tasks t WHERE t.owner = p.id) "
     "ORDER BY p.id"),
    ("correlated-beside-an-uncorrelated-one",
     "SELECT p.name, (SELECT COUNT(*) FROM tasks) AS every, "
     "(SELECT COUNT(*) FROM tasks t WHERE t.owner = p.id) AS mine "
     "FROM people p ORDER BY p.id"),

    # --- what type a column is declared as ----------------------------------
    # The comparison checks the declared type of every query above as well as
    # its values; these are here because their type is the whole point. Width
    # is not compared: a source without a schema is sized to what it holds.
    ("type-plain-column", "SELECT name FROM people"),
    ("type-integer-column", "SELECT id FROM people"),
    ("type-float-column", "SELECT score FROM people"),
    ("type-upper", "SELECT UPPER(name) AS v FROM people"),
    ("type-left", "SELECT LEFT(name, 2) AS v FROM people"),
    ("type-len", "SELECT LEN(name) AS v FROM people"),
    ("type-charindex", "SELECT CHARINDEX('a', name) AS v FROM people"),
    ("type-int-plus-int", "SELECT id + 1 AS v FROM people"),
    ("type-int-divided", "SELECT id / 2 AS v FROM people"),
    ("type-float-times", "SELECT score * 2 AS v FROM people"),
    ("type-int-plus-float", "SELECT id + score AS v FROM people"),
    ("type-text-plus-text", "SELECT 'x' + name AS v FROM people"),
    ("type-concat", "SELECT CONCAT(name, 'x') AS v FROM people"),
    ("type-cast-to-text", "SELECT CAST(id AS nvarchar(10)) AS v FROM people"),
    ("type-case-of-text",
     "SELECT CASE WHEN id > 1 THEN 'a' ELSE 'b' END AS v FROM people"),
    ("type-case-of-numbers",
     "SELECT CASE WHEN id > 1 THEN 1 ELSE 2 END AS v FROM people"),
    ("type-case-mixing-numbers",
     "SELECT CASE WHEN id > 1 THEN 1 ELSE 2.5 END AS v FROM people"),
    ("type-abs", "SELECT ABS(id) AS v FROM people"),
    ("type-round", "SELECT ROUND(score, 1) AS v FROM people"),
    ("type-floor", "SELECT FLOOR(score) AS v FROM people"),
    ("type-sign", "SELECT SIGN(id) AS v FROM people"),
    ("type-isnull", "SELECT ISNULL(name, 'x') AS v FROM people"),
    ("type-coalesce", "SELECT COALESCE(name, 'x') AS v FROM people"),
    ("type-iif", "SELECT IIF(id > 1, 'a', 'b') AS v FROM people"),
    ("type-left-of-a-number", "SELECT LEFT(12345, 2) AS v"),
    ("type-len-of-a-number", "SELECT LEN(12345) AS v"),
    ("type-replace", "SELECT REPLACE(name, 'a', 'b') AS v FROM people"),
    ("type-reverse", "SELECT REVERSE(name) AS v FROM people"),
    ("type-remainder", "SELECT id % 2 AS v FROM people"),
    ("type-literal-number", "SELECT 1 + 1 AS v"),
    ("type-literal-text", "SELECT 'lit' AS v"),
    ("type-count", "SELECT COUNT(*) AS v FROM people"),
    ("type-sum-of-int", "SELECT SUM(id) AS v FROM people"),
    ("type-sum-of-float", "SELECT SUM(score) AS v FROM people"),
    ("type-avg-of-int", "SELECT AVG(id) AS v FROM people"),
    ("type-min-of-text", "SELECT MIN(name) AS v FROM people"),
    ("type-max-of-float", "SELECT MAX(score) AS v FROM people"),
    ("type-all-null-arithmetic", "SELECT 1 + NULL AS v"),
    ("type-all-null-function", "SELECT LEN(NULL) AS v"),
    ("type-all-null-cast", "SELECT CAST(NULL AS int) AS v"),
    ("type-all-null-cast-to-text", "SELECT CAST(NULL AS nvarchar(10)) AS v"),
    ("type-all-null-nullif", "SELECT NULLIF(1, 1) AS v"),
    ("type-all-null-isnull", "SELECT ISNULL(NULL, 1) AS v"),
    ("type-all-null-case",
     "SELECT CASE WHEN 1 = 1 THEN NULL ELSE 2 END AS v"),
    ("type-all-null-subquery",
     "SELECT (SELECT tid FROM tasks WHERE owner = 999) AS v"),
    ("type-all-null-column", "SELECT rank AS v FROM people WHERE rank IS NULL"),
    ("type-no-rows-at-all", "SELECT score * 2 AS v FROM people WHERE 1 = 0"),
    ("type-no-rows-text", "SELECT UPPER(name) AS v FROM people WHERE 1 = 0"),
    ("type-no-rows-cast", "SELECT CAST(id AS nvarchar(9)) AS v FROM people WHERE 1 = 0"),
    ("type-no-rows-qualified",
     "SELECT p.score + 1 AS v FROM people p WHERE 1 = 0"),

    # --- an aggregate nothing asked to see -----------------------------------
    ("order-by-an-unlisted-aggregate",
     "SELECT team FROM people GROUP BY team ORDER BY MAX(score) DESC, team"),
    ("having-an-unlisted-aggregate",
     "SELECT team FROM people GROUP BY team HAVING MAX(score) > 15 ORDER BY team"),
    ("both-unlisted-at-once",
     "SELECT team FROM people GROUP BY team HAVING COUNT(*) > 1 "
     "ORDER BY SUM(score) DESC, team"),
    ("order-by-an-expression-of-two",
     "SELECT team FROM people GROUP BY team "
     "ORDER BY MAX(score) - MIN(score) DESC, team"),
    ("having-with-no-group-by", "SELECT COUNT(*) AS n FROM people HAVING COUNT(*) > 1"),
    ("having-with-no-group-by-excluding",
     "SELECT COUNT(*) AS n FROM people HAVING MAX(score) > 1000"),
    ("having-with-no-group-by-unlisted",
     "SELECT COUNT(*) AS n FROM people HAVING MAX(score) > 15"),

    # --- what a cast produces, size and all ---------------------------------
    ("cast-truncates-text", "SELECT CAST('abcdef' AS nvarchar(3)) AS s"),
    ("cast-text-that-fits", "SELECT CAST('abc' AS nvarchar(3)) AS s"),
    ("cast-default-width",
     "SELECT CAST('abcdefghijabcdefghijabcdefghijabcdefghij' AS nvarchar) AS s"),
    ("cast-number-to-text", "SELECT CAST(1234567890 AS nvarchar) AS s"),
    ("cast-pads-a-char", "SELECT CAST('ab' AS nchar(5)) + '|' AS s"),
    ("cast-float-to-int", "SELECT CAST(1.7 AS int) AS n"),
    ("cast-negative-to-int", "SELECT CAST(-1.7 AS int) AS n"),
    ("cast-text-to-int", "SELECT CAST('12345' AS int) AS n"),
    ("cast-padded-text-to-int", "SELECT CAST('  12  ' AS int) AS n"),
    ("convert-with-a-size", "SELECT CONVERT(nvarchar(3), 'abcdef') AS s"),
    ("cast-of-a-column", "SELECT CAST(name AS nvarchar(2)) AS s FROM people ORDER BY id"),
    ("floor-keeps-its-type", "SELECT FLOOR(score) AS n FROM people ORDER BY id"),
    ("ceiling-keeps-its-type", "SELECT CEILING(score) AS n FROM people ORDER BY id"),

    # --- several selects combined into one ----------------------------------
    # Written with an ORDER BY throughout: without one the row order of a
    # combined result is not defined by either server, and which spelling of
    # a case-insensitive duplicate survives is not defined either, so the
    # comparisons below count rows or read columns that have no such pair.
    ("union", "SELECT team FROM people UNION SELECT state FROM tasks ORDER BY 1"),
    ("union-all-count",
     "SELECT COUNT(*) AS n FROM (SELECT team FROM people "
     "UNION ALL SELECT state FROM tasks) AS u"),
    ("union-of-ids",
     "SELECT id FROM people UNION SELECT owner FROM tasks ORDER BY id"),
    ("union-two-columns",
     "SELECT id, rank FROM people UNION SELECT owner, hours FROM tasks "
     "ORDER BY 1, 2"),
    ("union-order-by-position",
     "SELECT id, rank FROM people UNION SELECT owner, hours FROM tasks "
     "ORDER BY 2, 1"),
    ("union-three-parts",
     "SELECT id FROM people UNION SELECT owner FROM tasks UNION SELECT 99 "
     "ORDER BY 1"),
    ("union-with-a-literal-part", "SELECT 1 AS n UNION SELECT 2 ORDER BY n"),
    ("union-offset-fetch",
     "SELECT id FROM people UNION SELECT owner FROM tasks ORDER BY id "
     "OFFSET 1 ROWS FETCH NEXT 2 ROWS ONLY"),
    ("except-ids", "SELECT id FROM people EXCEPT SELECT owner FROM tasks ORDER BY id"),
    ("intersect-ids",
     "SELECT id FROM people INTERSECT SELECT owner FROM tasks ORDER BY id"),
    ("except-of-nothing",
     "SELECT id FROM people EXCEPT SELECT id FROM people ORDER BY id"),
    ("union-drops-repeats",
     "SELECT COUNT(*) AS n FROM (SELECT owner FROM tasks "
     "UNION SELECT owner FROM tasks) AS u"),

    # --- a subquery standing where one value belongs ------------------------
    ("scalar-in-select-alone", "SELECT (SELECT COUNT(*) FROM tasks) AS n"),
    ("scalar-in-select-beside-a-column",
     "SELECT name, (SELECT COUNT(*) FROM tasks) AS n FROM people ORDER BY name"),
    ("scalar-in-select-in-an-expression",
     "SELECT (SELECT COUNT(*) FROM tasks) * 2 AS n"),
    ("scalar-two-of-them",
     "SELECT (SELECT COUNT(*) FROM tasks) - (SELECT COUNT(*) FROM people) AS d"),
    ("scalar-beside-an-aggregate",
     "SELECT COUNT(*) AS c, (SELECT COUNT(*) FROM tasks) AS n FROM people"),
    ("scalar-beside-a-group",
     "SELECT team, COUNT(*) AS c, (SELECT COUNT(*) FROM tasks) AS n "
     "FROM people GROUP BY team ORDER BY team"),
    ("scalar-in-order-by",
     "SELECT name FROM people ORDER BY (SELECT COUNT(*) FROM tasks), name"),
    ("scalar-matching-nothing",
     "SELECT (SELECT tid FROM tasks WHERE owner = 99) AS n"),
    ("scalar-in-every-clause",
     "SELECT (SELECT COUNT(*) FROM tasks) AS n FROM people "
     "WHERE id IN (SELECT owner FROM tasks) "
     "ORDER BY (SELECT MIN(tid) FROM tasks), id"),
    ("literal-beside-an-aggregate", "SELECT 1 AS one, COUNT(*) AS c FROM people"),

    # --- conditional aggregation, which is how a report counts things -------
    ("cond-agg-sum",
     "SELECT SUM(CASE WHEN team = 'red' THEN 1 ELSE 0 END) AS reds FROM people"),
    ("cond-agg-count",
     "SELECT COUNT(CASE WHEN team = 'red' THEN 1 END) AS reds FROM people"),
    ("cond-agg-grouped",
     "SELECT team, SUM(CASE WHEN rank > 1 THEN 1 ELSE 0 END) AS high "
     "FROM people GROUP BY team ORDER BY team"),
    ("agg-of-function",
     "SELECT SUM(LEN(name)) AS n FROM people"),
    ("agg-nested-function",
     "SELECT MAX(UPPER(LEFT(name, 2))) AS s FROM people"),
    ("agg-isnull", "SELECT SUM(ISNULL(score, 0)) AS s FROM people"),
    ("agg-two-columns", "SELECT SUM(rank * score) AS s FROM people"),

    # --- ORDER BY beyond a column ------------------------------------------
    ("order-expression", "SELECT id FROM people ORDER BY id * -1"),
    ("order-alias", "SELECT id * -1 AS negated FROM people ORDER BY negated"),
    ("order-function", "SELECT name FROM people ORDER BY LEN(name), name"),
    ("order-case",
     "SELECT id FROM people ORDER BY CASE WHEN team = 'red' THEN 0 ELSE 1 END, id"),
    ("order-not-selected", "SELECT name FROM people ORDER BY id DESC"),
    ("distinct-order",
     "SELECT DISTINCT team FROM people ORDER BY team"),
    ("order-position", "SELECT name, team FROM people ORDER BY 2, 1"),
    ("order-position-desc", "SELECT name, id FROM people ORDER BY 2 DESC"),
    ("order-alias-shadows-a-column",
     "SELECT team AS name FROM people ORDER BY name"),
    ("order-alias-of-a-function",
     "SELECT UPPER(name) AS s FROM people ORDER BY s"),
    ("order-two-directions", "SELECT name, rank FROM people ORDER BY rank DESC, name ASC"),
    ("order-aggregate-written-out",
     "SELECT team, COUNT(*) AS n FROM people GROUP BY team "
     "ORDER BY COUNT(*) DESC, team"),
    ("order-aggregate-alias",
     "SELECT team, COUNT(*) AS n FROM people GROUP BY team ORDER BY n DESC, team"),
    ("order-grouped-position",
     "SELECT team, COUNT(*) AS n FROM people GROUP BY team ORDER BY 1"),
    ("order-top-alias",
     "SELECT TOP 3 UPPER(name) AS s FROM people ORDER BY s DESC"),

    # --- WHERE beyond a comparison -----------------------------------------
    ("where-case",
     "SELECT COUNT(*) AS n FROM people "
     "WHERE CASE WHEN score IS NULL THEN 0 ELSE score END > 5"),
    ("where-function",
     "SELECT COUNT(*) AS n FROM people WHERE LEN(name) > 3"),
    ("where-arith",
     "SELECT COUNT(*) AS n FROM people WHERE rank * 2 > 3"),
    ("where-concat",
     "SELECT COUNT(*) AS n FROM people WHERE name + '!' = 'ada!'"),
    ("where-nested-not",
     "SELECT COUNT(*) AS n FROM people WHERE NOT (rank IN (1, 2))"),
    ("where-not-like-null",
     "SELECT COUNT(*) AS n FROM people WHERE NOT (name LIKE 'a%')"),
    ("where-mixed-bool",
     "SELECT COUNT(*) AS n FROM people "
     "WHERE (rank = 2 OR team = 'red') AND score IS NOT NULL"),

    # --- joins in more shapes ----------------------------------------------
    ("left-join-where-right",
     "SELECT COUNT(*) AS n FROM people p LEFT JOIN tasks t ON t.owner = p.id "
     "WHERE t.state = 'open'"),
    ("left-join-is-null",
     "SELECT COUNT(*) AS n FROM people p LEFT JOIN tasks t ON t.owner = p.id "
     "WHERE t.tid IS NULL"),
    ("join-two-conditions",
     "SELECT COUNT(*) AS n FROM people p JOIN tasks t "
     "ON t.owner = p.id AND t.state = 'open'"),
    ("join-inequality",
     "SELECT COUNT(*) AS n FROM people p JOIN tasks t ON t.hours > p.rank"),
    ("join-order",
     "SELECT p.name, t.state FROM people p JOIN tasks t ON t.owner = p.id "
     "ORDER BY p.name, t.state"),
    ("join-distinct",
     "SELECT COUNT(*) AS n FROM (SELECT DISTINCT p.team FROM people p "
     "JOIN tasks t ON t.owner = p.id) AS d"),
    ("join-having",
     "SELECT p.id, COUNT(*) AS n FROM people p JOIN tasks t ON t.owner = p.id "
     "GROUP BY p.id HAVING COUNT(*) > 1 ORDER BY p.id"),

    # --- nested queries in more shapes --------------------------------------
    ("scalar-in-select",
     "SELECT (SELECT COUNT(*) FROM tasks) AS n"),
    ("subquery-with-where",
     "SELECT COUNT(*) AS n FROM people "
     "WHERE id IN (SELECT owner FROM tasks WHERE state = 'open')"),
    ("subquery-aggregate",
     "SELECT COUNT(*) AS n FROM people WHERE rank >= (SELECT AVG(rank) FROM people)"),
    ("derived-grouped",
     "SELECT COUNT(*) AS n FROM (SELECT team, COUNT(*) AS c FROM people "
     "GROUP BY team) AS g WHERE c > 1"),
    ("derived-join",
     "SELECT COUNT(*) AS n FROM (SELECT id FROM people WHERE rank IS NOT NULL) AS a "
     "JOIN tasks t ON t.owner = a.id"),
    ("cte-twice",
     "WITH a AS (SELECT id FROM people WHERE rank = 2) "
     "SELECT COUNT(*) AS n FROM a JOIN a AS b ON b.id = a.id"),
    ("cte-then-group",
     "WITH a AS (SELECT team, rank FROM people WHERE team IS NOT NULL) "
     "SELECT team, COUNT(*) AS n FROM a GROUP BY team ORDER BY team"),
    ("exists-correlated-ish",
     "SELECT COUNT(*) AS n FROM people WHERE EXISTS "
     "(SELECT 1 FROM tasks WHERE hours IS NULL)"),

    # --- empty results through every path -----------------------------------
    ("empty-where", "SELECT id FROM people WHERE id = 999"),
    ("empty-group",
     "SELECT team, COUNT(*) AS n FROM people WHERE id = 999 GROUP BY team"),
    ("empty-join",
     "SELECT COUNT(*) AS n FROM people p JOIN tasks t ON t.owner = 999"),
    ("empty-distinct", "SELECT DISTINCT team FROM people WHERE id = 999"),
    ("empty-order", "SELECT id FROM people WHERE id = 999 ORDER BY id"),
    ("empty-top", "SELECT TOP 5 id FROM people WHERE id = 999"),

    # --- more string and null edges ------------------------------------------
    ("concat-mixed", "SELECT CONCAT('a', 1, NULL, 2.5) AS s"),
    ("nested-isnull", "SELECT ISNULL(NULLIF('a', 'a'), 'b') AS s"),
    ("coalesce-numbers", "SELECT COALESCE(NULL, 1, 2) AS n"),
    ("case-in-concat",
     "SELECT 'x' + CASE WHEN 1 = 1 THEN 'y' ELSE 'z' END AS s"),
    ("upper-of-concat", "SELECT UPPER('a' + 'b') AS s"),
    ("len-of-number", "SELECT LEN(12345) AS n"),
    ("substring-of-number", "SELECT SUBSTRING(12345, 2, 2) AS s"),
    ("charindex-empty", "SELECT CHARINDEX('', 'abc') AS n"),
    ("replace-empty", "SELECT REPLACE('abc', '', 'x') AS s"),
    ("iif-null", "SELECT IIF(1 = 1, NULL, 'x') AS s"),
    ("count-distinct-note",
     "SELECT COUNT(*) AS n FROM (SELECT DISTINCT name FROM people) AS d"),

    # --- how far into a statement its own variables reach --------------------
    ("variable-plain", "DECLARE @p int = 5 SELECT @p AS v"),
    ("variable-in-a-subquery", "DECLARE @p int = 5 SELECT (SELECT @p) AS v"),
    ("variable-in-a-cte",
     "DECLARE @p int = 5 ;WITH one AS (SELECT @p AS v) SELECT v FROM one"),
    ("variable-in-a-derived-table",
     "DECLARE @p int = 5 SELECT v FROM (SELECT @p AS v) AS d"),
    ("variable-in-a-where",
     "DECLARE @p int = 2 SELECT id FROM people WHERE id <= @p ORDER BY id"),
    ("variable-in-a-subquery-where",
     "DECLARE @p int = 2 SELECT COUNT(*) AS n FROM people "
     "WHERE id IN (SELECT id FROM people WHERE id <= @p)"),
    ("variable-set-by-a-select", "DECLARE @p int SELECT @p = 6 SELECT @p AS v"),
    ("variable-from-a-table",
     "DECLARE @p int SELECT @p = COUNT(*) FROM people SELECT @p AS v"),
    ("variable-from-the-last-row",
     "DECLARE @p int SELECT @p = id FROM people ORDER BY id SELECT @p AS v"),
    ("variable-from-no-rows-keeps-what-it-had",
     "DECLARE @p int = 3 SELECT @p = id FROM people WHERE id = 999 "
     "SELECT @p AS v"),
    ("variable-from-a-table-with-a-where",
     "DECLARE @p nvarchar(50) SELECT @p = name FROM people WHERE id = 1 "
     "SELECT @p AS v"),
    ("variable-set-from-a-subquery",
     "DECLARE @p int SET @p = (SELECT COUNT(*) FROM people) SELECT @p AS v"),
    ("variable-set-from-a-top-one",
     "DECLARE @p int SET @p = (SELECT TOP 1 id FROM people ORDER BY id) "
     "SELECT @p AS v"),
    ("variable-set-from-a-subquery-with-no-rows",
     "DECLARE @p int = 3 SET @p = (SELECT id FROM people WHERE id = 999) "
     "SELECT @p AS v"),
    ("declared-int-holding-nothing", "DECLARE @p int SELECT @p AS v"),
    ("declared-text-holding-nothing", "DECLARE @p nvarchar(50) SELECT @p AS v"),
    ("declared-float-holding-nothing", "DECLARE @p float SELECT @p AS v"),
    ("declared-bit-holding-nothing", "DECLARE @p bit SELECT @p AS v"),
    ("declared-int-holding-a-number", "DECLARE @p int = 7 SELECT @p AS v"),
    ("declared-int-in-an-expression",
     "DECLARE @p int = 7 SELECT @p * 2 AS v"),

    # --- a branch inside a branch --------------------------------------------
    ("if-inside-if-outer-holds",
     "IF 1 = 1 BEGIN IF 1 = 2 BEGIN SELECT 9 AS v END "
     "ELSE BEGIN SELECT 5 AS v END END ELSE SELECT 0 AS v"),
    ("if-inside-if-outer-does-not",
     "IF 1 = 2 BEGIN IF 1 = 1 BEGIN SELECT 9 AS v END "
     "ELSE BEGIN SELECT 5 AS v END END ELSE SELECT 0 AS v"),
    ("if-with-a-case-inside-it",
     "IF 1 = 1 BEGIN SELECT CASE WHEN 1 = 1 THEN 3 ELSE 4 END AS v END "
     "ELSE SELECT 0 AS v"),

    # --- a byte holds 0 to 255 ------------------------------------------------
    ("tinyint-above-a-signed-byte", "SELECT CAST(200 AS tinyint) AS v"),
    ("tinyint-at-the-top", "SELECT CAST(255 AS tinyint) AS v"),
    ("tinyint-at-the-bottom", "SELECT CAST(0 AS tinyint) AS v"),

    # --- a moment, and what a number counts into one ------------------------
    ("datetime-from-zero", "SELECT CAST(0 AS datetime) AS v"),
    ("datetime-from-one", "SELECT CAST(1 AS datetime) AS v"),
    ("datetime-from-text", "SELECT CAST('2026-09-07' AS datetime) AS v"),
    ("datetime-from-nothing", "SELECT CAST(NULL AS datetime) AS v"),
    ("datetime-through-isnull",
     "SELECT CAST(ISNULL(NULL, 0) AS datetime) AS v"),
    # --- what a column is called when a query qualifies it -------------------
    ("heading-of-a-qualified-column",
     "SELECT p.name FROM people AS p ORDER BY p.name"),
    ("heading-of-a-qualified-column-in-a-join",
     "SELECT p.name, t.state FROM people AS p "
     "JOIN tasks AS t ON t.owner = p.id ORDER BY p.name, t.state"),
    ("heading-of-a-column-qualified-by-the-table",
     "SELECT people.name FROM people ORDER BY people.name"),
    ("heading-of-an-alias-over-a-qualified-column",
     "SELECT p.name AS who FROM people AS p ORDER BY who"),
    ("heading-of-a-star-beside-a-qualified-column",
     "SELECT p.*, p.name FROM people AS p ORDER BY p.name"),

    # --- what the catalog says about a column --------------------------------
    # The fixture here is temporary tables, which INFORMATION_SCHEMA on the
    # real server does not describe, so what the catalog says about a column
    # is checked in the tests rather than here. What can be compared is that
    # a view of things neither server has answers with no rows rather than
    # with an error.
    # --- a clause that runs up against a set operator ------------------------
    ("where-then-union",
     "SELECT name FROM people WHERE id = 1 "
     "UNION SELECT name FROM people WHERE id = 2 ORDER BY name"),
    ("where-then-union-all",
     "SELECT id FROM people WHERE id < 3 "
     "UNION ALL SELECT id FROM people WHERE id < 2 ORDER BY id"),
    ("where-then-except",
     "SELECT id FROM people WHERE id > 0 "
     "EXCEPT SELECT id FROM people WHERE id > 2 ORDER BY id"),
    ("where-then-intersect",
     "SELECT id FROM people WHERE id > 0 "
     "INTERSECT SELECT id FROM people WHERE id < 3 ORDER BY id"),
    ("group-by-then-union",
     "SELECT team FROM people WHERE id > 0 GROUP BY team "
     "UNION SELECT team FROM people GROUP BY team ORDER BY team"),
    ("having-then-union",
     "SELECT team FROM people GROUP BY team HAVING COUNT(*) > 0 "
     "UNION SELECT team FROM people GROUP BY team ORDER BY team"),
    ("union-ordered-by-a-qualified-column",
     "SELECT p.id FROM people AS p WHERE p.id < 3 "
     "UNION SELECT q.id FROM people AS q WHERE q.id > 1 "
     "ORDER BY p.id"),
    ("union-ordered-by-the-plain-name",
     "SELECT p.id FROM people AS p WHERE p.id < 3 "
     "UNION SELECT q.id FROM people AS q WHERE q.id > 1 ORDER BY id"),
    ("union-ordered-by-a-qualifier-only-one-side-has",
     "SELECT p.id FROM people AS p WHERE p.id < 3 "
     "UNION SELECT t.tid FROM tasks AS t WHERE t.tid > 900 ORDER BY p.id"),
    ("a-union-inside-a-string-is-not-one",
     "SELECT name FROM people WHERE name = 'a union b'"),

    ("routines-is-empty-not-missing",
     "SELECT COUNT(*) AS n FROM INFORMATION_SCHEMA.ROUTINES "
     "WHERE ROUTINE_SCHEMA = 'nothing_here'"),
    ("constraints-are-empty-not-missing",
     "SELECT COUNT(*) AS n FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS "
     "WHERE CONSTRAINT_SCHEMA = 'nothing_here'"),

    ("datetime-round-trip",
     "SELECT CAST(CAST('2026-09-07' AS datetime) AS nvarchar(30)) AS v"),

    # --- text read as a number ----------------------------------------------
    # One rule, used by CAST and by the conversion a union does to bring its
    # branches to one type. Two of these are counter-intuitive enough to be
    # worth naming: an empty string is zero, and a whole number written with
    # a decimal point is not an integer.
    ("cast-empty-text-to-int", "SELECT CAST('' AS int) AS v"),
    ("cast-empty-text-to-bigint", "SELECT CAST('' AS bigint) AS v"),
    ("cast-empty-text-to-float", "SELECT CAST('' AS float) AS v"),
    ("cast-spaced-text-to-int", "SELECT CAST(' 2 ' AS int) AS v"),
    ("cast-decimal-text-to-int", "SELECT CAST('2.0' AS int) AS v"),
    ("cast-decimal-text-to-bigint", "SELECT CAST('2.0' AS bigint) AS v"),
    ("cast-decimal-text-to-float", "SELECT CAST('2.5' AS float) AS v"),
    ("cast-signed-text-to-int", "SELECT CAST('-2' AS int) AS v"),
    ("cast-float-to-int-truncates", "SELECT CAST(CAST(2.7 AS float) AS int) AS v"),
    ("cast-negative-float-to-int-truncates",
     "SELECT CAST(CAST(-2.7 AS float) AS int) AS v"),
    ("cast-text-to-bit", "SELECT CAST('2' AS bit) AS v"),
    ("cast-zero-text-to-bit", "SELECT CAST('0' AS bit) AS v"),
    ("cast-word-to-int", "SELECT CAST('ada' AS int) AS v"),

    # --- a union whose branches are not the same type ------------------------
    # The column gets one type, chosen across every branch by data type
    # precedence, and every branch's values are converted to it. A value that
    # will not convert is an error, and it is the whole statement's error
    # rather than one branch's.
    ("union-int-and-float",
     "SELECT id FROM people UNION ALL SELECT score FROM people ORDER BY 1"),
    ("union-float-and-int",
     "SELECT score FROM people UNION ALL SELECT id FROM people ORDER BY 1"),
    ("union-int-and-numeric-text",
     "SELECT id FROM people UNION ALL SELECT '7' ORDER BY 1"),
    ("union-numeric-text-and-int",
     "SELECT '7' AS v UNION ALL SELECT id FROM people ORDER BY 1"),
    ("union-int-and-a-word", "SELECT id FROM people UNION ALL SELECT name FROM people"),
    ("union-a-word-and-int", "SELECT name FROM people UNION ALL SELECT id FROM people"),
    ("union-keeps-the-type-of-a-branch-with-no-rows",
     "SELECT id FROM people WHERE 1 = 0 UNION ALL SELECT name FROM people"),
    ("union-of-a-branch-that-is-only-null",
     "SELECT NULL AS v FROM people UNION ALL SELECT name FROM people ORDER BY 1"),
    ("union-converts-before-it-drops-repeats",
     "SELECT id FROM people UNION SELECT '1' ORDER BY 1"),
    ("union-three-branches-take-the-highest",
     "SELECT id FROM people UNION ALL SELECT '7' "
     "UNION ALL SELECT score FROM people ORDER BY 1"),
    ("union-across-types-counted",
     "SELECT COUNT(*) AS n FROM "
     "(SELECT id FROM people UNION SELECT score FROM people) AS u"),
    ("except-across-types", "SELECT id FROM people EXCEPT SELECT '1' ORDER BY 1"),
    ("intersect-across-types", "SELECT id FROM people INTERSECT SELECT '1' ORDER BY 1"),
    ("union-int-and-a-date",
     "SELECT id FROM people UNION ALL SELECT CAST('2020-01-02' AS datetime) ORDER BY 1"),

    # --- dates -------------------------------------------------------------
    # A moment on a Tuesday in the third quarter, in week 37, so that every
    # part of it is a different number and a part read as the wrong one shows.
    ("year", "SELECT YEAR(CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),
    ("month", "SELECT MONTH(CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),
    ("day", "SELECT DAY(CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),
    ("datepart-year", "SELECT DATEPART(year, CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),
    ("datepart-quarter", "SELECT DATEPART(quarter, CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),
    ("datepart-month", "SELECT DATEPART(month, CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),
    ("datepart-dayofyear", "SELECT DATEPART(dayofyear, CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),
    ("datepart-day", "SELECT DATEPART(day, CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),
    ("datepart-week", "SELECT DATEPART(week, CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),
    ("datepart-weekday", "SELECT DATEPART(weekday, CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),
    ("datepart-hour", "SELECT DATEPART(hour, CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),
    ("datepart-minute", "SELECT DATEPART(minute, CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),
    ("datepart-second", "SELECT DATEPART(second, CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),
    ("datepart-millisecond", "SELECT DATEPART(millisecond, CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),
    # The abbreviations, of which y and d are the pair worth watching.
    ("datepart-abbreviated-one",
     "SELECT DATEPART(yy, CAST('2026-09-08T14:35:47.123' AS datetime)) AS a, DATEPART(qq, CAST('2026-09-08T14:35:47.123' AS datetime)) AS b, "
     "DATEPART(mm, CAST('2026-09-08T14:35:47.123' AS datetime)) AS c, DATEPART(dy, CAST('2026-09-08T14:35:47.123' AS datetime)) AS d"),
    ("datepart-abbreviated-two",
     "SELECT DATEPART(dd, CAST('2026-09-08T14:35:47.123' AS datetime)) AS a, DATEPART(wk, CAST('2026-09-08T14:35:47.123' AS datetime)) AS b, "
     "DATEPART(dw, CAST('2026-09-08T14:35:47.123' AS datetime)) AS c, DATEPART(hh, CAST('2026-09-08T14:35:47.123' AS datetime)) AS d"),
    ("datepart-abbreviated-three",
     "SELECT DATEPART(mi, CAST('2026-09-08T14:35:47.123' AS datetime)) AS a, DATEPART(ss, CAST('2026-09-08T14:35:47.123' AS datetime)) AS b, "
     "DATEPART(ms, CAST('2026-09-08T14:35:47.123' AS datetime)) AS c, DATEPART(y, CAST('2026-09-08T14:35:47.123' AS datetime)) AS d, DATEPART(d, CAST('2026-09-08T14:35:47.123' AS datetime)) AS e"),
    ("datepart-of-a-year-that-opens-on-a-thursday",
     "SELECT DATEPART(week, CAST('2026-01-01' AS datetime)) AS a, "
     "DATEPART(week, CAST('2026-01-04' AS datetime)) AS b, "
     "DATEPART(week, CAST('2026-12-31' AS datetime)) AS c"),
    ("datepart-weekday-counts-sunday-as-one",
     "SELECT DATEPART(weekday, CAST('2026-09-06' AS datetime)) AS a, "
     "DATEPART(weekday, CAST('2026-09-07' AS datetime)) AS b, "
     "DATEPART(weekday, CAST('2026-01-01' AS datetime)) AS c"),

    ("datename-month", "SELECT DATENAME(month, CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),
    ("datename-weekday", "SELECT DATENAME(weekday, CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),
    ("datename-of-a-number-is-that-number",
     "SELECT DATENAME(year, CAST('2026-09-08T14:35:47.123' AS datetime)) AS a, DATENAME(day, CAST('2026-09-08T14:35:47.123' AS datetime)) AS b, "
     "DATENAME(quarter, CAST('2026-09-08T14:35:47.123' AS datetime)) AS c, DATENAME(hour, CAST('2026-09-08T14:35:47.123' AS datetime)) AS d"),

    ("dateadd-day", "SELECT DATEADD(day, 1, CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),
    ("dateadd-negative-day", "SELECT DATEADD(day, -1, CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),
    ("dateadd-week", "SELECT DATEADD(week, 2, CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),
    ("dateadd-month", "SELECT DATEADD(month, 1, CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),
    ("dateadd-quarter", "SELECT DATEADD(quarter, 1, CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),
    ("dateadd-year", "SELECT DATEADD(year, -1, CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),
    ("dateadd-hour", "SELECT DATEADD(hour, 10, CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),
    ("dateadd-minute", "SELECT DATEADD(minute, -90, CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),
    ("dateadd-second", "SELECT DATEADD(second, 30, CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),
    ("dateadd-truncates-what-it-is-given",
     "SELECT DATEADD(day, 1.9, CAST('2026-09-08T14:35:47.123' AS datetime)) AS a, DATEADD(day, -1.9, CAST('2026-09-08T14:35:47.123' AS datetime)) AS b"),
    # A month is held back to the end of a short one rather than spilling.
    ("dateadd-into-a-shorter-month",
     "SELECT DATEADD(month, 1, CAST('2026-01-31' AS datetime)) AS a, "
     "DATEADD(month, -1, CAST('2026-03-31' AS datetime)) AS b, "
     "DATEADD(month, 1, CAST('2026-08-31' AS datetime)) AS c"),
    ("dateadd-out-of-a-leap-year",
     "SELECT DATEADD(year, 1, CAST('2024-02-29' AS datetime)) AS a, "
     "DATEADD(month, 12, CAST('2024-02-29' AS datetime)) AS b"),
    ("dateadd-past-what-a-datetime-holds",
     "SELECT DATEADD(day, -1, CAST('1753-01-01' AS datetime)) AS v"),

    # Boundaries crossed, not elapsed time.
    ("datediff-a-minute-either-side-of-midnight",
     "SELECT DATEDIFF(day, CAST('2026-01-01 23:59' AS datetime), "
     "CAST('2026-01-02 00:01' AS datetime)) AS v"),
    ("datediff-a-whole-day-inside-one-date",
     "SELECT DATEDIFF(day, CAST('2026-01-01 00:00' AS datetime), "
     "CAST('2026-01-01 23:59' AS datetime)) AS v"),
    ("datediff-year", "SELECT DATEDIFF(year, CAST('2026-12-31' AS datetime), "
     "CAST('2027-01-01' AS datetime)) AS v"),
    ("datediff-month", "SELECT DATEDIFF(month, CAST('2026-01-31' AS datetime), "
     "CAST('2026-02-01' AS datetime)) AS v"),
    ("datediff-quarter", "SELECT DATEDIFF(quarter, CAST('2026-03-31' AS datetime), "
     "CAST('2026-04-01' AS datetime)) AS v"),
    ("datediff-week-starts-on-sunday",
     "SELECT DATEDIFF(week, CAST('2026-01-03' AS datetime), "
     "CAST('2026-01-04' AS datetime)) AS v"),
    ("datediff-hour", "SELECT DATEDIFF(hour, CAST('2026-01-01 00:59' AS datetime), "
     "CAST('2026-01-01 01:00' AS datetime)) AS v"),
    ("datediff-second", "SELECT DATEDIFF(second, CAST('2026-01-01' AS datetime), "
     "CAST('2026-01-02' AS datetime)) AS v"),
    ("datediff-backwards", "SELECT DATEDIFF(day, CAST('2026-01-02' AS datetime), "
     "CAST('2026-01-01' AS datetime)) AS v"),
    ("datediff-day-of-the-year-is-a-day",
     "SELECT DATEDIFF(dayofyear, CAST('2026-01-01' AS datetime), "
     "CAST('2026-12-31' AS datetime)) AS a, "
     "DATEDIFF(weekday, CAST('2026-01-01' AS datetime), "
     "CAST('2026-12-31' AS datetime)) AS b"),
    ("datediff-that-does-not-fit-an-int",
     "SELECT DATEDIFF(second, CAST('1900-01-01' AS datetime), "
     "CAST('2026-01-01' AS datetime)) AS v"),

    ("eomonth", "SELECT EOMONTH(CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),
    ("eomonth-months-on", "SELECT EOMONTH(CAST('2026-09-08T14:35:47.123' AS datetime), 1) AS a, EOMONTH(CAST('2026-09-08T14:35:47.123' AS datetime), -1) AS b"),
    ("eomonth-of-a-leap-february",
     "SELECT EOMONTH(CAST('2024-02-10' AS datetime)) AS v"),

    # Text that spells a date, and a number counted from 1900.
    ("dates-out-of-text",
     "SELECT YEAR('2026-09-08') AS a, "
     "DATEDIFF(day, '2026-09-08', '2026-09-10') AS b"),
    ("year-of-a-number", "SELECT YEAR(0) AS v"),

    # NULL, which is every argument but the count DATEADD moves by.
    ("dates-of-null",
     "SELECT YEAR(NULL) AS a, DATEADD(day, 1, NULL) AS b, "
     "DATEDIFF(day, NULL, CAST('2026-09-08T14:35:47.123' AS datetime)) AS c, EOMONTH(NULL) AS d, "
     "DATEPART(year, NULL) AS e, DATENAME(month, NULL) AS f"),
    ("dateadd-by-null", "SELECT DATEADD(day, NULL, CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),

    # Now, compared only against itself, because it is a different moment on
    # each server. What is worth comparing is that it holds still.
    ("now-is-one-moment",
     "SELECT DATEDIFF(second, GETDATE(), CURRENT_TIMESTAMP) AS a, "
     "DATEDIFF(second, GETDATE(), SYSDATETIME()) AS b, "
     "DATEDIFF(day, GETDATE(), GETDATE()) AS c"),
    ("now-is-the-same-in-every-row",
     "SELECT COUNT(*) AS n FROM "
     "(SELECT DISTINCT GETDATE() AS moment FROM people) AS u"),
    ("utc-is-the-same-clock",
     "SELECT DATEDIFF(day, GETDATE(), GETUTCDATE()) AS a, "
     "DATEDIFF(second, GETUTCDATE(), SYSUTCDATETIME()) AS b"),
    ("a-date-filter-a-person-would-write",
     "SELECT COUNT(*) AS n FROM people "
     "WHERE CAST('2026-09-08' AS datetime) > DATEADD(day, -7, GETDATE())"),

    ("datepart-of-something-that-is-not-a-part",
     "SELECT DATEPART(fortnight, CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),

    # --- grouping on what an expression works out ---------------------------
    # What a report actually groups by: a name folded to one case, a first
    # letter, the year of a date, a number bucketed.
    ("group-by-a-folded-column",
     "SELECT UPPER(team) AS t, COUNT(*) AS n FROM people "
     "GROUP BY UPPER(team) ORDER BY t"),
    ("group-by-a-first-letter",
     "SELECT LEFT(name, 1) AS c, COUNT(*) AS n FROM people "
     "GROUP BY LEFT(name, 1) ORDER BY c"),
    ("group-by-arithmetic",
     "SELECT rank + 1 AS r, COUNT(*) AS n FROM people "
     "GROUP BY rank + 1 ORDER BY r"),
    ("group-by-a-year",
     "SELECT YEAR(DATEADD(day, rank, CAST('2026-01-01' AS datetime))) AS y, "
     "COUNT(*) AS n FROM people "
     "GROUP BY YEAR(DATEADD(day, rank, CAST('2026-01-01' AS datetime))) "
     "ORDER BY y"),
    ("group-by-a-month-of-a-date",
     "SELECT DATENAME(month, DATEADD(month, rank, "
     "CAST('2026-01-15' AS datetime))) AS m, COUNT(*) AS n FROM people "
     "GROUP BY DATENAME(month, DATEADD(month, rank, "
     "CAST('2026-01-15' AS datetime))) ORDER BY m"),
    ("group-by-a-case",
     "SELECT CASE WHEN score > 10 THEN 'high' ELSE 'low' END AS band, "
     "COUNT(*) AS n FROM people "
     "GROUP BY CASE WHEN score > 10 THEN 'high' ELSE 'low' END ORDER BY band"),
    ("group-by-a-cast",
     "SELECT CAST(score AS int) AS s, COUNT(*) AS n FROM people "
     "GROUP BY CAST(score AS int) ORDER BY s"),
    ("group-by-an-expression-and-a-column",
     "SELECT UPPER(team) AS t, rank, COUNT(*) AS n FROM people "
     "GROUP BY UPPER(team), rank ORDER BY t, rank"),
    ("group-by-an-expression-spelled-differently",
     "SELECT UPPER(team) AS t, COUNT(*) AS n FROM people "
     "GROUP BY UPPER( team ) ORDER BY t"),
    ("group-by-an-expression-with-a-having",
     "SELECT LEFT(name, 1) AS c, COUNT(*) AS n FROM people "
     "GROUP BY LEFT(name, 1) HAVING COUNT(*) > 1 ORDER BY c"),
    ("group-by-an-expression-ordered-by-it",
     "SELECT UPPER(team) AS t FROM people GROUP BY UPPER(team) "
     "ORDER BY UPPER(team)"),
    ("group-by-an-expression-with-a-sum",
     "SELECT UPPER(team) AS t, SUM(score) AS s, MAX(rank) AS r FROM people "
     "GROUP BY UPPER(team) ORDER BY t"),
    ("group-by-an-expression-over-a-join",
     "SELECT UPPER(p.team) AS t, COUNT(*) AS n FROM people AS p "
     "JOIN tasks AS k ON k.owner = p.id GROUP BY UPPER(p.team) ORDER BY t"),
    ("group-by-an-expression-nothing-selects",
     "SELECT COUNT(*) AS groups FROM "
     "(SELECT COUNT(*) AS n FROM people GROUP BY LEFT(name, 1)) AS g"),
    ("dateadd-by-a-column-that-holds-null",
     "SELECT DATEADD(day, rank, CAST('2026-01-01' AS datetime)) AS v "
     "FROM people ORDER BY id"),
    ("dateadd-by-a-null-that-has-a-type",
     "SELECT DATEADD(day, CAST(NULL AS int), "
     "CAST('2026-01-01' AS datetime)) AS v"),
    ("group-by-a-column-the-select-list-computes-over",
     "SELECT team, COUNT(*) AS n FROM people GROUP BY team ORDER BY team"),
    ("a-column-not-grouped-is-still-refused",
     "SELECT name, COUNT(*) AS n FROM people GROUP BY team"),

    # --- converting without failing -----------------------------------------
    # A source read off a CSV or an API holds whatever it holds, and TRY_CAST
    # is how a query asks about it without one bad value costing the answer.
    ("try-cast-a-number", "SELECT TRY_CAST('12' AS int) AS v"),
    ("try-cast-a-word", "SELECT TRY_CAST('x' AS int) AS v"),
    ("try-cast-a-decimal-point-into-an-int", "SELECT TRY_CAST('2.0' AS int) AS v"),
    ("try-cast-blank-text", "SELECT TRY_CAST('' AS int) AS v"),
    ("try-cast-null", "SELECT TRY_CAST(NULL AS int) AS v"),
    ("try-cast-a-float", "SELECT TRY_CAST('2.5' AS float) AS v"),
    ("try-cast-a-date", "SELECT TRY_CAST('2026-09-08' AS datetime) AS v"),
    ("try-cast-something-that-is-not-a-date",
     "SELECT TRY_CAST('nope' AS datetime) AS v"),
    # Truncating text is what a sized cast is for, so it is not a failure and
    # still happens; a number that will not fit is a failure and gives NULL.
    ("try-cast-text-too-long", "SELECT TRY_CAST('abcdef' AS nvarchar(3)) AS v"),
    ("try-cast-a-number-too-long", "SELECT TRY_CAST(123456 AS nvarchar(3)) AS v"),
    ("try-convert-a-word", "SELECT TRY_CONVERT(int, 'x') AS v"),
    ("try-convert-a-number", "SELECT TRY_CONVERT(int, '12') AS v"),
    ("try-cast-over-a-column",
     "SELECT COUNT(TRY_CAST(name AS int)) AS n FROM people"),
    ("try-cast-keeps-the-rows-a-cast-would-cost",
     "SELECT COUNT(*) AS n FROM people WHERE TRY_CAST(name AS int) IS NULL"),

    # A cast to an integer type is held to the range of that type.
    ("cast-past-a-tinyint", "SELECT CAST(300 AS tinyint) AS v"),
    ("cast-below-a-tinyint", "SELECT CAST(-1 AS tinyint) AS v"),
    ("cast-to-the-top-of-a-tinyint", "SELECT CAST(255 AS tinyint) AS v"),
    ("cast-past-a-smallint", "SELECT CAST(99999 AS smallint) AS v"),
    ("cast-past-an-int", "SELECT CAST(3000000000 AS int) AS v"),
    ("cast-inside-a-bigint", "SELECT CAST(3000000000 AS bigint) AS v"),
    ("cast-text-past-an-int", "SELECT CAST('3000000000' AS int) AS v"),
    ("cast-text-past-a-tinyint", "SELECT CAST('300' AS tinyint) AS v"),
    ("cast-a-fraction-into-a-tinyint", "SELECT CAST(2.9 AS tinyint) AS v"),
    ("try-cast-past-a-tinyint", "SELECT TRY_CAST(300 AS tinyint) AS v"),
    ("try-cast-past-an-int", "SELECT TRY_CAST(3000000000 AS int) AS v"),
    ("try-cast-text-past-an-int",
     "SELECT TRY_CAST('99999999999999999999' AS int) AS v"),

    # --- an aggregate inside a larger value ---------------------------------
    # The shape of every percentage and every spread in every report.
    ("a-spread", "SELECT MAX(score) - MIN(score) AS v FROM people"),
    ("a-count-scaled", "SELECT COUNT(*) * 2 AS v FROM people"),
    ("an-average-the-long-way",
     "SELECT SUM(score) / COUNT(score) AS v FROM people"),
    ("a-percentage",
     "SELECT COUNT(*) * CAST(100.0 AS float) / 6 AS v FROM people "
     "WHERE score > 0"),
    ("an-aggregate-inside-a-function", "SELECT ABS(MIN(score)) AS v FROM people"),
    ("an-aggregate-inside-a-cast",
     "SELECT CAST(COUNT(*) AS nvarchar(10)) AS v FROM people"),
    ("an-aggregate-inside-a-case",
     "SELECT CASE WHEN COUNT(*) > 3 THEN 'many' ELSE 'few' END AS v FROM people"),
    ("two-aggregates-and-a-literal",
     "SELECT MAX(score) + MIN(score) + 1 AS v FROM people"),
    ("a-spread-of-a-group-that-is-all-null",
     "SELECT team, MAX(score) - MIN(score) AS v FROM people "
     "WHERE score IS NULL GROUP BY team ORDER BY team"),
    ("a-spread-per-group",
     "SELECT team, MAX(score) - MIN(score) AS v FROM people "
     "GROUP BY team ORDER BY team"),
    # float rather than the decimal literal: decimal arithmetic here is a
    # float on purpose, so 100.0 / 6 would be comparing that decision rather
    # than what the aggregate did.
    ("a-percentage-per-group",
     "SELECT team, COUNT(*) * CAST(100.0 AS float) / 6 AS v FROM people "
     "GROUP BY team ORDER BY team"),
    ("a-grouped-column-beside-an-expression-over-aggregates",
     "SELECT team, COUNT(*) AS n, MAX(score) - MIN(score) AS spread "
     "FROM people GROUP BY team ORDER BY team"),
    ("an-expression-over-aggregates-with-a-having",
     "SELECT team, MAX(score) - MIN(score) AS v FROM people "
     "GROUP BY team HAVING COUNT(*) > 1 ORDER BY team"),
    ("an-expression-over-aggregates-ordered-by",
     "SELECT team, MAX(score) - MIN(score) AS v FROM people "
     "GROUP BY team ORDER BY MAX(score) - MIN(score), team"),
    ("an-expression-over-an-aggregate-and-a-grouped-column",
     "SELECT UPPER(team) AS t, COUNT(*) AS n FROM people "
     "GROUP BY UPPER(team) ORDER BY t"),
    ("an-aggregate-of-an-expression-inside-an-expression",
     "SELECT MAX(score * 2) - MIN(score * 2) AS v FROM people"),
    ("a-count-of-distinct-inside-an-expression",
     "SELECT COUNT(DISTINCT team) * 10 AS v FROM people"),
    # Still refused, because the column is neither grouped nor reduced.
    ("a-column-beside-an-expression-over-aggregates",
     "SELECT name, MAX(score) - MIN(score) AS v FROM people"),

    # --- what else a FROM clause may say ------------------------------------
    ("tables-listed-with-a-comma",
     "SELECT COUNT(*) AS n FROM people, tasks"),
    ("tables-listed-and-related-by-the-where",
     "SELECT COUNT(*) AS n FROM people p, tasks t WHERE t.owner = p.id"),
    ("three-tables-listed",
     "SELECT COUNT(*) AS n FROM people p, tasks t, people q "
     "WHERE t.owner = p.id AND q.id = p.id"),
    ("tables-listed-and-a-join-after",
     "SELECT COUNT(*) AS n FROM people p, tasks t "
     "JOIN people q ON q.id = t.owner"),
    ("listed-tables-read-in-order",
     "SELECT p.name, t.state FROM people p, tasks t "
     "WHERE t.owner = p.id ORDER BY p.id, t.tid"),
    # A hint about locking, which this holds none of.
    ("a-table-hint", "SELECT COUNT(*) AS n FROM people WITH (NOLOCK)"),
    ("a-table-hint-with-no-with", "SELECT COUNT(*) AS n FROM people (NOLOCK)"),
    ("a-table-hint-after-an-alias",
     "SELECT COUNT(*) AS n FROM people AS p WITH (NOLOCK)"),
    ("a-table-hint-with-no-space",
     "SELECT COUNT(*) AS n FROM people WITH(NOLOCK)"),
    ("a-table-hint-on-each-side-of-a-join",
     "SELECT COUNT(*) AS n FROM people p WITH (NOLOCK) "
     "JOIN tasks t WITH (NOLOCK) ON t.owner = p.id"),
    ("a-table-hint-on-listed-tables",
     "SELECT COUNT(*) AS n FROM people WITH (NOLOCK), tasks WITH (NOLOCK)"),

    # --- more of the string and maths functions ------------------------------
    # PATINDEX is LIKE anchored at both ends, reporting where the match began,
    # so a pattern with no trailing % has to reach the end of the value.
    ("patindex-found", "SELECT PATINDEX('%a%', 'bad') AS v"),
    ("patindex-not-found", "SELECT PATINDEX('%z%', 'bad') AS v"),
    ("patindex-at-the-start", "SELECT PATINDEX('a%', 'abc') AS v"),
    ("patindex-a-set", "SELECT PATINDEX('%[0-9]%', 'ab3cd') AS v"),
    ("patindex-has-to-reach-the-end", "SELECT PATINDEX('abc', 'abcy') AS v"),
    ("patindex-reaching-the-end", "SELECT PATINDEX('abc', 'abc') AS v"),
    ("patindex-floating-only-the-start", "SELECT PATINDEX('%abc', 'xabc') AS v"),
    ("patindex-of-nothing", "SELECT PATINDEX('', 'abc') AS v"),
    ("patindex-of-anything", "SELECT PATINDEX('%', 'abc') AS v"),
    ("patindex-one-character", "SELECT PATINDEX('_b%', 'abc') AS v"),
    ("patindex-ignores-case", "SELECT PATINDEX('%a%', 'BAD') AS v"),
    ("patindex-over-a-column",
     "SELECT PATINDEX('%a%', name) AS v FROM people ORDER BY id"),
    ("patindex-of-a-null-pattern", "SELECT PATINDEX(NULL, 'bad') AS v"),

    ("stuff", "SELECT STUFF('abcdef', 2, 3, 'XY') AS v"),
    ("stuff-nothing-out", "SELECT STUFF('abcdef', 2, 0, 'XY') AS v"),
    ("stuff-from-before-the-start", "SELECT STUFF('abcdef', 0, 2, 'X') AS v"),
    ("stuff-from-past-the-end", "SELECT STUFF('abcdef', 9, 2, 'X') AS v"),
    ("stuff-more-than-there-is", "SELECT STUFF('abcdef', 2, 99, 'X') AS v"),
    ("stuff-nothing-in", "SELECT STUFF('abcdef', 2, 3, NULL) AS v"),
    ("stuff-a-negative-length", "SELECT STUFF('abcdef', 2, -1, 'X') AS v"),
    ("stuff-of-null", "SELECT STUFF(NULL, 2, 3, 'X') AS v"),

    ("replicate", "SELECT REPLICATE('ab', 3) AS v"),
    ("replicate-none", "SELECT REPLICATE('ab', 0) AS v"),
    ("replicate-fewer-than-none", "SELECT REPLICATE('ab', -1) AS v"),
    ("replicate-a-number", "SELECT REPLICATE(12, 2) AS v"),
    ("replicate-of-null", "SELECT REPLICATE(NULL, 3) AS v"),
    ("replicate-null-times", "SELECT REPLICATE('ab', NULL) AS v"),

    ("ascii-and-char",
     "SELECT ASCII('A') AS a, ASCII('abc') AS b, ASCII(' ') AS c, "
     "ASCII(65) AS d"),
    ("ascii-of-nothing", "SELECT ASCII('') AS a, ASCII(NULL) AS b"),
    ("char", "SELECT CHAR(65) AS a, CHAR(255) AS b, CHAR('65') AS c"),
    ("char-outside-a-byte",
     "SELECT CHAR(256) AS a, CHAR(-1) AS b, CHAR(NULL) AS c"),
    ("unicode-and-nchar",
     "SELECT UNICODE('A') AS a, UNICODE('') AS b, NCHAR(65) AS c, "
     "NCHAR(9731) AS d"),
    ("nchar-outside-two-bytes",
     "SELECT NCHAR(-1) AS a, NCHAR(65536) AS b, NCHAR(65535) AS c"),

    # The values that are NULL are left out, and the separator around them
    # with them, which is the whole point of it.
    ("concat-ws", "SELECT CONCAT_WS('-', 'a', 'b', 'c') AS v"),
    ("concat-ws-skips-null", "SELECT CONCAT_WS('-', 'a', NULL, 'c') AS v"),
    ("concat-ws-of-nulls", "SELECT CONCAT_WS('-', NULL, NULL) AS v"),
    ("concat-ws-with-no-separator", "SELECT CONCAT_WS(NULL, 'a', 'b') AS v"),
    ("concat-ws-of-numbers", "SELECT CONCAT_WS('-', 1, 2) AS v"),
    ("concat-ws-with-too-few", "SELECT CONCAT_WS('-', 'a') AS v"),

    ("log", "SELECT LOG(10) AS a, LOG(10, 10) AS b, LOG(1) AS c"),
    ("log10", "SELECT LOG10(100) AS v"),
    ("log-of-nothing", "SELECT LOG(0) AS v"),
    ("log-of-less-than-nothing", "SELECT LOG(-1) AS v"),
    ("exp", "SELECT EXP(1) AS a, EXP(0) AS b"),
    ("exp-past-what-a-float-holds", "SELECT EXP(1000) AS v"),
    ("square", "SELECT SQUARE(3) AS a, SQUARE(2.5) AS b, SQUARE(-3) AS c"),
    ("pi", "SELECT PI() AS v"),
    ("maths-of-null",
     "SELECT SQUARE(NULL) AS a, EXP(NULL) AS b, LOG(NULL) AS c"),

    ("choose", "SELECT CHOOSE(2, 'a', 'b', 'c') AS v"),
    ("choose-before-the-first", "SELECT CHOOSE(0, 'a', 'b') AS v"),
    ("choose-past-the-last", "SELECT CHOOSE(9, 'a', 'b') AS v"),
    ("choose-truncates-what-it-is-given", "SELECT CHOOSE(2.9, 'a', 'b', 'c') AS v"),
    ("choose-of-null", "SELECT CHOOSE(NULL, 'a') AS v"),

    ("translate", "SELECT TRANSLATE('abcdef', 'abc', 'xyz') AS v"),
    ("translate-of-different-lengths", "SELECT TRANSLATE('abc', 'ab', 'x') AS v"),
    ("translate-of-null", "SELECT TRANSLATE(NULL, 'ab', 'xy') AS v"),

    # --- a group of something every row agrees on ---------------------------
    # One group of everything is not the question anyone meant to ask, and a
    # real server refuses it rather than answering it.
    ("group-by-a-number", "SELECT COUNT(*) AS n FROM people GROUP BY 1"),
    ("group-by-some-text", "SELECT COUNT(*) AS n FROM people GROUP BY 'x'"),
    ("group-by-arithmetic-on-nothing",
     "SELECT COUNT(*) AS n FROM people GROUP BY 1 + 1"),
    ("group-by-the-time-of-day",
     "SELECT COUNT(*) AS n FROM people GROUP BY GETDATE()"),

    # --- a function over a window -------------------------------------------
    # Answered once per row, over a set of rows, which is neither what an
    # aggregate does nor what an ordinary expression does.
    ("row-number",
     "SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS r FROM people ORDER BY id"),
    ("row-number-by-something-with-nulls",
     "SELECT id, ROW_NUMBER() OVER (ORDER BY score) AS r FROM people "
     "ORDER BY id"),
    ("row-number-descending",
     "SELECT id, ROW_NUMBER() OVER (ORDER BY id DESC) AS r FROM people "
     "ORDER BY id"),
    ("row-number-by-two-things",
     "SELECT id, ROW_NUMBER() OVER (ORDER BY team, id) AS r FROM people "
     "ORDER BY id"),
    # RANK counts the rows before this one's ties; DENSE_RANK counts the ties.
    ("rank", "SELECT id, RANK() OVER (ORDER BY rank) AS r FROM people ORDER BY id"),
    ("dense-rank",
     "SELECT id, DENSE_RANK() OVER (ORDER BY rank) AS r FROM people ORDER BY id"),
    ("rank-over-a-column-with-nulls",
     "SELECT id, RANK() OVER (ORDER BY score) AS r FROM people ORDER BY id"),
    ("ntile-two",
     "SELECT id, NTILE(2) OVER (ORDER BY id) AS r FROM people ORDER BY id"),
    ("ntile-four-does-not-divide",
     "SELECT id, NTILE(4) OVER (ORDER BY id) AS r FROM people ORDER BY id"),
    ("ntile-more-tiles-than-rows",
     "SELECT id, NTILE(9) OVER (ORDER BY id) AS r FROM people ORDER BY id"),

    ("row-number-per-partition",
     "SELECT id, team, ROW_NUMBER() OVER (PARTITION BY team ORDER BY id) AS r "
     "FROM people ORDER BY id"),
    ("rank-per-partition",
     "SELECT id, team, RANK() OVER (PARTITION BY team ORDER BY rank) AS r "
     "FROM people ORDER BY id"),

    # An aggregate over a window is not an aggregate of the statement: it
    # answers once per row rather than reducing the rows.
    ("count-over-everything",
     "SELECT id, COUNT(*) OVER () AS n FROM people ORDER BY id"),
    ("count-over-a-partition",
     "SELECT id, team, COUNT(*) OVER (PARTITION BY team) AS n FROM people "
     "ORDER BY id"),
    ("count-of-a-column-over-a-partition",
     "SELECT id, COUNT(score) OVER (PARTITION BY team) AS n FROM people "
     "ORDER BY id"),
    ("sum-over-a-partition",
     "SELECT id, team, SUM(score) OVER (PARTITION BY team) AS s FROM people "
     "ORDER BY id"),
    ("min-and-max-over-everything",
     "SELECT id, MIN(score) OVER () AS a, MAX(score) OVER () AS b FROM people "
     "ORDER BY id"),
    ("average-over-a-partition",
     "SELECT id, AVG(score) OVER (PARTITION BY team) AS a FROM people "
     "ORDER BY id"),
    # With an order it reaches to the end of this row's ties, which is what
    # makes it a running total where the order is unique and the whole
    # partition's where it is not.
    ("a-running-total",
     "SELECT id, SUM(score) OVER (ORDER BY id) AS s FROM people ORDER BY id"),
    ("a-running-count",
     "SELECT id, COUNT(*) OVER (PARTITION BY team ORDER BY id) AS n "
     "FROM people ORDER BY id"),
    ("a-total-shared-by-ties",
     "SELECT id, team, SUM(score) OVER (ORDER BY team) AS s FROM people "
     "ORDER BY id"),
    ("a-count-shared-by-ties",
     "SELECT id, COUNT(*) OVER (ORDER BY team) AS n FROM people ORDER BY id"),
    ("a-running-minimum",
     "SELECT id, MIN(score) OVER (ORDER BY id) AS a, "
     "MAX(score) OVER (ORDER BY id) AS b FROM people ORDER BY id"),

    ("lag-and-lead",
     "SELECT id, LAG(score) OVER (ORDER BY id) AS a, "
     "LEAD(score) OVER (ORDER BY id) AS b FROM people ORDER BY id"),
    ("lag-further-back-with-a-default",
     "SELECT id, LAG(score, 2, -1) OVER (ORDER BY id) AS a FROM people "
     "ORDER BY id"),
    ("lag-of-nothing", "SELECT id, LAG(score, 0) OVER (ORDER BY id) AS a "
     "FROM people ORDER BY id"),
    ("lag-inside-a-partition",
     "SELECT id, LAG(score) OVER (PARTITION BY team ORDER BY id) AS a "
     "FROM people ORDER BY id"),
    # LAST_VALUE with a plain order is this row, because the frame ends here.
    ("first-and-last-value",
     "SELECT id, FIRST_VALUE(name) OVER (ORDER BY id) AS a, "
     "LAST_VALUE(name) OVER (ORDER BY id) AS b FROM people ORDER BY id"),
    ("first-value-of-a-partition-backwards",
     "SELECT id, FIRST_VALUE(name) OVER (PARTITION BY team ORDER BY id DESC) "
     "AS a FROM people ORDER BY id"),

    # Where it sits among everything else: the window is worked out over the
    # rows the WHERE kept, before the sort and before TOP.
    ("a-window-after-a-where",
     "SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS r FROM people "
     "WHERE id > 2 ORDER BY id"),
    ("a-window-ordered-by-its-own-name",
     "SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS r FROM people "
     "ORDER BY r DESC"),
    ("a-window-and-then-top",
     "SELECT TOP 2 id, ROW_NUMBER() OVER (ORDER BY id DESC) AS r FROM people "
     "ORDER BY id"),
    ("a-window-read-back-out-of-a-derived-table",
     "SELECT id, r FROM (SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS r "
     "FROM people) AS x WHERE r <= 2 ORDER BY id"),
    ("a-window-beside-a-star",
     "SELECT *, ROW_NUMBER() OVER (ORDER BY id) AS r FROM people ORDER BY id"),
    ("a-window-over-an-expression",
     "SELECT id, ROW_NUMBER() OVER (ORDER BY score * -1) AS r FROM people "
     "ORDER BY id"),
    ("a-window-partitioned-by-an-expression",
     "SELECT id, COUNT(*) OVER (PARTITION BY UPPER(team)) AS n FROM people "
     "ORDER BY id"),
    ("two-windows-at-once",
     "SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS a, "
     "COUNT(*) OVER () AS b FROM people ORDER BY id"),
    ("a-window-over-nothing",
     "SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS r FROM people "
     "WHERE id > 99 ORDER BY id"),

    # And where it may not be.
    ("a-window-in-the-where",
     "SELECT id FROM people WHERE ROW_NUMBER() OVER (ORDER BY id) = 1"),
    ("a-window-in-the-having",
     "SELECT id FROM people GROUP BY id HAVING COUNT(*) OVER () = 1"),
    ("a-window-with-no-over", "SELECT ROW_NUMBER() AS r FROM people"),
    ("a-window-with-no-order", "SELECT ROW_NUMBER() OVER () AS r FROM people"),
    ("a-window-over-distinct",
     "SELECT SUM(DISTINCT score) OVER () AS s FROM people"),

    # --- and how much of the window one row sees ----------------------------
    ("frame-from-the-start",
     "SELECT id, SUM(id) OVER (ORDER BY id ROWS UNBOUNDED PRECEDING) AS s "
     "FROM people ORDER BY id"),
    ("frame-from-the-start-written-out",
     "SELECT id, SUM(id) OVER (ORDER BY id ROWS BETWEEN UNBOUNDED PRECEDING "
     "AND CURRENT ROW) AS s FROM people ORDER BY id"),
    ("frame-one-row-back",
     "SELECT id, SUM(id) OVER (ORDER BY id ROWS BETWEEN 1 PRECEDING AND "
     "CURRENT ROW) AS s FROM people ORDER BY id"),
    ("frame-either-side",
     "SELECT id, SUM(id) OVER (ORDER BY id ROWS BETWEEN 1 PRECEDING AND "
     "1 FOLLOWING) AS s FROM people ORDER BY id"),
    ("frame-to-the-end",
     "SELECT id, SUM(id) OVER (ORDER BY id ROWS BETWEEN CURRENT ROW AND "
     "UNBOUNDED FOLLOWING) AS s FROM people ORDER BY id"),
    ("frame-of-everything",
     "SELECT id, SUM(id) OVER (ORDER BY id ROWS BETWEEN UNBOUNDED PRECEDING "
     "AND UNBOUNDED FOLLOWING) AS s FROM people ORDER BY id"),
    ("frame-of-this-row-only",
     "SELECT id, SUM(id) OVER (ORDER BY id ROWS CURRENT ROW) AS s "
     "FROM people ORDER BY id"),
    # Entirely behind this row, so the first row sees nothing at all.
    ("frame-that-holds-nothing-at-the-start",
     "SELECT id, SUM(id) OVER (ORDER BY id ROWS BETWEEN 2 PRECEDING AND "
     "1 PRECEDING) AS s FROM people ORDER BY id"),
    ("frame-wider-than-the-rows",
     "SELECT id, SUM(id) OVER (ORDER BY id ROWS BETWEEN 9 PRECEDING AND "
     "9 FOLLOWING) AS s FROM people ORDER BY id"),
    ("frame-inside-a-partition",
     "SELECT id, team, SUM(id) OVER (PARTITION BY team ORDER BY id ROWS "
     "BETWEEN 1 PRECEDING AND CURRENT ROW) AS s FROM people ORDER BY id"),
    ("frame-counted-over",
     "SELECT id, COUNT(*) OVER (ORDER BY id ROWS BETWEEN 1 PRECEDING AND "
     "1 FOLLOWING) AS n FROM people ORDER BY id"),
    ("frame-averaged-over",
     "SELECT id, AVG(score) OVER (ORDER BY id ROWS BETWEEN 1 PRECEDING AND "
     "1 FOLLOWING) AS a FROM people ORDER BY id"),
    ("frame-with-a-minimum",
     "SELECT id, MIN(score) OVER (ORDER BY id ROWS BETWEEN 1 PRECEDING AND "
     "1 FOLLOWING) AS a FROM people ORDER BY id"),
    # The classic surprise undone: told to look ahead, LAST_VALUE does.
    ("last-value-told-to-look-ahead",
     "SELECT id, LAST_VALUE(id) OVER (ORDER BY id ROWS BETWEEN UNBOUNDED "
     "PRECEDING AND UNBOUNDED FOLLOWING) AS l FROM people ORDER BY id"),
    ("first-value-over-a-moving-frame",
     "SELECT id, FIRST_VALUE(id) OVER (ORDER BY id ROWS BETWEEN 1 PRECEDING "
     "AND CURRENT ROW) AS f FROM people ORDER BY id"),
    # RANGE counts by what the rows tie on rather than by rows.
    ("range-to-this-row",
     "SELECT id, team, COUNT(*) OVER (ORDER BY team RANGE BETWEEN UNBOUNDED "
     "PRECEDING AND CURRENT ROW) AS n FROM people ORDER BY id"),
    ("range-from-the-start",
     "SELECT id, team, SUM(id) OVER (ORDER BY team RANGE UNBOUNDED "
     "PRECEDING) AS s FROM people ORDER BY id"),

    ("range-with-a-number", "SELECT SUM(id) OVER (ORDER BY id RANGE BETWEEN "
     "1 PRECEDING AND CURRENT ROW) AS s FROM people"),
    ("frame-that-ends-before-it-begins",
     "SELECT SUM(id) OVER (ORDER BY id ROWS BETWEEN 1 FOLLOWING AND "
     "1 PRECEDING) AS s FROM people"),
    ("a-frame-on-something-that-may-not-have-one",
     "SELECT ROW_NUMBER() OVER (ORDER BY id ROWS UNBOUNDED PRECEDING) AS r "
     "FROM people"),
    ("a-frame-with-nothing-to-count-from",
     "SELECT SUM(id) OVER (ROWS UNBOUNDED PRECEDING) AS s FROM people"),

    # --- how CONVERT writes a moment out -----------------------------------
    # The style says the shape, and none of the shapes are guessable: the
    # spacing around a one-digit hour differs between two of them, and the
    # same number means a two-digit year below a hundred and four above it.
    ("style-0",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 0) AS v"),
    ("style-1",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 1) AS v"),
    ("style-2",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 2) AS v"),
    ("style-3",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 3) AS v"),
    ("style-4",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 4) AS v"),
    ("style-5",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 5) AS v"),
    ("style-6",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 6) AS v"),
    ("style-7",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 7) AS v"),
    ("style-8",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 8) AS v"),
    ("style-9",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 9) AS v"),
    ("style-10",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 10) AS v"),
    ("style-11",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 11) AS v"),
    ("style-12",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 12) AS v"),
    ("style-13",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 13) AS v"),
    ("style-14",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 14) AS v"),
    ("style-20",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 20) AS v"),
    ("style-21",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 21) AS v"),
    ("style-22",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 22) AS v"),
    ("style-23",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 23) AS v"),
    ("style-24",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 24) AS v"),
    ("style-25",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 25) AS v"),
    ("style-100",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 100) AS v"),
    ("style-101",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 101) AS v"),
    ("style-102",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 102) AS v"),
    ("style-103",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 103) AS v"),
    ("style-104",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 104) AS v"),
    ("style-105",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 105) AS v"),
    ("style-106",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 106) AS v"),
    ("style-107",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 107) AS v"),
    ("style-108",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 108) AS v"),
    ("style-109",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 109) AS v"),
    ("style-110",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 110) AS v"),
    ("style-111",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 111) AS v"),
    ("style-112",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 112) AS v"),
    ("style-113",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 113) AS v"),
    ("style-114",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 114) AS v"),
    ("style-120",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 120) AS v"),
    ("style-121",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 121) AS v"),
    ("style-126",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 126) AS v"),
    ("style-127",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 127) AS v"),
    # The size still holds, so a style wider than the column is cut short.
    ("style-cut-to-the-column", "SELECT CONVERT(nvarchar(5), CAST('2026-09-08T14:35:47.123' AS datetime), 101) AS v"),
    ("style-that-is-not-one", "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime), 999) AS v"),
    ("style-on-something-that-is-not-a-moment",
     "SELECT CONVERT(nvarchar(10), 12345, 1) AS v"),
    ("a-convert-with-no-style",
     "SELECT CONVERT(nvarchar(50), CAST('2026-09-08T14:35:47.123' AS datetime)) AS v"),
    ("a-style-over-a-column",
     "SELECT CONVERT(nvarchar(10), DATEADD(day, id, CAST('2026-01-01' AS datetime)), 112) AS v FROM people ORDER BY id"),

    # --- a HAVING that holds a subquery -------------------------------------
    # Lifted the way a WHERE's is. Without that the condition reached the
    # parser with a SELECT still written out in it.
    ("having-a-subquery",
     "SELECT team FROM people GROUP BY team HAVING COUNT(*) = (SELECT 2) "
     "ORDER BY team"),
    ("having-a-subquery-over-a-table",
     "SELECT team FROM people GROUP BY team "
     "HAVING COUNT(*) > (SELECT AVG(hours) FROM tasks) ORDER BY team"),
    ("having-the-biggest-group",
     "SELECT team FROM people GROUP BY team HAVING COUNT(*) = "
     "(SELECT MAX(c) FROM (SELECT COUNT(*) AS c FROM people GROUP BY team) "
     "AS x) ORDER BY team"),
    ("having-an-in-over-a-subquery",
     "SELECT team FROM people GROUP BY team "
     "HAVING COUNT(*) IN (SELECT COUNT(*) FROM tasks GROUP BY state) "
     "ORDER BY team"),

    # --- a statement written out as text ------------------------------------
    ("exec-a-string", "EXEC ('SELECT 4 AS v')"),
    ("exec-sql-by-name", "EXEC sp_executesql N'SELECT 1 AS v'"),
    ("exec-sql-spelled-out", "EXECUTE sp_executesql N'SELECT 2 AS v'"),
    ("exec-sql-where-it-lives",
     "EXEC master.dbo.sp_executesql N'SELECT 3 AS v'"),
    ("exec-sql-with-a-quote-in-it",
     "EXEC sp_executesql N'SELECT ''quoted'' AS v'"),
    ("exec-sql-over-a-table",
     "EXEC sp_executesql N'SELECT COUNT(*) AS n FROM people'"),
    ("exec-sql-with-a-value",
     "EXEC sp_executesql N'SELECT @x AS v', N'@x int', @x = 7"),
    ("exec-sql-with-two-values",
     "EXEC sp_executesql N'SELECT @a + @b AS v', N'@a int, @b int', "
     "@a = 2, @b = 3"),
    ("exec-sql-with-a-value-it-reads-by",
     "EXEC sp_executesql N'SELECT COUNT(*) AS n FROM people WHERE id <= @n', "
     "N'@n int', @n = 2"),

    # --- a join that keeps what the other side matched nothing of ------------
    ("right-join-counted",
     "SELECT COUNT(*) AS n FROM people p RIGHT JOIN tasks t "
     "ON t.owner = p.id"),
    ("full-join-counted",
     "SELECT COUNT(*) AS n FROM people p FULL JOIN tasks t ON t.owner = p.id"),
    ("right-outer-join-is-the-same",
     "SELECT COUNT(*) AS n FROM people p RIGHT OUTER JOIN tasks t "
     "ON t.owner = p.id"),
    ("full-outer-join-is-the-same",
     "SELECT COUNT(*) AS n FROM people p FULL OUTER JOIN tasks t "
     "ON t.owner = p.id"),
    ("right-join-keeps-the-unmatched-row",
     "SELECT p.id, t.tid FROM people p RIGHT JOIN tasks t ON t.owner = p.id "
     "ORDER BY t.tid"),
    ("full-join-keeps-both-sides",
     "SELECT p.id, t.tid FROM people p FULL JOIN tasks t ON t.owner = p.id "
     "ORDER BY p.id, t.tid"),
    ("right-join-with-nothing-to-hash-on",
     "SELECT COUNT(*) AS n FROM people p RIGHT JOIN tasks t "
     "ON t.owner > p.id"),
    ("full-join-with-nothing-to-hash-on",
     "SELECT COUNT(*) AS n FROM people p FULL JOIN tasks t ON t.owner > p.id"),
    ("right-join-then-a-where",
     "SELECT COUNT(*) AS n FROM people p RIGHT JOIN tasks t "
     "ON t.owner = p.id WHERE p.id IS NULL"),
    ("full-join-grouped",
     "SELECT p.team, COUNT(*) AS n FROM people p FULL JOIN tasks t "
     "ON t.owner = p.id GROUP BY p.team ORDER BY p.team"),
    ("right-join-onto-a-null-key",
     "SELECT COUNT(*) AS n FROM people p RIGHT JOIN people q "
     "ON q.team = p.team"),

    # --- the other two things TOP may say ------------------------------------
    # A share of the rows, rounded up, and whatever ties with the last one.
    ("top-half", "SELECT TOP 50 PERCENT id FROM people ORDER BY id"),
    ("top-a-third", "SELECT TOP 30 PERCENT id FROM people ORDER BY id"),
    ("top-a-hundredth-is-still-a-row",
     "SELECT TOP 1 PERCENT id FROM people ORDER BY id"),
    ("top-none-of-it", "SELECT TOP 0 PERCENT id FROM people ORDER BY id"),
    ("top-all-of-it", "SELECT TOP 100 PERCENT id FROM people ORDER BY id"),
    ("top-a-share-of-a-filtered-set",
     "SELECT TOP 50 PERCENT id FROM people WHERE id > 2 ORDER BY id"),
    ("top-with-ties-on-a-column-that-has-them",
     "SELECT TOP 2 WITH TIES id, rank FROM people ORDER BY rank"),
    ("top-with-ties-on-a-null",
     "SELECT TOP 1 WITH TIES id, team FROM people ORDER BY team"),
    ("top-with-ties-where-there-are-none",
     "SELECT TOP 2 WITH TIES id FROM people ORDER BY id"),
    ("top-with-ties-reaching-everything",
     "SELECT TOP 1 WITH TIES id FROM people ORDER BY id - id"),
    ("top-with-ties-past-the-rows",
     "SELECT TOP 9 WITH TIES id FROM people ORDER BY id"),
    ("top-with-ties-over-groups",
     "SELECT COUNT(*) AS groups FROM (SELECT TOP 1 WITH TIES team, "
     "COUNT(*) AS n FROM people GROUP BY team ORDER BY n DESC) AS x"),
    ("top-with-ties-and-nothing-to-tie-on",
     "SELECT TOP 2 WITH TIES id FROM people"),

    # --- a comparison against every row of a subquery, or against any ------
    # ALL over nothing is true and ANY over nothing is false: there is no row
    # to break the promise, and none to keep it.
    ("equal-to-any", "SELECT COUNT(*) AS n FROM people "
     "WHERE id = ANY (SELECT owner FROM tasks)"),
    ("unequal-to-all", "SELECT COUNT(*) AS n FROM people "
     "WHERE id <> ALL (SELECT owner FROM tasks)"),
    ("greater-than-all", "SELECT COUNT(*) AS n FROM people "
     "WHERE id > ALL (SELECT hours FROM tasks)"),
    ("greater-than-any", "SELECT COUNT(*) AS n FROM people "
     "WHERE id > ANY (SELECT hours FROM tasks)"),
    ("some-is-any-under-another-name", "SELECT COUNT(*) AS n FROM people "
     "WHERE id > SOME (SELECT hours FROM tasks)"),
    ("greater-than-all-of-nothing", "SELECT COUNT(*) AS n FROM people "
     "WHERE id > ALL (SELECT hours FROM tasks WHERE 1 = 0)"),
    ("greater-than-any-of-nothing", "SELECT COUNT(*) AS n FROM people "
     "WHERE id > ANY (SELECT hours FROM tasks WHERE 1 = 0)"),
    ("less-than-all", "SELECT COUNT(*) AS n FROM people "
     "WHERE id < ALL (SELECT tid FROM tasks)"),
    ("all-over-a-column-holding-null", "SELECT COUNT(*) AS n FROM people "
     "WHERE id > ALL (SELECT hours FROM tasks WHERE hours IS NULL)"),
    ("any-over-a-column-holding-null", "SELECT COUNT(*) AS n FROM people "
     "WHERE id > ANY (SELECT hours FROM tasks WHERE hours IS NULL)"),
    ("all-where-one-is-null", "SELECT COUNT(*) AS n FROM people "
     "WHERE id < ALL (SELECT hours FROM tasks)"),
    ("any-of-a-null-operand", "SELECT COUNT(*) AS n FROM people "
     "WHERE score > ANY (SELECT hours FROM tasks)"),
    ("all-negated", "SELECT COUNT(*) AS n FROM people "
     "WHERE NOT (id > ALL (SELECT hours FROM tasks))"),
    ("any-over-text", "SELECT COUNT(*) AS n FROM people "
     "WHERE name > ANY (SELECT state FROM tasks)"),
    ("any-of-more-than-one-column", "SELECT COUNT(*) AS n FROM people "
     "WHERE id = ANY (SELECT tid, owner FROM tasks)"),

    # --- an insert that names the columns it fills ---------------------------
    # Each of these makes its own table, because the comparison runs every
    # query on one connection and a name used twice would already exist.
    ("insert-by-position",
     "CREATE TABLE #ins1 (a int, b nvarchar(50)); "
     "INSERT #ins1 SELECT id, name FROM people; "
     "SELECT a, b FROM #ins1 ORDER BY a"),
    ("insert-naming-the-columns",
     "CREATE TABLE #ins2 (a int, b nvarchar(50)); "
     "INSERT INTO #ins2 (a, b) SELECT id, name FROM people; "
     "SELECT a, b FROM #ins2 ORDER BY a"),
    ("insert-naming-them-the-other-way-round",
     "CREATE TABLE #ins3 (a int, b nvarchar(50)); "
     "INSERT INTO #ins3 (b, a) SELECT name, id FROM people; "
     "SELECT a, b FROM #ins3 ORDER BY a"),
    ("insert-filling-only-some-of-them",
     "CREATE TABLE #ins4 (a int, b nvarchar(50), c int); "
     "INSERT INTO #ins4 (a) SELECT id FROM people; "
     "SELECT a, b, c FROM #ins4 ORDER BY a"),
    ("insert-naming-a-column-that-is-not-there",
     "CREATE TABLE #ins5 (a int); "
     "INSERT INTO #ins5 (nosuch) SELECT id FROM people; "
     "SELECT a FROM #ins5"),
    ("insert-naming-a-different-number-of-columns",
     "CREATE TABLE #ins6 (a int, b int); "
     "INSERT INTO #ins6 (a, b) SELECT id FROM people; "
     "SELECT a FROM #ins6"),

    # --- a group's values run together ---------------------------------------
    # A NULL is left out and its separator with it; an empty string is a
    # value and stays; a group of nothing but NULLs is NULL.
    ("string-agg-in-order",
     "SELECT STRING_AGG(name, ',') WITHIN GROUP (ORDER BY id) AS v "
     "FROM people"),
    ("string-agg-in-another-order",
     "SELECT STRING_AGG(name, ',') WITHIN GROUP (ORDER BY name DESC) AS v "
     "FROM people"),
    ("string-agg-per-group",
     "SELECT team, STRING_AGG(name, ',') WITHIN GROUP (ORDER BY id) AS v "
     "FROM people GROUP BY team ORDER BY team"),
    ("string-agg-of-nothing",
     "SELECT STRING_AGG(name, ',') AS v FROM people WHERE 1 = 0"),
    ("string-agg-of-only-nulls",
     "SELECT STRING_AGG(team, ',') AS v FROM people WHERE team IS NULL"),
    ("string-agg-of-numbers",
     "SELECT STRING_AGG(rank, '-') WITHIN GROUP (ORDER BY id) AS v "
     "FROM people"),
    ("string-agg-with-no-separator",
     "SELECT STRING_AGG(name, NULL) WITHIN GROUP (ORDER BY id) AS v "
     "FROM people"),
    ("string-agg-with-an-empty-separator",
     "SELECT STRING_AGG(name, '') WITHIN GROUP (ORDER BY id) AS v "
     "FROM people"),
    ("string-agg-over-an-expression",
     "SELECT STRING_AGG(name + '!', ',') WITHIN GROUP (ORDER BY id) AS v "
     "FROM people"),
    ("string-agg-beside-another-aggregate",
     "SELECT team, COUNT(*) AS n, STRING_AGG(name, ',') "
     "WITHIN GROUP (ORDER BY id) AS v FROM people GROUP BY team "
     "ORDER BY team"),
    ("string-agg-with-a-having",
     "SELECT team, STRING_AGG(name, ',') WITHIN GROUP (ORDER BY id) AS v "
     "FROM people GROUP BY team HAVING COUNT(*) > 1 ORDER BY team"),
    ("string-agg-of-a-moment",
     "SELECT STRING_AGG(CAST(DATEADD(day, rank, "
     "CAST('2026-01-01' AS datetime)) AS nvarchar(30)), ' | ') "
     "WITHIN GROUP (ORDER BY id) AS v FROM people"),

    # --- a select that makes the table it fills ------------------------------
    # A name each, because the comparison runs every query on one connection.
    ("select-into", "SELECT id, name INTO #into1 FROM people; "
     "SELECT id, name FROM #into1 ORDER BY id"),
    ("select-into-keeps-the-aliases",
     "SELECT id AS a, name AS b INTO #into2 FROM people; "
     "SELECT a, b FROM #into2 ORDER BY a"),
    ("select-into-a-whole-star",
     "SELECT * INTO #into3 FROM people; "
     "SELECT COUNT(*) AS n FROM #into3"),
    ("select-into-an-aggregate",
     "SELECT COUNT(*) AS n INTO #into4 FROM people; "
     "SELECT n FROM #into4"),
    ("select-into-nothing-still-makes-it",
     "SELECT id INTO #into5 FROM people WHERE 1 = 0; "
     "SELECT COUNT(*) AS n FROM #into5"),
    ("select-into-then-read-and-filter",
     "SELECT id, team INTO #into6 FROM people; "
     "SELECT COUNT(*) AS n FROM #into6 WHERE team IS NOT NULL"),
    ("select-into-a-column-with-no-name",
     "SELECT id + 1 INTO #into7 FROM people; SELECT * FROM #into7"),
    ("select-into-a-name-already-taken",
     "SELECT id INTO #into8 FROM people; "
     "SELECT id INTO #into8 FROM people; SELECT * FROM #into8"),

    # --- an apply that reads a select ----------------------------------------
    # Run again for every row, which is what makes it an apply and not a join.
    ("apply-a-select-that-reads-the-row",
     "SELECT p.id, x.n FROM people p CROSS APPLY "
     "(SELECT COUNT(*) AS n FROM tasks WHERE owner = p.id) x ORDER BY p.id"),
    ("apply-the-first-of-them",
     "SELECT p.id, x.tid FROM people p CROSS APPLY "
     "(SELECT TOP 1 tid FROM tasks WHERE owner = p.id ORDER BY tid) x "
     "ORDER BY p.id"),
    ("apply-that-answers-nothing-drops-the-row",
     "SELECT p.id, x.tid FROM people p CROSS APPLY "
     "(SELECT tid FROM tasks WHERE owner = p.id) x ORDER BY p.id, x.tid"),
    ("outer-apply-keeps-it",
     "SELECT p.id, x.tid FROM people p OUTER APPLY "
     "(SELECT TOP 1 tid FROM tasks WHERE owner = p.id ORDER BY tid) x "
     "ORDER BY p.id"),
    ("apply-a-select-that-reads-nothing-of-the-row",
     "SELECT COUNT(*) AS n FROM people p CROSS APPLY (SELECT 1 AS one) x"),
    ("apply-then-a-where",
     "SELECT COUNT(*) AS n FROM people p CROSS APPLY "
     "(SELECT COUNT(*) AS n FROM tasks WHERE owner = p.id) x WHERE x.n > 0"),
    ("apply-more-than-one",
     "SELECT p.id, x.a, y.b FROM people p CROSS APPLY (SELECT 1 AS a) x "
     "CROSS APPLY (SELECT 2 AS b) y ORDER BY p.id"),
    ("apply-beside-a-join",
     "SELECT COUNT(*) AS n FROM people p JOIN tasks t ON t.owner = p.id "
     "CROSS APPLY (SELECT 1 AS one) x"),
    ("apply-of-values-still-works",
     "SELECT p.id, x.one FROM people p CROSS APPLY (VALUES (1), (2)) AS x(one) "
     "ORDER BY p.id, x.one"),
    ("outer-apply-of-values",
     "SELECT COUNT(*) AS n FROM people p OUTER APPLY (VALUES (1)) AS x(one)"),

    # A select written inside brackets, which is how a client writes each
    # part of a combination.
    ("brackets-alone", "(SELECT id FROM people)"),
    # A real server refuses an ORDER BY inside the brackets: msg 156,
    # incorrect syntax near ORDER. Answered here because the order a person
    # asked for is the order they get, and refusing it would buy nothing but
    # the resemblance. One inside a combination is still refused, there and
    # here, because rows about to be combined and reordered cannot be
    # ordered first.
    ("mine-only-brackets-with-their-own-order-by",
     "(SELECT id FROM people ORDER BY id)"),
    ("brackets-both-parts",
     "(SELECT id FROM people) UNION (SELECT owner FROM tasks) ORDER BY id"),
    ("brackets-one-part",
     "(SELECT id FROM people) UNION SELECT owner FROM tasks ORDER BY id"),
    ("brackets-the-other-part",
     "SELECT id FROM people UNION (SELECT owner FROM tasks) ORDER BY id"),
    ("brackets-inside-brackets", "((SELECT COUNT(*) AS n FROM people))"),
    ("brackets-except",
     "(SELECT id FROM people) EXCEPT (SELECT owner FROM tasks) ORDER BY id"),
    ("brackets-intersect",
     "(SELECT id FROM people) INTERSECT (SELECT owner FROM tasks) ORDER BY id"),

    # A hint about the plan, which cannot change the answer.
    ("hint-recompile", "SELECT COUNT(*) AS n FROM people OPTION (RECOMPILE)"),
    ("hint-after-order-by",
     "SELECT id FROM people ORDER BY id OPTION (MAXDOP 1)"),
    ("hint-after-where",
     "SELECT id FROM people WHERE id > 2 ORDER BY id OPTION (RECOMPILE)"),
    ("hint-after-having",
     "SELECT team, COUNT(*) AS n FROM people GROUP BY team "
     "HAVING COUNT(*) > 1 ORDER BY team OPTION (HASH GROUP)"),
    ("hint-with-no-from", "SELECT 1 AS v OPTION (RECOMPILE)"),

    # The aggregates a person writes that this did not have.
    ("count-big", "SELECT COUNT_BIG(*) AS n FROM people"),
    ("count-big-of-a-column", "SELECT COUNT_BIG(score) AS n FROM people"),
    ("count-big-distinct", "SELECT COUNT_BIG(DISTINCT team) AS n FROM people"),
    ("count-all-written-out", "SELECT COUNT(ALL team) AS n FROM people"),
    ("sum-all-written-out", "SELECT SUM(ALL id) AS n FROM people"),
    ("stdev", "SELECT STDEV(score) AS v FROM people"),
    ("stdevp", "SELECT STDEVP(score) AS v FROM people"),
    ("var", "SELECT VAR(score) AS v FROM people"),
    ("varp", "SELECT VARP(score) AS v FROM people"),
    ("spread-per-group",
     "SELECT team, VAR(score) AS v FROM people GROUP BY team ORDER BY team"),
    ("spread-of-one-value", "SELECT VAR(score) AS v FROM people WHERE id = 1"),
    ("spread-of-nothing", "SELECT VAR(score) AS v FROM people WHERE 1 = 0"),
    ("spread-of-integers", "SELECT STDEV(id) AS v, VAR(id) AS w FROM people"),
    ("spread-over-a-window",
     "SELECT id, STDEV(score) OVER (PARTITION BY team) AS s FROM people "
     "ORDER BY id"),
    ("count-big-over-a-window",
     "SELECT id, COUNT_BIG(*) OVER () AS n FROM people ORDER BY id"),

    # A named query that reads itself. Both refuse; what is compared is the
    # number, because a person shown msg 208, invalid object name, goes
    # looking for a table that was never the problem.
    ("names-itself-with-nothing-to-grow-from",
     "WITH loop AS (SELECT id FROM loop) SELECT COUNT(*) AS n FROM loop"),
    ("names-itself-further-in",
     "WITH loop AS (SELECT * FROM (SELECT id FROM loop) x) "
     "SELECT COUNT(*) AS n FROM loop"),
    ("names-itself-in-a-join",
     "WITH loop AS (SELECT p.id FROM loop JOIN people p ON 1 = 1) "
     "SELECT COUNT(*) AS n FROM loop"),
    # A name inside a named query is that query, and a real server says so
    # even where a table of the name exists. Measured with a real table
    # called folk and a CTE called folk: msg 252, the same as the rest.
    ("a-query-named-after-the-table-it-reads",
     "WITH people AS (SELECT id FROM people) SELECT COUNT(*) AS n FROM people"),

    # Trimming named characters rather than whitespace. Wrapped in brackets
    # so a trailing space that was not taken off is visible in the answer.
    ("trim-whitespace", "SELECT '[' + TRIM('  abc  ') + ']' AS v"),
    ("trim-characters", "SELECT '[' + TRIM('xy' FROM 'xyxabcyx') + ']' AS v"),
    ("trim-both", "SELECT '[' + TRIM(BOTH 'x' FROM 'xxabcxx') + ']' AS v"),
    ("trim-leading", "SELECT '[' + TRIM(LEADING 'x' FROM 'xxabcxx') + ']' AS v"),
    ("trim-trailing", "SELECT '[' + TRIM(TRAILING 'x' FROM 'xxabcxx') + ']' AS v"),
    ("ltrim-characters", "SELECT '[' + LTRIM('xyxabc', 'xy') + ']' AS v"),
    ("rtrim-characters", "SELECT '[' + RTRIM('abcxyx', 'xy') + ']' AS v"),
    ("ltrim-whitespace-still", "SELECT '[' + LTRIM('  abc') + ']' AS v"),
    ("trim-of-null", "SELECT TRIM('x' FROM NULL) AS v"),
    ("trim-null-characters", "SELECT TRIM(NULL FROM 'xxabc') AS v"),
    ("trim-no-characters", "SELECT '[' + LTRIM('abc', '') + ']' AS v"),
    ("trim-a-column",
     "SELECT '[' + TRIM('ae' FROM name) + ']' AS v FROM people ORDER BY id"),

    # The biggest and smallest of what it was given.
    ("greatest", "SELECT GREATEST(1, 2, 3) AS v"),
    ("least", "SELECT LEAST(1, 2, 3) AS v"),
    ("greatest-mixed-numbers", "SELECT GREATEST(1, 2.5) AS v"),
    ("greatest-skips-nulls", "SELECT GREATEST(1, NULL, 3) AS v"),
    ("greatest-of-nulls", "SELECT GREATEST(NULL, NULL) AS v"),
    ("greatest-of-text", "SELECT GREATEST('ada', 'Grace', 'bob') AS v"),
    ("least-of-text", "SELECT LEAST('ada', 'Grace', 'bob') AS v"),
    ("greatest-text-converts", "SELECT GREATEST('10', 9) AS v"),
    ("greatest-text-that-will-not", "SELECT GREATEST(1, 'x') AS v"),
    ("greatest-of-one", "SELECT GREATEST(1) AS v"),
    ("greatest-of-columns",
     "SELECT GREATEST(id, score) AS v FROM people ORDER BY id"),
    ("least-of-columns",
     "SELECT LEAST(id, score) AS v FROM people ORDER BY id"),

    # A frame that has nothing in it where it begins, which is what ROWS
    # BETWEEN UNBOUNDED PRECEDING AND n PRECEDING asks for at the first n
    # rows. An aggregate over nothing is NULL, except COUNT, which is 0.
    ("frame-empty-at-the-start-sum",
     "SELECT id, SUM(score) OVER (ORDER BY id ROWS BETWEEN UNBOUNDED "
     "PRECEDING AND 1 PRECEDING) AS s FROM people ORDER BY id"),
    ("frame-empty-for-two-rows",
     "SELECT id, SUM(score) OVER (ORDER BY id ROWS BETWEEN UNBOUNDED "
     "PRECEDING AND 2 PRECEDING) AS s FROM people ORDER BY id"),
    ("frame-empty-throughout",
     "SELECT id, SUM(score) OVER (ORDER BY id ROWS BETWEEN UNBOUNDED "
     "PRECEDING AND 99 PRECEDING) AS s FROM people ORDER BY id"),
    ("frame-empty-at-the-start-count",
     "SELECT id, COUNT(*) OVER (ORDER BY id ROWS BETWEEN UNBOUNDED "
     "PRECEDING AND 1 PRECEDING) AS s FROM people ORDER BY id"),
    ("frame-empty-at-the-start-count-of-a-column",
     "SELECT id, COUNT(score) OVER (ORDER BY id ROWS BETWEEN UNBOUNDED "
     "PRECEDING AND 1 PRECEDING) AS s FROM people ORDER BY id"),
    ("frame-empty-at-the-start-min",
     "SELECT id, MIN(score) OVER (ORDER BY id ROWS BETWEEN UNBOUNDED "
     "PRECEDING AND 1 PRECEDING) AS s FROM people ORDER BY id"),
    ("frame-empty-at-the-start-max-of-text",
     "SELECT id, MAX(name) OVER (ORDER BY id ROWS BETWEEN UNBOUNDED "
     "PRECEDING AND 1 PRECEDING) AS s FROM people ORDER BY id"),
    ("frame-empty-at-the-start-avg",
     "SELECT id, AVG(score) OVER (ORDER BY id ROWS BETWEEN UNBOUNDED "
     "PRECEDING AND 1 PRECEDING) AS s FROM people ORDER BY id"),
    ("frame-empty-at-the-start-per-partition",
     "SELECT id, SUM(score) OVER (PARTITION BY team ORDER BY id ROWS "
     "BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS s FROM people "
     "ORDER BY id"),
    ("frame-empty-at-the-start-spread",
     "SELECT id, STDEV(score) OVER (ORDER BY id ROWS BETWEEN UNBOUNDED "
     "PRECEDING AND 1 PRECEDING) AS s FROM people ORDER BY id"),

    # The running totals the accumulation is for, one per aggregate, so a
    # change to how they are worked out has to keep answering the same.
    ("running-sum",
     "SELECT id, SUM(score) OVER (ORDER BY id) AS s FROM people ORDER BY id"),
    ("running-count",
     "SELECT id, COUNT(score) OVER (ORDER BY id) AS s FROM people "
     "ORDER BY id"),
    ("running-avg",
     "SELECT id, AVG(score) OVER (ORDER BY id) AS s FROM people ORDER BY id"),
    ("running-min",
     "SELECT id, MIN(score) OVER (ORDER BY id) AS s FROM people ORDER BY id"),
    ("running-max-of-text",
     "SELECT id, MAX(name) OVER (ORDER BY id) AS s FROM people ORDER BY id"),
    ("running-min-of-text",
     "SELECT id, MIN(name) OVER (ORDER BY id) AS s FROM people ORDER BY id"),
    ("running-sum-of-integers",
     "SELECT id, SUM(id) OVER (ORDER BY id) AS s FROM people ORDER BY id"),
    ("running-sum-over-ties",
     "SELECT id, SUM(score) OVER (ORDER BY team) AS s FROM people "
     "ORDER BY id"),

    # Which order a window aggregate adds its values in, which shows only
    # where the values are far enough apart that adding them one way and
    # the other give different floats.
    ("order-running-forwards",
     "SELECT [at], SUM(big) OVER (ORDER BY [at]) AS s FROM wide ORDER BY [at]"),
    ("order-running-to-the-end",
     "SELECT [at], SUM(big) OVER (ORDER BY [at] ROWS BETWEEN CURRENT ROW AND "
     "UNBOUNDED FOLLOWING) AS s FROM wide ORDER BY [at]"),
    ("order-whole-partition",
     "SELECT [at], SUM(big) OVER () AS s FROM wide ORDER BY [at]"),
    ("order-a-sliding-frame",
     "SELECT [at], SUM(big) OVER (ORDER BY [at] ROWS BETWEEN 2 PRECEDING AND "
     "CURRENT ROW) AS s FROM wide ORDER BY [at]"),
    ("order-the-mean",
     "SELECT [at], AVG(big) OVER (ORDER BY [at]) AS s FROM wide ORDER BY [at]"),
    ("order-grouped", "SELECT SUM(big) AS s FROM wide"),

    # Which of two values the collation calls equal a window keeps.
    ("ties-running-min",
     "SELECT [at], MIN(word) OVER (ORDER BY [at]) AS s FROM wide ORDER BY [at]"),
    ("ties-running-max",
     "SELECT [at], MAX(word) OVER (ORDER BY [at]) AS s FROM wide ORDER BY [at]"),
    ("ties-min-to-the-end",
     "SELECT [at], MIN(word) OVER (ORDER BY [at] ROWS BETWEEN CURRENT ROW AND "
     "UNBOUNDED FOLLOWING) AS s FROM wide ORDER BY [at]"),
    ("ties-max-to-the-end",
     "SELECT [at], MAX(word) OVER (ORDER BY [at] ROWS BETWEEN CURRENT ROW AND "
     "UNBOUNDED FOLLOWING) AS s FROM wide ORDER BY [at]"),
    ("ties-grouped-min", "SELECT MIN(word) AS s FROM wide"),
    ("ties-grouped-max", "SELECT MAX(word) AS s FROM wide"),
    ("order-spread-to-the-end",
     "SELECT [at], STDEV(big) OVER (ORDER BY [at] ROWS BETWEEN CURRENT ROW "
     "AND UNBOUNDED FOLLOWING) AS s FROM wide ORDER BY [at]"),
    ("order-count-to-the-end",
     "SELECT [at], COUNT(big) OVER (ORDER BY [at] ROWS BETWEEN CURRENT ROW "
     "AND UNBOUNDED FOLLOWING) AS s FROM wide ORDER BY [at]"),
    ("order-mean-to-the-end",
     "SELECT [at], AVG(big) OVER (ORDER BY [at] ROWS BETWEEN CURRENT ROW "
     "AND UNBOUNDED FOLLOWING) AS s FROM wide ORDER BY [at]"),

    # EXISTS matching on one column, which is read once rather than once per
    # outer row. Every shape here either takes that route or has to notice
    # it cannot: the answers say which, because a wrong route answers
    # differently rather than more slowly.
    ("exists-plain",
     "SELECT p.id FROM people p WHERE EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.owner = p.id) ORDER BY p.id"),
    ("exists-the-other-way-round",
     "SELECT p.id FROM people p WHERE EXISTS "
     "(SELECT 1 FROM tasks t WHERE p.id = t.owner) ORDER BY p.id"),
    ("exists-not",
     "SELECT p.id FROM people p WHERE NOT EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.owner = p.id) ORDER BY p.id"),
    ("exists-with-another-condition",
     "SELECT p.id FROM people p WHERE EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.owner = p.id AND t.state = 'open') "
     "ORDER BY p.id"),
    ("exists-with-a-condition-first",
     "SELECT p.id FROM people p WHERE EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.state = 'open' AND t.owner = p.id) "
     "ORDER BY p.id"),
    ("exists-with-two-more-conditions",
     "SELECT p.id FROM people p WHERE EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.hours > 1 AND t.owner = p.id "
     "AND t.state = 'open') ORDER BY p.id"),
    ("exists-on-a-null-key",
     "SELECT p.id FROM people p WHERE EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.owner = p.rank) ORDER BY p.id"),
    ("exists-matching-a-nullable-column",
     "SELECT p.id FROM people p WHERE EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.hours = p.rank) ORDER BY p.id"),
    ("exists-matching-text",
     "SELECT p.id FROM people p WHERE EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.state = p.name) ORDER BY p.id"),
    ("exists-matching-an-expression",
     "SELECT p.id FROM people p WHERE EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.owner + 1 = p.id) ORDER BY p.id"),
    ("exists-matching-an-outer-expression",
     "SELECT p.id FROM people p WHERE EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.owner = p.id + 1) ORDER BY p.id"),
    # These cannot take the shortcut; the answers say whether it noticed.
    ("exists-with-an-or",
     "SELECT p.id FROM people p WHERE EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.owner = p.id OR t.state = 'done') "
     "ORDER BY p.id"),
    ("exists-not-an-equality",
     "SELECT p.id FROM people p WHERE EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.owner > p.id) ORDER BY p.id"),
    ("exists-with-a-group-by",
     "SELECT p.id FROM people p WHERE EXISTS "
     "(SELECT t.owner FROM tasks t WHERE t.owner = p.id "
     "GROUP BY t.owner HAVING COUNT(*) > 1) ORDER BY p.id"),
    ("exists-with-a-top",
     "SELECT p.id FROM people p WHERE EXISTS "
     "(SELECT TOP 1 1 FROM tasks t WHERE t.owner = p.id) ORDER BY p.id"),
    ("exists-on-two-columns",
     "SELECT p.id FROM people p WHERE EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.owner = p.id AND t.hours = p.rank) "
     "ORDER BY p.id"),
    ("exists-with-a-join-inside",
     "SELECT p.id FROM people p WHERE EXISTS "
     "(SELECT 1 FROM tasks t JOIN people q ON q.id = t.owner "
     "WHERE t.owner = p.id) ORDER BY p.id"),
    ("exists-with-nothing-correlated",
     "SELECT p.id FROM people p WHERE EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.state = 'open') ORDER BY p.id"),
    ("exists-counted",
     "SELECT COUNT(*) AS n FROM people p WHERE EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.owner = p.id)"),
    ("exists-beside-another-condition",
     "SELECT p.id FROM people p WHERE p.id > 1 AND EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.owner = p.id) ORDER BY p.id"),
    ("exists-twice",
     "SELECT p.id FROM people p WHERE EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.owner = p.id) AND NOT EXISTS "
     "(SELECT 1 FROM tasks u WHERE u.owner = p.id AND u.state = 'done') "
     "ORDER BY p.id"),
    # A column that is not there. The number is the point: a client shows it,
    # and 208, invalid object name, sends whoever reads it looking for a
    # table that was never the problem.
    ("no-such-column-in-the-select-list", "SELECT nope FROM people"),
    ("no-such-column-in-a-where",
     "SELECT COUNT(*) AS n FROM people WHERE nope = 1"),
    ("no-such-column-in-an-on",
     "SELECT COUNT(*) AS n FROM people p JOIN tasks t ON t.owner = p.nope"),
    ("no-such-column-on-the-other-side-of-an-on",
     "SELECT COUNT(*) AS n FROM people p JOIN tasks t ON t.nope = p.id"),
    ("no-such-column-in-a-left-join",
     "SELECT COUNT(*) AS n FROM people p LEFT JOIN tasks t ON t.owner = p.nope"),
    ("no-such-column-in-an-order-by",
     "SELECT id FROM people ORDER BY nope"),
    ("no-such-column-in-a-group-by",
     "SELECT COUNT(*) AS n FROM people GROUP BY nope"),
    ("no-such-table", "SELECT * FROM nope"),

    ("exists-in-the-select-list",
     "SELECT p.id, CASE WHEN EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.owner = p.id) THEN 1 ELSE 0 END AS has "
     "FROM people p ORDER BY p.id"),
    # --- moments -------------------------------------------------------------
    # A column of real dates, which only a workbook source can produce. Every
    # one of these was answered by both sides before it was kept.
    ("dt-select", "SELECT mid, [when] FROM moments ORDER BY mid"),
    ("dt-order", "SELECT mid FROM moments ORDER BY [when]"),
    ("dt-order-desc", "SELECT mid FROM moments ORDER BY [when] DESC"),
    ("dt-gt", "SELECT mid FROM moments WHERE [when] > '2024-01-01' ORDER BY mid"),
    ("dt-lt", "SELECT mid FROM moments WHERE [when] < '2000-01-01' ORDER BY mid"),
    ("dt-between", "SELECT mid FROM moments "
                   "WHERE [when] BETWEEN '2024-01-01' AND '2024-12-31' ORDER BY mid"),
    ("dt-eq", "SELECT mid FROM moments WHERE [when] = '2024-01-15'"),
    ("dt-is-null", "SELECT mid FROM moments WHERE [when] IS NULL"),
    ("dt-not-null", "SELECT COUNT(*) AS n FROM moments WHERE [when] IS NOT NULL"),
    ("dt-count", "SELECT COUNT([when]) AS n FROM moments"),
    ("dt-min-max", "SELECT MIN([when]) AS lo, MAX([when]) AS hi FROM moments"),
    ("dt-distinct", "SELECT COUNT(*) AS n FROM (SELECT DISTINCT [when] FROM moments) d"),
    ("dt-year", "SELECT mid, YEAR([when]) AS y FROM moments ORDER BY mid"),
    ("dt-month-day",
     "SELECT MONTH([when]) AS m, DAY([when]) AS d FROM moments WHERE mid = 2"),
    ("dt-group-year", "SELECT YEAR([when]) AS y, COUNT(*) AS n FROM moments "
                      "GROUP BY YEAR([when]) ORDER BY y"),
    ("dt-datepart", "SELECT DATEPART(hour, [when]) AS h FROM moments WHERE mid = 2"),
    ("dt-datediff", "SELECT DATEDIFF(day, '2024-01-01', [when]) AS d "
                    "FROM moments WHERE mid = 1"),
    ("dt-dateadd", "SELECT DATEADD(day, 1, [when]) AS d FROM moments WHERE mid = 1"),
    ("dt-cast-text", "SELECT CAST([when] AS nvarchar(30)) AS t "
                     "FROM moments ORDER BY mid"),
    ("dt-case", "SELECT mid, CASE WHEN [when] > '2024-01-01' THEN 'new' "
                "ELSE 'old' END AS era FROM moments ORDER BY mid"),
    ("dt-coalesce", "SELECT COUNT(*) AS n FROM moments "
                    "WHERE COALESCE([when], '1900-01-01') < '1970-01-01'"),
    ("dt-max-over", "SELECT mid, MAX([when]) OVER () AS latest FROM moments "
                    "ORDER BY mid"),
    ("dt-row-number", "SELECT mid, ROW_NUMBER() OVER (ORDER BY [when]) AS r "
                      "FROM moments ORDER BY mid"),
    ("dt-join", "SELECT p.name, m.mid FROM people p JOIN moments m ON p.id = m.mid "
                "WHERE m.[when] IS NOT NULL ORDER BY m.mid"),
    ("dt-schema", "SELECT DATA_TYPE, DATETIME_PRECISION FROM "
                  "INFORMATION_SCHEMA.COLUMNS WHERE COLUMN_NAME = 'when'"),

    # How a date written as text is read, which decides every comparison
    # above: datetime outranks varchar, so the text becomes a moment rather
    # than the moment becoming text.
    ("dt-eq-compact", "SELECT mid FROM moments WHERE [when] = '20240115'"),
    ("dt-eq-with-time", "SELECT mid FROM moments WHERE [when] = '2024-06-01 13:30'"),
    ("dt-eq-t", "SELECT mid FROM moments WHERE [when] = '2024-06-01T13:30:00'"),
    ("dt-gt-unpadded", "SELECT mid FROM moments WHERE [when] > '2024-6-1' ORDER BY mid"),
    ("dt-eq-slashes", "SELECT mid FROM moments WHERE [when] = '2024/01/15'"),
    ("dt-ne", "SELECT COUNT(*) AS n FROM moments WHERE [when] <> '2024-01-15'"),
    ("dt-in", "SELECT mid FROM moments "
              "WHERE [when] IN ('2024-01-15', '1965-03-02') ORDER BY mid"),
    ("dt-eq-nonsense", "SELECT mid FROM moments WHERE [when] = 'nonsense'"),
    ("dt-eq-literal", "SELECT COUNT(*) AS n FROM moments "
                      "WHERE CAST('2024-01-15' AS datetime) = [when]"),
    ("dt-eq-us", "SELECT mid FROM moments WHERE [when] = '01/15/2024'"),
    ("dt-eq-us-dashes", "SELECT mid FROM moments WHERE [when] = '01-15-2024'"),
    ("dt-eq-dotted", "SELECT mid FROM moments WHERE [when] = '2024.01.15'"),
    ("dt-eq-month-name", "SELECT mid FROM moments WHERE [when] = 'Jan 15 2024'"),
    ("dt-eq-month-long", "SELECT mid FROM moments WHERE [when] = 'January 15, 2024'"),
    ("dt-eq-day-first", "SELECT mid FROM moments WHERE [when] = '15 Jan 2024'"),
    ("dt-eq-ampm", "SELECT mid FROM moments WHERE [when] = '2024-06-01 1:30 PM'"),
    ("dt-eq-seconds", "SELECT mid FROM moments WHERE [when] = '2023-12-31 23:59:59'"),
    ("dt-cast-time-only", "SELECT CAST('13:30' AS datetime) AS t"),
    ("dt-cast-number", "SELECT CAST(45304 AS datetime) AS t"),
    ("dt-cast-month-name", "SELECT CAST('Mar 2 1965' AS datetime) AS t"),
    ("dt-cast-unpadded", "SELECT CAST('2024-6-1' AS datetime) AS t"),
    ("dt-cast-nonsense", "SELECT CAST('nonsense' AS datetime) AS t"),
    ("dt-cast-back", "SELECT CAST([when] AS int) AS n FROM moments WHERE mid = 1"),
    ("dt-cast-float", "SELECT CAST([when] AS float) AS n FROM moments WHERE mid = 2"),
    ("dt-cast-int-rounds", "SELECT CAST([when] AS int) AS n FROM moments WHERE mid = 2"),
    ("dt-cast-int-old", "SELECT CAST([when] AS int) AS n FROM moments WHERE mid = 5"),
    ("dt-cast-bigint", "SELECT CAST([when] AS bigint) AS n FROM moments WHERE mid = 2"),

    # --- what the bug hunt found ---------------------------------------------
    # A number too big to be a date. Left alone this was an OverflowError
    # travelling out of the query as an internal error rather than as
    # anything a client can read.
    ("dt-overflow-gt", "SELECT mid FROM moments WHERE [when] > 1e18"),
    ("dt-overflow-eq", "SELECT mid FROM moments WHERE [when] = 1e18"),
    ("dt-overflow-cast", "SELECT CAST(1e18 AS datetime) AS d"),
    ("dt-overflow-in", "SELECT mid FROM moments WHERE [when] IN (1e18)"),
    ("dt-overflow-negative", "SELECT CAST(-1e18 AS datetime) AS d"),

    # LIKE turned a moment into characters with str rather than the
    # conversion everything else uses, so it matched the ISO spelling and
    # not the one a real server produces.
    ("dt-like-iso", "SELECT mid FROM moments WHERE [when] LIKE '2024%'"),
    ("dt-like-default", "SELECT mid FROM moments WHERE [when] LIKE 'Jan%' ORDER BY mid"),
    ("dt-like-month", "SELECT mid FROM moments WHERE [when] LIKE '%2024%' ORDER BY mid"),
    ("dt-not-like", "SELECT COUNT(*) AS n FROM moments WHERE [when] NOT LIKE 'Jan%'"),
    ("dt-patindex", "SELECT PATINDEX('%Jan%', [when]) AS i FROM moments WHERE mid = 1"),
    ("dt-len", "SELECT LEN([when]) AS n FROM moments WHERE mid = 1"),
    ("dt-left", "SELECT LEFT([when], 3) AS l FROM moments WHERE mid = 1"),

    # A join whose two sides are different types. The hash the join buckets
    # on kept the collation and not SQL's type precedence, so a table of
    # numbers joined to the same numbers written as text answered nothing.
    ("join-int-to-text",
     "SELECT p.id FROM people p JOIN tasks t ON p.id = t.state ORDER BY p.id"),
    ("join-int-to-text-reversed",
     "SELECT p.id FROM people p JOIN tasks t ON t.state = p.id ORDER BY p.id"),
    ("join-moment-to-text",
     "SELECT m.mid FROM moments m JOIN people p ON m.[when] = p.name"),
    ("join-text-to-int",
     "SELECT p.id FROM people p JOIN tasks t ON p.name = t.owner ORDER BY p.id"),

    # An unqualified column name in a join, which a join renames out of
    # reach: after one the row is keyed by table.column, and a bare name
    # matched nothing.
    ("join-bare-name-in-on",
     "SELECT tid FROM tasks JOIN people ON owner = id ORDER BY tid"),
    ("join-bare-name-both-sides",
     "SELECT hours FROM tasks JOIN people ON tasks.owner = people.id "
     "ORDER BY hours"),
    ("join-bare-name-in-where",
     "SELECT tid FROM tasks JOIN people ON owner = id WHERE hours > 2"),
    # A bare name that only one of the joined tables has, which is not
    # ambiguous and which a real server answers.
    ("join-bare-name-across-tables",
     "SELECT id FROM people JOIN wide ON people.id = wide.[at] ORDER BY id"),
    # And one both of them have, which is not a spelling mistake and does not
    # get the number for one.
    ("join-ambiguous-name",
     "SELECT id FROM people a JOIN people b ON a.id = b.id"),
    ("join-ambiguous-name-in-where",
     "SELECT a.id FROM people a JOIN people b ON a.id = b.id WHERE name = 'ada'"),

    # @@TRANCOUNT, which a connection keeps for itself and which stood at
    # nought whatever a batch did. Each batch here ends with the count back
    # at nought, because both connections run every query after it and a
    # transaction left open would be read by all of them. The last three are
    # consecutive on purpose: one leaves a transaction open, the next reads
    # it from a batch of its own, and the third closes it.
    ("tran-count-at-rest", "SELECT @@TRANCOUNT AS n"),
    ("tran-begin", "BEGIN TRAN; SELECT @@TRANCOUNT AS n; COMMIT"),
    ("tran-nested",
     "BEGIN TRAN; BEGIN TRANSACTION; SELECT @@TRANCOUNT AS n; COMMIT; COMMIT"),
    ("tran-commit-ends-one-level",
     "BEGIN TRAN; BEGIN TRAN; COMMIT; SELECT @@TRANCOUNT AS n; COMMIT"),
    ("tran-rollback-ends-them-all",
     "BEGIN TRAN; BEGIN TRAN; ROLLBACK; SELECT @@TRANCOUNT AS n"),
    ("tran-savepoint",
     "BEGIN TRAN; SAVE TRAN s1; ROLLBACK TRAN s1; SELECT @@TRANCOUNT AS n; "
     "COMMIT"),
    ("tran-commit-ignores-its-name",
     "BEGIN TRAN t1; BEGIN TRAN t2; COMMIT TRAN whatever; "
     "SELECT @@TRANCOUNT AS n; ROLLBACK"),
    ("tran-rollback-to-the-outer-name",
     "BEGIN TRAN outer1; BEGIN TRAN; ROLLBACK TRAN outer1; "
     "SELECT @@TRANCOUNT AS n"),
    ("tran-leaves-rowcount-at-nought",
     "DECLARE @r int; SELECT @r = rank FROM people; BEGIN TRAN; "
     "SELECT @@ROWCOUNT AS n; COMMIT"),
    ("tran-if-commit",
     "BEGIN TRAN; IF @@TRANCOUNT > 0 COMMIT TRAN; SELECT @@TRANCOUNT AS n"),
    ("tran-if-begin",
     "IF @@TRANCOUNT = 0 BEGIN TRAN; SELECT @@TRANCOUNT AS n; COMMIT"),
    # An IF was given none of the connection's @@ variables, so each was null
    # to it. PRINT leaves the count at nought on both, whatever came before.
    ("if-sees-rowcount", "PRINT 'x'; IF @@ROWCOUNT = 0 SELECT 1 AS one"),
    ("tran-no-semicolons",
     "BEGIN TRAN\nSELECT COUNT(*) AS n FROM people\nCOMMIT"),
    ("tran-lower-case",
     "begin transaction; select @@trancount as n; commit transaction"),
    ("tran-commit-at-rest", "COMMIT"),
    ("tran-rollback-at-rest", "ROLLBACK"),
    ("tran-save-at-rest", "SAVE TRAN s1"),
    ("tran-unknown-savepoint", "BEGIN TRAN; ROLLBACK TRAN nosuch"),
    ("tran-still-open", "SELECT @@TRANCOUNT AS n"),
    ("tran-closed", "ROLLBACK; SELECT @@TRANCOUNT AS n"),
]


def _literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, datetime.datetime):
        # The unseparated form, which SQL Server reads the same way whatever
        # the connection's language and date format are set to.
        return "'" + value.strftime("%Y-%m-%dT%H:%M:%S") + "'"
    if isinstance(value, str):
        return "N'" + value.replace("'", "''") + "'"
    return repr(value)


# Which source each table is written as. Both go into the same temporary
# tables on the real server; the difference is only in what this side reads
# them out of, and moments is a workbook because a workbook is the source that
# hands over a moment rather than text that looks like one.
AS_JSON = (("people", PEOPLE), ("tasks", TASKS), ("wide", WIDE))
AS_A_WORKBOOK = (("moments", MOMENTS),)
FIXTURE = AS_JSON + AS_A_WORKBOOK


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for name, records in AS_JSON:
        (OUT / f"{name}.json").write_text(json.dumps(records), encoding="utf-8")

    for name, records in AS_A_WORKBOOK:
        workbook(OUT / f"{name}.xlsx", {name: [
            list(COLUMNS[name]),
            *[[row[key] for key in COLUMNS[name]] for row in records],
        ]})

    (OUT / "config.json").write_text(json.dumps({
        "tables": [
            *[{"name": name, "json": str(OUT / f"{name}.json")}
              for name, _ in AS_JSON],
            *[{"name": name, "excel": str(OUT / f"{name}.xlsx")}
              for name, _ in AS_A_WORKBOOK],
        ]
    }, indent=2), encoding="utf-8")

    setup = [f"CREATE TABLE #{name} ({columns});"
             for name, columns in TYPES.items()]
    for name, records in FIXTURE:
        for row in records:
            values = ", ".join(_literal(row[k]) for k in COLUMNS[name])
            setup.append(f"INSERT INTO #{name} VALUES ({values});")
    (OUT / "setup.sql").write_text("\n".join(setup), encoding="utf-8")
    (OUT / "queries.json").write_text(json.dumps(QUERIES), encoding="utf-8")

    print(f"{len(QUERIES)} queries written to {OUT}")
    print("next:")
    print(f"  python -m pysqlbridge --config {OUT / 'config.json'} --port 1371")
    print("  pwsh scripts/differential.ps1 -Port 1371")


if __name__ == "__main__":
    main()
