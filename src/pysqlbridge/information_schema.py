"""The catalog views a client browses with.

A table picker does not ask for a list of tables in any direct way. It sends
this, measured from .NET's GetSchema("Tables"):

    select TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE
    from INFORMATION_SCHEMA.TABLES
    where (TABLE_CATALOG = @Catalog or (@Catalog is null))
      and (TABLE_SCHEMA  = @Owner   or (@Owner   is null))
      and (TABLE_NAME    = @Name    or (@Name    is null))
      and (TABLE_TYPE    = @TableType or (@TableType is null))

So the views are ordinary tables and the WHERE does the filtering, which is why
the predicate evaluator had to exist first. Nothing here is special-cased on
the shape of the query.

Column names and their order match SQL Server's, because a client selects them
by name and reads them positionally.
"""

from __future__ import annotations

from .source import Table
from .tds.result import Column, Integer, NVarChar

# What the bridge calls its database and schema. Clients display both, and a
# schema of dbo is what every SQL Server tool expects to see.
DATABASE_NAME = "pysqlbridge"
SCHEMA_NAME = "dbo"

SCHEMA_PREFIX = "INFORMATION_SCHEMA"

# INFORMATION_SCHEMA reports types by their SQL names, not by the TDS type
# bytes the wire uses, so the two vocabularies have to be mapped.
_SQL_TYPE_NAMES = {"Integer": "int", "Float": "float", "NVarChar": "nvarchar"}
_BIGINT_WIDTH = 8


def _sql_type(column: Column) -> tuple[str, int | None]:
    """The SQL type name a client should see, and its length where it has one."""
    kind = type(column.type).__name__
    name = _SQL_TYPE_NAMES.get(kind, "nvarchar")
    if kind == "Integer":
        return ("bigint" if column.type.width == _BIGINT_WIDTH else "int"), None
    if kind == "NVarChar":
        return name, column.type.max_chars
    return name, None


def tables_view(tables: list[Table]) -> Table:
    """INFORMATION_SCHEMA.TABLES, one row per served table."""
    return Table(
        name="TABLES",
        columns=[
            Column("TABLE_CATALOG", NVarChar(128)),
            Column("TABLE_SCHEMA", NVarChar(128)),
            Column("TABLE_NAME", NVarChar(128)),
            Column("TABLE_TYPE", NVarChar(10)),
        ],
        rows=[
            [DATABASE_NAME, SCHEMA_NAME, table.name, "BASE TABLE"]
            for table in sorted(tables, key=lambda t: t.name.lower())
        ],
    )


def columns_view(tables: list[Table]) -> Table:
    """INFORMATION_SCHEMA.COLUMNS, one row per column of every served table."""
    rows: list[list[object]] = []
    for table in sorted(tables, key=lambda t: t.name.lower()):
        for position, column in enumerate(table.columns, start=1):
            type_name, length = _sql_type(column)
            rows.append([
                DATABASE_NAME,
                SCHEMA_NAME,
                table.name,
                column.name,
                position,
                # Every column is nullable: the sources carry no constraints,
                # and claiming NO would be a promise the data does not make.
                "YES",
                type_name,
                length,
            ])

    return Table(
        name="COLUMNS",
        columns=[
            Column("TABLE_CATALOG", NVarChar(128)),
            Column("TABLE_SCHEMA", NVarChar(128)),
            Column("TABLE_NAME", NVarChar(128)),
            Column("COLUMN_NAME", NVarChar(128)),
            Column("ORDINAL_POSITION", Integer(4)),
            Column("IS_NULLABLE", NVarChar(3)),
            Column("DATA_TYPE", NVarChar(128)),
            Column("CHARACTER_MAXIMUM_LENGTH", Integer(4)),
        ],
        rows=rows,
    )


def schemata_view() -> Table:
    """INFORMATION_SCHEMA.SCHEMATA, the one schema this server has."""
    return Table(
        name="SCHEMATA",
        columns=[
            Column("CATALOG_NAME", NVarChar(128)),
            Column("SCHEMA_NAME", NVarChar(128)),
            Column("SCHEMA_OWNER", NVarChar(128)),
        ],
        rows=[[DATABASE_NAME, SCHEMA_NAME, SCHEMA_NAME]],
    )


def build(tables: list[Table]) -> dict[str, Table]:
    """Every catalog view, keyed by its lowercase name."""
    views = [tables_view(tables), columns_view(tables), schemata_view()]
    return {view.name.lower(): view for view in views}
