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
from .tds.result import Bit, Column, Integer, NVarChar

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


# The schema a client reads the server's own state from. One view of it is
# served, because one is asked for before a connection will open.
SYS_PREFIX = "SYS"

# What every text column here is declared with, and what sys.databases
# reports for the database it describes.
COLLATION_NAME = "SQL_Latin1_General_CP1_CI_AS"


def host_info() -> Table:
    """sys.dm_os_host_info, which says what the server is running on.

    SSMS reads host_platform while connecting. The values are this machine's,
    read from Python rather than invented, because a client that asked what
    it reached deserves the answer.
    """
    import platform

    release = platform.release()
    return Table(
        name="dm_os_host_info",
        columns=[
            Column("host_platform", NVarChar(256)),
            Column("host_distribution", NVarChar(256)),
            Column("host_release", NVarChar(256)),
            Column("host_service_pack_level", NVarChar(256)),
            Column("host_sku", Integer(4)),
            Column("os_language_version", Integer(4)),
            Column("host_architecture", NVarChar(256)),
        ],
        rows=[[
            "Windows" if platform.system() == "Windows" else platform.system(),
            f"{platform.system()} {release}".strip(),
            platform.version(),
            "",
            None,
            None,
            platform.machine(),
        ]],
    )


# What sys.databases says about the one database served. Every value is
# either true of this server or the value a real one reports for a database
# that is online and nothing unusual: read-only is true, because that is what
# this is, and a client that hides writes because of it is right to.
DATABASE_STATE = {
    "database_id": 1,
    "state": 0,                     # online
    "compatibility_level": 170,     # what a 17.0 server reports
    "recovery_model": 3,            # simple: nothing here is logged
    "user_access": 0,               # multi user
    "is_read_only": 1,
    "is_in_standby": 0,
    "is_fulltext_enabled": 0,
    "is_distributor": 0,
    "is_published": 0,
    "is_subscribed": 0,
    "containment": 0,
    "source_database_id": None,
}


def databases(name: str) -> Table:
    """sys.databases, holding the one database this serves.

    Object Explorer reads thirteen columns of this to decide what to show
    under Databases, including a status it assembles out of three CASEs and
    two bitwise ors. A server that does not have the view shows nothing
    there, which is what an empty Databases node means.
    """
    columns = [
        Column("name", NVarChar(128)),
        Column("database_id", Integer(4)),
        Column("owner_sid", NVarChar(1)),
        Column("collation_name", NVarChar(128)),
        Column("state", Integer(4)),
        Column("state_desc", NVarChar(60)),
        Column("compatibility_level", Integer(4)),
        Column("recovery_model", Integer(4)),
        Column("recovery_model_desc", NVarChar(60)),
        Column("user_access", Integer(4)),
        Column("user_access_desc", NVarChar(60)),
        Column("is_read_only", Bit()),
        Column("is_in_standby", Bit()),
        Column("is_fulltext_enabled", Bit()),
        Column("is_distributor", Bit()),
        Column("is_published", Bit()),
        Column("is_subscribed", Bit()),
        Column("containment", Integer(4)),
        Column("source_database_id", Integer(4)),
        Column("catalog_collation_type", Integer(4)),
        Column("catalog_collation_type_desc", NVarChar(60)),
    ]
    held = DATABASE_STATE
    return Table(
        name="databases",
        columns=columns,
        rows=[[
            name,
            held["database_id"],
            "",
            COLLATION_NAME,
            held["state"], "ONLINE",
            held["compatibility_level"],
            held["recovery_model"], "SIMPLE",
            held["user_access"], "MULTI_USER",
            bool(held["is_read_only"]),
            bool(held["is_in_standby"]),
            bool(held["is_fulltext_enabled"]),
            bool(held["is_distributor"]),
            bool(held["is_published"]),
            bool(held["is_subscribed"]),
            held["containment"],
            held["source_database_id"],
            0, "DATABASE_DEFAULT",
        ]],
    )


def configurations() -> Table:
    """sys.configurations, of which this server has none.

    Nothing here is configurable, so the view is empty rather than absent: a
    client asking whether a setting is on gets no row, which is the answer,
    where a missing view would be an error about the wrong thing.
    """
    return Table(
        name="configurations",
        columns=[
            Column("configuration_id", Integer(4)),
            Column("name", NVarChar(35)),
            Column("value", Integer(8)),
            Column("minimum", Integer(8)),
            Column("maximum", Integer(8)),
            Column("value_in_use", Integer(8)),
            Column("description", NVarChar(255)),
            Column("is_dynamic", Bit()),
            Column("is_advanced", Bit()),
        ],
        rows=[],
    )


# What is served under dbo besides the tables a configuration names. These
# are read while a client builds its tree, and every one of them says the
# same thing: this server does not do that.
def policy_configuration() -> Table:
    """msdb.dbo.syspolicy_configuration, which says policies are off.

    Rows rather than an empty table, because the client reads each setting
    with a scalar subquery and off is an answer where nothing is not.
    """
    return Table(
        name="syspolicy_configuration",
        columns=[
            Column("name", NVarChar(128)),
            Column("current_value", Integer(4)),
            Column("default_value", Integer(4)),
        ],
        rows=[
            ["Enabled", 0, 0],
            ["HistoryRetentionInDays", 0, 0],
            ["LogOnSuccess", 0, 0],
        ],
    )


def default_schema_views() -> dict[str, Table]:
    """Every system table served under dbo, by lower-case name."""
    return {"syspolicy_configuration": policy_configuration()}


def system_views(database: str = "") -> dict[str, Table]:
    """Every view served under the sys schema, by lower-case name."""
    return {
        "dm_os_host_info": host_info(),
        "databases": databases(database),
        "configurations": configurations(),
    }
