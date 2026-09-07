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
