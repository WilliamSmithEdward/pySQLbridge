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

import json
import pathlib

HERE = pathlib.Path(__file__).parent
OUT = HERE / "differential"

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

COLUMNS = {
    "people": ("id", "name", "team", "score", "rank"),
    "tasks": ("tid", "owner", "state", "hours"),
}

TYPES = {
    "people": "id int, name nvarchar(50), team nvarchar(50), score float, rank int",
    "tasks": "tid int, owner int, state nvarchar(50), hours int",
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
]


def _literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return "N'" + value.replace("'", "''") + "'"
    return repr(value)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for name, records in (("people", PEOPLE), ("tasks", TASKS)):
        (OUT / f"{name}.json").write_text(json.dumps(records), encoding="utf-8")

    (OUT / "config.json").write_text(json.dumps({
        "tables": [
            {"name": name, "json": str(OUT / f"{name}.json")}
            for name in COLUMNS
        ]
    }, indent=2), encoding="utf-8")

    setup = [f"CREATE TABLE #{name} ({columns});"
             for name, columns in TYPES.items()]
    for name, records in (("people", PEOPLE), ("tasks", TASKS)):
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
