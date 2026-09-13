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

import datetime
import zlib

from .source import Table
from .tds.result import (
    Bit,
    Column,
    DateTime,
    Float,
    Integer,
    NVarChar,
    SmallInt,
    UniqueIdentifier,
    VarBinary,
)

# What the bridge calls its database and schema. Clients display both, and a
# schema of dbo is what every SQL Server tool expects to see.
DATABASE_NAME = "pysqlbridge"
SCHEMA_NAME = "dbo"

SCHEMA_PREFIX = "INFORMATION_SCHEMA"

# INFORMATION_SCHEMA reports types by their SQL names, not by the TDS type
# bytes the wire uses, so the two vocabularies have to be mapped.
_SQL_TYPE_NAMES = {"Integer": "int", "UntypedNull": "int", "Float": "float",
                   "NVarChar": "nvarchar", "DateTime": "datetime"}
_BIGINT_WIDTH = 8

# What a real server reports for a datetime in DATETIME_PRECISION. Measured
# on SQL Server 2025: a datetime says 3, and it is the only field describing
# the type that it fills in at all.
_DATETIME_PRECISION = 3


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


# Every column of INFORMATION_SCHEMA.COLUMNS, in the order a real server
# returns them. What each holds for a column of ours is filled in below;
# what a numeric or a text column reports in the fields that describe it was
# read off SQL Server 2025 rather than worked out from the standard.
COLUMNS_VIEW: list[tuple[str, object]] = [
    ("TABLE_CATALOG", NVarChar(128)),
    ("TABLE_SCHEMA", NVarChar(128)),
    ("TABLE_NAME", NVarChar(128)),
    ("COLUMN_NAME", NVarChar(128)),
    ("ORDINAL_POSITION", Integer(4)),
    ("COLUMN_DEFAULT", NVarChar(4000)),
    ("IS_NULLABLE", NVarChar(3)),
    ("DATA_TYPE", NVarChar(128)),
    ("CHARACTER_MAXIMUM_LENGTH", Integer(4)),
    ("CHARACTER_OCTET_LENGTH", Integer(4)),
    ("NUMERIC_PRECISION", Integer(1)),
    ("NUMERIC_PRECISION_RADIX", Integer(2)),
    ("NUMERIC_SCALE", Integer(4)),
    ("DATETIME_PRECISION", Integer(2)),
    ("CHARACTER_SET_CATALOG", NVarChar(128)),
    ("CHARACTER_SET_SCHEMA", NVarChar(128)),
    ("CHARACTER_SET_NAME", NVarChar(128)),
    ("COLLATION_CATALOG", NVarChar(128)),
    ("COLLATION_SCHEMA", NVarChar(128)),
    ("COLLATION_NAME", NVarChar(128)),
    ("DOMAIN_CATALOG", NVarChar(128)),
    ("DOMAIN_SCHEMA", NVarChar(128)),
    ("DOMAIN_NAME", NVarChar(128)),
]

# What a real server reports in the fields that describe a number, by the
# type name it reports for it: the precision, what it counts in, and the
# scale. Measured on SQL Server 2025 over a table of every type: a float
# counts in 2 and a whole number in 10, a float has no scale, and a bit
# fills in none of the three, where this said it was a one-digit number.
# A type not here fills in none of them either.
_NUMERIC = {
    "int": (10, 10, 0), "bigint": (19, 10, 0), "smallint": (5, 10, 0),
    "tinyint": (3, 10, 0), "float": (53, 2, None), "real": (24, 2, None),
    "decimal": (18, 10, 0), "numeric": (18, 10, 0), "money": (19, 10, 4),
}


def columns_view(tables: list[Table]) -> Table:
    """INFORMATION_SCHEMA.COLUMNS, one row per column of every served table."""
    rows: list[list[object]] = []
    for table in sorted(tables, key=lambda t: t.name.lower()):
        for position, column in enumerate(table.columns, start=1):
            type_name, length = _sql_type(column)
            precision, radix, scale = _NUMERIC.get(type_name,
                                                   (None, None, None))
            text = length is not None
            rows.append([
                DATABASE_NAME, SCHEMA_NAME, table.name, column.name, position,
                None,                                     # no defaults here
                # Every column is nullable: the sources carry no constraints,
                # and claiming NO would be a promise the data does not make.
                "YES",
                type_name,
                length,
                # An octet is a byte, and text here is two bytes a character.
                length * 2 if text else None,
                precision,
                radix,
                scale,
                _DATETIME_PRECISION if type_name == "datetime" else None,
                None, None,
                "UNICODE" if text else None,
                None, None,
                COLLATION_NAME if text else None,
                None, None, None,
            ])

    return Table(
        name="COLUMNS",
        columns=[Column(one, kind) for one, kind in COLUMNS_VIEW],
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
            Column("DEFAULT_CHARACTER_SET_CATALOG", NVarChar(128)),
            Column("DEFAULT_CHARACTER_SET_SCHEMA", NVarChar(128)),
            Column("DEFAULT_CHARACTER_SET_NAME", NVarChar(128)),
        ],
        rows=[[DATABASE_NAME, SCHEMA_NAME, SCHEMA_NAME, None, None, "iso_1"]],
    )


# The rest of INFORMATION_SCHEMA, at the shape a real server has and with
# nothing in it. There are no routines here, no constraints, no privileges
# and no domains, and no row is what says so. A view that is missing says
# something else, and a client asking about routines gets an error about a
# name it did not choose instead of an empty list.
EMPTY_VIEWS: dict[str, list[tuple[str, object]]] = {
    "CHECK_CONSTRAINTS": [
        ("CONSTRAINT_CATALOG", NVarChar(128)),
        ("CONSTRAINT_SCHEMA", NVarChar(128)),
        ("CONSTRAINT_NAME", NVarChar(128)),
        ("CHECK_CLAUSE", NVarChar(4000)),
    ],
    "COLUMN_DOMAIN_USAGE": [
        ("DOMAIN_CATALOG", NVarChar(128)),
        ("DOMAIN_SCHEMA", NVarChar(128)),
        ("DOMAIN_NAME", NVarChar(128)),
        ("TABLE_CATALOG", NVarChar(128)),
        ("TABLE_SCHEMA", NVarChar(128)),
        ("TABLE_NAME", NVarChar(128)),
        ("COLUMN_NAME", NVarChar(128)),
    ],
    "COLUMN_PRIVILEGES": [
        ("GRANTOR", NVarChar(128)),
        ("GRANTEE", NVarChar(128)),
        ("TABLE_CATALOG", NVarChar(128)),
        ("TABLE_SCHEMA", NVarChar(128)),
        ("TABLE_NAME", NVarChar(128)),
        ("COLUMN_NAME", NVarChar(128)),
        ("PRIVILEGE_TYPE", NVarChar(10)),
        ("IS_GRANTABLE", NVarChar(3)),
    ],
    "CONSTRAINT_COLUMN_USAGE": [
        ("TABLE_CATALOG", NVarChar(128)),
        ("TABLE_SCHEMA", NVarChar(128)),
        ("TABLE_NAME", NVarChar(128)),
        ("COLUMN_NAME", NVarChar(128)),
        ("CONSTRAINT_CATALOG", NVarChar(128)),
        ("CONSTRAINT_SCHEMA", NVarChar(128)),
        ("CONSTRAINT_NAME", NVarChar(128)),
    ],
    "CONSTRAINT_TABLE_USAGE": [
        ("TABLE_CATALOG", NVarChar(128)),
        ("TABLE_SCHEMA", NVarChar(128)),
        ("TABLE_NAME", NVarChar(128)),
        ("CONSTRAINT_CATALOG", NVarChar(128)),
        ("CONSTRAINT_SCHEMA", NVarChar(128)),
        ("CONSTRAINT_NAME", NVarChar(128)),
    ],
    "DOMAINS": [
        ("DOMAIN_CATALOG", NVarChar(128)),
        ("DOMAIN_SCHEMA", NVarChar(128)),
        ("DOMAIN_NAME", NVarChar(128)),
        ("DATA_TYPE", NVarChar(128)),
        ("CHARACTER_MAXIMUM_LENGTH", Integer(4)),
        ("CHARACTER_OCTET_LENGTH", Integer(4)),
        ("COLLATION_CATALOG", NVarChar(128)),
        ("COLLATION_SCHEMA", NVarChar(128)),
        ("COLLATION_NAME", NVarChar(128)),
        ("CHARACTER_SET_CATALOG", NVarChar(128)),
        ("CHARACTER_SET_SCHEMA", NVarChar(128)),
        ("CHARACTER_SET_NAME", NVarChar(128)),
        ("NUMERIC_PRECISION", Integer(1)),
        ("NUMERIC_PRECISION_RADIX", Integer(2)),
        ("NUMERIC_SCALE", Integer(4)),
        ("DATETIME_PRECISION", Integer(2)),
        ("DOMAIN_DEFAULT", NVarChar(4000)),
    ],
    "DOMAIN_CONSTRAINTS": [
        ("CONSTRAINT_CATALOG", NVarChar(128)),
        ("CONSTRAINT_SCHEMA", NVarChar(128)),
        ("CONSTRAINT_NAME", NVarChar(128)),
        ("DOMAIN_CATALOG", NVarChar(128)),
        ("DOMAIN_SCHEMA", NVarChar(128)),
        ("DOMAIN_NAME", NVarChar(128)),
        ("IS_DEFERRABLE", NVarChar(2)),
        ("INITIALLY_DEFERRED", NVarChar(2)),
    ],
    "KEY_COLUMN_USAGE": [
        ("CONSTRAINT_CATALOG", NVarChar(128)),
        ("CONSTRAINT_SCHEMA", NVarChar(128)),
        ("CONSTRAINT_NAME", NVarChar(128)),
        ("TABLE_CATALOG", NVarChar(128)),
        ("TABLE_SCHEMA", NVarChar(128)),
        ("TABLE_NAME", NVarChar(128)),
        ("COLUMN_NAME", NVarChar(128)),
        ("ORDINAL_POSITION", Integer(4)),
    ],
    "PARAMETERS": [
        ("SPECIFIC_CATALOG", NVarChar(128)),
        ("SPECIFIC_SCHEMA", NVarChar(128)),
        ("SPECIFIC_NAME", NVarChar(128)),
        ("ORDINAL_POSITION", Integer(4)),
        ("PARAMETER_MODE", NVarChar(10)),
        ("IS_RESULT", NVarChar(10)),
        ("AS_LOCATOR", NVarChar(10)),
        ("PARAMETER_NAME", NVarChar(128)),
        ("DATA_TYPE", NVarChar(128)),
        ("CHARACTER_MAXIMUM_LENGTH", Integer(4)),
        ("CHARACTER_OCTET_LENGTH", Integer(4)),
        ("COLLATION_CATALOG", NVarChar(128)),
        ("COLLATION_SCHEMA", NVarChar(128)),
        ("COLLATION_NAME", NVarChar(128)),
        ("CHARACTER_SET_CATALOG", NVarChar(128)),
        ("CHARACTER_SET_SCHEMA", NVarChar(128)),
        ("CHARACTER_SET_NAME", NVarChar(128)),
        ("NUMERIC_PRECISION", Integer(1)),
        ("NUMERIC_PRECISION_RADIX", Integer(2)),
        ("NUMERIC_SCALE", Integer(4)),
        ("DATETIME_PRECISION", Integer(2)),
        ("INTERVAL_TYPE", NVarChar(30)),
        ("INTERVAL_PRECISION", Integer(2)),
        ("USER_DEFINED_TYPE_CATALOG", NVarChar(128)),
        ("USER_DEFINED_TYPE_SCHEMA", NVarChar(128)),
        ("USER_DEFINED_TYPE_NAME", NVarChar(128)),
        ("SCOPE_CATALOG", NVarChar(128)),
        ("SCOPE_SCHEMA", NVarChar(128)),
        ("SCOPE_NAME", NVarChar(128)),
    ],
    "REFERENTIAL_CONSTRAINTS": [
        ("CONSTRAINT_CATALOG", NVarChar(128)),
        ("CONSTRAINT_SCHEMA", NVarChar(128)),
        ("CONSTRAINT_NAME", NVarChar(128)),
        ("UNIQUE_CONSTRAINT_CATALOG", NVarChar(128)),
        ("UNIQUE_CONSTRAINT_SCHEMA", NVarChar(128)),
        ("UNIQUE_CONSTRAINT_NAME", NVarChar(128)),
        ("MATCH_OPTION", NVarChar(7)),
        ("UPDATE_RULE", NVarChar(11)),
        ("DELETE_RULE", NVarChar(11)),
    ],
    "ROUTINES": [
        ("SPECIFIC_CATALOG", NVarChar(128)),
        ("SPECIFIC_SCHEMA", NVarChar(128)),
        ("SPECIFIC_NAME", NVarChar(128)),
        ("ROUTINE_CATALOG", NVarChar(128)),
        ("ROUTINE_SCHEMA", NVarChar(128)),
        ("ROUTINE_NAME", NVarChar(128)),
        ("ROUTINE_TYPE", NVarChar(20)),
        ("MODULE_CATALOG", NVarChar(128)),
        ("MODULE_SCHEMA", NVarChar(128)),
        ("MODULE_NAME", NVarChar(128)),
        ("UDT_CATALOG", NVarChar(128)),
        ("UDT_SCHEMA", NVarChar(128)),
        ("UDT_NAME", NVarChar(128)),
        ("DATA_TYPE", NVarChar(128)),
        ("CHARACTER_MAXIMUM_LENGTH", Integer(4)),
        ("CHARACTER_OCTET_LENGTH", Integer(4)),
        ("COLLATION_CATALOG", NVarChar(128)),
        ("COLLATION_SCHEMA", NVarChar(128)),
        ("COLLATION_NAME", NVarChar(128)),
        ("CHARACTER_SET_CATALOG", NVarChar(128)),
        ("CHARACTER_SET_SCHEMA", NVarChar(128)),
        ("CHARACTER_SET_NAME", NVarChar(128)),
        ("NUMERIC_PRECISION", Integer(1)),
        ("NUMERIC_PRECISION_RADIX", Integer(2)),
        ("NUMERIC_SCALE", Integer(4)),
        ("DATETIME_PRECISION", Integer(2)),
        ("INTERVAL_TYPE", NVarChar(30)),
        ("INTERVAL_PRECISION", Integer(2)),
        ("TYPE_UDT_CATALOG", NVarChar(128)),
        ("TYPE_UDT_SCHEMA", NVarChar(128)),
        ("TYPE_UDT_NAME", NVarChar(128)),
        ("SCOPE_CATALOG", NVarChar(128)),
        ("SCOPE_SCHEMA", NVarChar(128)),
        ("SCOPE_NAME", NVarChar(128)),
        ("MAXIMUM_CARDINALITY", Integer(8)),
        ("DTD_IDENTIFIER", NVarChar(128)),
        ("ROUTINE_BODY", NVarChar(30)),
        ("ROUTINE_DEFINITION", NVarChar(4000)),
        ("EXTERNAL_NAME", NVarChar(128)),
        ("EXTERNAL_LANGUAGE", NVarChar(30)),
        ("PARAMETER_STYLE", NVarChar(30)),
        ("IS_DETERMINISTIC", NVarChar(10)),
        ("SQL_DATA_ACCESS", NVarChar(30)),
        ("IS_NULL_CALL", NVarChar(10)),
        ("SQL_PATH", NVarChar(128)),
        ("SCHEMA_LEVEL_ROUTINE", NVarChar(10)),
        ("MAX_DYNAMIC_RESULT_SETS", Integer(2)),
        ("IS_USER_DEFINED_CAST", NVarChar(10)),
        ("IS_IMPLICITLY_INVOCABLE", NVarChar(10)),
        ("CREATED", DateTime()),
        ("LAST_ALTERED", DateTime()),
    ],
    "ROUTINE_COLUMNS": [
        ("TABLE_CATALOG", NVarChar(128)),
        ("TABLE_SCHEMA", NVarChar(128)),
        ("TABLE_NAME", NVarChar(128)),
        ("COLUMN_NAME", NVarChar(128)),
        ("ORDINAL_POSITION", Integer(4)),
        ("COLUMN_DEFAULT", NVarChar(4000)),
        ("IS_NULLABLE", NVarChar(3)),
        ("DATA_TYPE", NVarChar(128)),
        ("CHARACTER_MAXIMUM_LENGTH", Integer(4)),
        ("CHARACTER_OCTET_LENGTH", Integer(4)),
        ("NUMERIC_PRECISION", Integer(1)),
        ("NUMERIC_PRECISION_RADIX", Integer(2)),
        ("NUMERIC_SCALE", Integer(4)),
        ("DATETIME_PRECISION", Integer(2)),
        ("CHARACTER_SET_CATALOG", NVarChar(128)),
        ("CHARACTER_SET_SCHEMA", NVarChar(128)),
        ("CHARACTER_SET_NAME", NVarChar(128)),
        ("COLLATION_CATALOG", NVarChar(128)),
        ("COLLATION_SCHEMA", NVarChar(128)),
        ("COLLATION_NAME", NVarChar(128)),
        ("DOMAIN_CATALOG", NVarChar(128)),
        ("DOMAIN_SCHEMA", NVarChar(128)),
        ("DOMAIN_NAME", NVarChar(128)),
    ],
    "SEQUENCES": [
        ("SEQUENCE_CATALOG", NVarChar(128)),
        ("SEQUENCE_SCHEMA", NVarChar(128)),
        ("SEQUENCE_NAME", NVarChar(128)),
        ("DATA_TYPE", NVarChar(128)),
        ("NUMERIC_PRECISION", Integer(1)),
        ("NUMERIC_PRECISION_RADIX", Integer(2)),
        ("NUMERIC_SCALE", Integer(4)),
        ("START_VALUE", NVarChar(4000)),
        ("MINIMUM_VALUE", NVarChar(4000)),
        ("MAXIMUM_VALUE", NVarChar(4000)),
        ("INCREMENT", NVarChar(4000)),
        ("CYCLE_OPTION", Bit()),
        ("DECLARED_DATA_TYPE", NVarChar(128)),
        ("DECLARED_NUMERIC_PRECISION", Integer(1)),
        ("DECLARED_NUMERIC_SCALE", Integer(1)),
    ],
    "TABLE_CONSTRAINTS": [
        ("CONSTRAINT_CATALOG", NVarChar(128)),
        ("CONSTRAINT_SCHEMA", NVarChar(128)),
        ("CONSTRAINT_NAME", NVarChar(128)),
        ("TABLE_CATALOG", NVarChar(128)),
        ("TABLE_SCHEMA", NVarChar(128)),
        ("TABLE_NAME", NVarChar(128)),
        ("CONSTRAINT_TYPE", NVarChar(11)),
        ("IS_DEFERRABLE", NVarChar(2)),
        ("INITIALLY_DEFERRED", NVarChar(2)),
    ],
    "TABLE_PRIVILEGES": [
        ("GRANTOR", NVarChar(128)),
        ("GRANTEE", NVarChar(128)),
        ("TABLE_CATALOG", NVarChar(128)),
        ("TABLE_SCHEMA", NVarChar(128)),
        ("TABLE_NAME", NVarChar(128)),
        ("PRIVILEGE_TYPE", NVarChar(10)),
        ("IS_GRANTABLE", NVarChar(3)),
    ],
    "VIEWS": [
        ("TABLE_CATALOG", NVarChar(128)),
        ("TABLE_SCHEMA", NVarChar(128)),
        ("TABLE_NAME", NVarChar(128)),
        ("VIEW_DEFINITION", NVarChar(4000)),
        ("CHECK_OPTION", NVarChar(7)),
        ("IS_UPDATABLE", NVarChar(2)),
    ],
    "VIEW_COLUMN_USAGE": [
        ("VIEW_CATALOG", NVarChar(128)),
        ("VIEW_SCHEMA", NVarChar(128)),
        ("VIEW_NAME", NVarChar(128)),
        ("TABLE_CATALOG", NVarChar(128)),
        ("TABLE_SCHEMA", NVarChar(128)),
        ("TABLE_NAME", NVarChar(128)),
        ("COLUMN_NAME", NVarChar(128)),
    ],
    "VIEW_TABLE_USAGE": [
        ("VIEW_CATALOG", NVarChar(128)),
        ("VIEW_SCHEMA", NVarChar(128)),
        ("VIEW_NAME", NVarChar(128)),
        ("TABLE_CATALOG", NVarChar(128)),
        ("TABLE_SCHEMA", NVarChar(128)),
        ("TABLE_NAME", NVarChar(128)),
    ],
}


def build(tables: list[Table]) -> dict[str, Table]:
    """Every catalog view, keyed by its lowercase name."""
    views = [tables_view(tables), columns_view(tables), schemata_view()]
    views.extend(
        Table(name=name, columns=[Column(one, kind) for one, kind in columns],
              rows=[])
        for name, columns in EMPTY_VIEWS.items()
    )
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


# The one database this serves, which every view that mentions a database
# names.
DATABASE_ID = 1

# When this server started, and so how long its database has existed. A real
# server has a creation date and a client shows it; this one has no history
# older than the process.
STARTED = datetime.datetime.now()

# Every column of sys.databases, in the order a real server returns them,
# with what this server says in each. Read off SQL Server 2025 rather than
# from the documentation, because a client reads some of these by name and
# some by position, and a column that is missing is an error rather than a
# null. The values are either true of this server or what a real one reports
# for a database that is online and unremarkable, taken from model where
# master is peculiar: read-only is true, because that is what this is, and a
# client that hides writes because of it is right to. Recovery is simple
# because nothing here is logged.
DATABASE_COLUMNS: list[tuple[str, object, object]] = [
    ("name", NVarChar(128), ""),
    ("database_id", Integer(4), DATABASE_ID),
    ("source_database_id", Integer(4), None),
    ("owner_sid", VarBinary(85), b""),
    ("create_date", DateTime(), STARTED),
    ("compatibility_level", Integer(1), 170),
    ("collation_name", NVarChar(128), COLLATION_NAME),
    ("user_access", Integer(1), 0),
    ("user_access_desc", NVarChar(60), "MULTI_USER"),
    ("is_read_only", Bit(), True),
    ("is_auto_close_on", Bit(), False),
    ("is_auto_shrink_on", Bit(), False),
    ("state", Integer(1), 0),
    ("state_desc", NVarChar(60), "ONLINE"),
    ("is_in_standby", Bit(), False),
    ("is_cleanly_shutdown", Bit(), False),
    ("is_supplemental_logging_enabled", Bit(), False),
    ("snapshot_isolation_state", Integer(1), 0),
    ("snapshot_isolation_state_desc", NVarChar(60), "OFF"),
    ("is_read_committed_snapshot_on", Bit(), False),
    ("recovery_model", Integer(1), 3),
    ("recovery_model_desc", NVarChar(60), "SIMPLE"),
    ("page_verify_option", Integer(1), 2),
    ("page_verify_option_desc", NVarChar(60), "CHECKSUM"),
    ("is_auto_create_stats_on", Bit(), True),
    ("is_auto_create_stats_incremental_on", Bit(), False),
    ("is_auto_update_stats_on", Bit(), True),
    ("is_auto_update_stats_async_on", Bit(), False),
    ("is_ansi_null_default_on", Bit(), False),
    ("is_ansi_nulls_on", Bit(), False),
    ("is_ansi_padding_on", Bit(), False),
    ("is_ansi_warnings_on", Bit(), False),
    ("is_arithabort_on", Bit(), False),
    ("is_concat_null_yields_null_on", Bit(), False),
    ("is_numeric_roundabort_on", Bit(), False),
    ("is_quoted_identifier_on", Bit(), False),
    ("is_recursive_triggers_on", Bit(), False),
    ("is_cursor_close_on_commit_on", Bit(), False),
    ("is_local_cursor_default", Bit(), False),
    ("is_fulltext_enabled", Bit(), False),
    ("is_trustworthy_on", Bit(), False),
    ("is_db_chaining_on", Bit(), False),
    ("is_parameterization_forced", Bit(), False),
    ("is_master_key_encrypted_by_server", Bit(), False),
    ("is_query_store_on", Bit(), False),
    ("is_published", Bit(), False),
    ("is_subscribed", Bit(), False),
    ("is_merge_published", Bit(), False),
    ("is_distributor", Bit(), False),
    ("is_sync_with_backup", Bit(), False),
    ("service_broker_guid", UniqueIdentifier(), "00000000-0000-0000-0000-000000000000"),
    ("is_broker_enabled", Bit(), False),
    ("log_reuse_wait", Integer(1), 0),
    ("log_reuse_wait_desc", NVarChar(60), "NOTHING"),
    ("is_date_correlation_on", Bit(), False),
    ("is_cdc_enabled", Bit(), False),
    ("is_encrypted", Bit(), False),
    ("is_honor_broker_priority_on", Bit(), False),
    ("replica_id", UniqueIdentifier(), None),
    ("group_database_id", UniqueIdentifier(), None),
    ("resource_pool_id", Integer(4), None),
    ("default_language_lcid", Integer(2), None),
    ("default_language_name", NVarChar(128), None),
    ("default_fulltext_language_lcid", Integer(4), None),
    ("default_fulltext_language_name", NVarChar(128), None),
    ("is_nested_triggers_on", Bit(), None),
    ("is_transform_noise_words_on", Bit(), None),
    ("two_digit_year_cutoff", Integer(2), None),
    ("containment", Integer(1), 0),
    ("containment_desc", NVarChar(60), "NONE"),
    ("target_recovery_time_in_seconds", Integer(4), 0),
    ("delayed_durability", Integer(4), 0),
    ("delayed_durability_desc", NVarChar(60), "DISABLED"),
    ("is_memory_optimized_elevate_to_snapshot_on", Bit(), False),
    ("is_federation_member", Bit(), False),
    ("is_remote_data_archive_enabled", Bit(), False),
    ("is_mixed_page_allocation_on", Bit(), True),
    ("is_temporal_history_retention_enabled", Bit(), True),
    ("catalog_collation_type", Integer(4), 0),
    ("catalog_collation_type_desc", NVarChar(60), "DATABASE_DEFAULT"),
    ("physical_database_name", NVarChar(128), ""),
    ("is_result_set_caching_on", Bit(), False),
    ("is_accelerated_database_recovery_on", Bit(), False),
    ("is_tempdb_spill_to_remote_store", Bit(), False),
    ("is_stale_page_detection_on", Bit(), False),
    ("is_memory_optimized_enabled", Bit(), True),
    ("is_data_retention_enabled", Bit(), False),
    ("is_ledger_on", Bit(), False),
    ("is_change_feed_enabled", Bit(), False),
    ("is_data_lake_replication_enabled", Bit(), False),
    ("is_event_stream_enabled", Bit(), False),
    ("data_compaction", Integer(1), 0),
    ("data_compaction_desc", NVarChar(60), "UNSUPPORTED"),
    ("data_lake_log_publishing", Integer(1), 0),
    ("data_lake_log_publishing_desc", NVarChar(60), "UNSUPPORTED"),
    ("is_vorder_enabled", Bit(), False),
    ("is_proactive_statistics_refresh_on", Bit(), False),
    ("is_optimized_locking_on", Bit(), False),
]


def _built(name: str, columns: list, said: list) -> Table:
    """A view laid out by its columns, each row saying only what differs.

    The columns carry what a real server reports for something unremarkable,
    so a row states the handful of values that are about the thing itself and
    inherits the rest. Forty-eight values in the right order is a mistake
    waiting to be made; eight named ones is not.
    """
    return Table(
        name=name,
        columns=[Column(one, kind) for one, kind, _ in columns],
        rows=[[row.get(one, held) for one, _, held in columns] for row in said],
    )


def object_id(name: str) -> int:
    """The number this server gives a table, made from its name.

    The same name gives the same number every time, so OBJECT_ID(), the
    catalog views and whatever a client remembers between queries all agree.
    """
    return 1000 + (zlib.crc32(name.lower().encode("utf-8")) % 1_000_000)


def databases(name: str) -> Table:
    """sys.databases, holding the one database this serves.

    Object Explorer reads sixteen columns of this to decide what to show
    under Databases, including a status it assembles out of three CASEs and
    two bitwise ors, and the properties of a database read most of the rest.
    A server that does not have the view shows nothing there, which is what
    an empty Databases node means.
    """
    # The two that are this database's own name rather than a default.
    said = {"name": name, "physical_database_name": name}
    return Table(
        name="databases",
        columns=[Column(one, kind) for one, kind, _ in DATABASE_COLUMNS],
        rows=[[said.get(one, held) for one, _, held in DATABASE_COLUMNS]],
    )


def database_mirroring() -> Table:
    """sys.database_mirroring, one row per database, mirroring nothing.

    Object Explorer joins this to sys.databases to show a mirroring role and
    state beside each database, and a LEFT JOIN to a view that is not there
    is an error rather than a null. Every mirroring column is null, which is
    what a real server holds for a database that is not mirrored, and the
    client reads them through ISNULL and shows the database as unmirrored.
    """
    return Table(
        name="database_mirroring",
        columns=[
            Column("database_id", Integer(4)),
            Column("mirroring_guid", UniqueIdentifier()),
            Column("mirroring_state", Integer(1)),
            Column("mirroring_state_desc", NVarChar(60)),
            Column("mirroring_role", Integer(1)),
            Column("mirroring_role_desc", NVarChar(60)),
            Column("mirroring_role_sequence", Integer(4)),
            Column("mirroring_safety_level", Integer(1)),
            Column("mirroring_safety_level_desc", NVarChar(60)),
            Column("mirroring_safety_sequence", Integer(4)),
            Column("mirroring_partner_name", NVarChar(128)),
            Column("mirroring_partner_instance", NVarChar(128)),
            Column("mirroring_witness_name", NVarChar(128)),
            Column("mirroring_witness_state", Integer(1)),
            Column("mirroring_witness_state_desc", NVarChar(60)),
            Column("mirroring_failover_lsn", Float(8)),
            Column("mirroring_connection_timeout", Integer(4)),
            Column("mirroring_redo_queue", Integer(4)),
            Column("mirroring_redo_queue_type", NVarChar(60)),
            Column("mirroring_end_of_log_lsn", Float(8)),
            Column("mirroring_replication_lsn", Float(8)),
        ],
        rows=[[DATABASE_ID] + [None] * 20],
    )


# Every setting a real server reports, at the value a default install has,
# read off SQL Server 2025. Nothing here is configurable and none of these
# can be changed, but a client does not ask whether a setting can be changed:
# it asks what the setting is, and reads the answer as a number.
#
# Serving no rows was the same mistake as answering NULL: a client that asks
# whether Agent XPs are enabled and is told nothing does not conclude they
# are off, it goes and finds out, and finding out means reaching for the
# service on the machine and waiting for that to fail.
CONFIGURATIONS: list[tuple] = [
    (101, "recovery interval (min)", 0, 0, 32767, 0, "Maximum recovery interval in minutes", True, True),
    (102, "allow updates", 0, 0, 1, 0, "Allow updates to system tables", True, False),
    (103, "user connections", 0, 0, 32767, 0, "Number of user connections allowed", False, True),
    (106, "locks", 0, 5000, 2147483647, 0, "Number of locks for all users", False, True),
    (107, "open objects", 0, 0, 2147483647, 0, "Number of open database objects", False, True),
    (109, "fill factor (%)", 0, 0, 100, 0, "Default fill factor percentage", False, True),
    (114, "disallow results from triggers", 0, 0, 1, 0, "Disallow returning results from triggers", True, True),
    (115, "nested triggers", 1, 0, 1, 1, "Allow triggers to be invoked within triggers", True, False),
    (116, "server trigger recursion", 1, 0, 1, 1, "Allow recursion for server level triggers", True, False),
    (117, "remote access", 1, 0, 1, 1, "Allow remote access", False, False),
    (124, "default language", 0, 0, 9999, 0, "default language", True, False),
    (400, "cross db ownership chaining", 0, 0, 1, 0, "Allow cross db ownership chaining", True, False),
    (503, "max worker threads", 0, 128, 65535, 0, "Maximum worker threads", True, True),
    (505, "network packet size (B)", 4096, 512, 32767, 4096, "Network packet size", True, True),
    (518, "show advanced options", 0, 0, 1, 0, "show advanced options", True, False),
    (542, "remote proc trans", 0, 0, 1, 0, "Create DTC transaction for remote procedures", True, False),
    (544, "c2 audit mode", 0, 0, 1, 0, "c2 audit mode", False, True),
    (1126, "default full-text language", 1033, 0, 2147483647, 1033, "default full-text language", True, True),
    (1127, "two digit year cutoff", 2049, 1753, 9999, 2049, "two digit year cutoff", True, True),
    (1505, "index create memory (KB)", 0, 704, 2147483647, 0, "Memory for index create sorts (kBytes)", True, True),
    (1517, "priority boost", 0, 0, 1, 0, "Priority boost", False, True),
    (1519, "remote login timeout (s)", 10, 0, 2147483647, 10, "remote login timeout", True, False),
    (1520, "remote query timeout (s)", 600, 0, 2147483647, 600, "remote query timeout", True, False),
    (1531, "cursor threshold", -1, -1, 2147483647, -1, "cursor threshold", True, True),
    (1532, "set working set size", 0, 0, 1, 0, "set working set size", False, True),
    (1534, "user options", 0, 0, 32767, 0, "user options", True, False),
    (1535, "affinity mask", 0, -2147483648, 2147483647, 0, "affinity mask", True, True),
    (1536, "max text repl size (B)", 65536, -1, 2147483647, 65536, "Maximum size of a text field in replication.", True, False),
    (1537, "media retention", 0, 0, 365, 0, "Tape retention period in days", True, True),
    (1538, "cost threshold for parallelism", 5, 0, 32767, 5, "cost threshold for parallelism", True, True),
    (1539, "max degree of parallelism", 8, 0, 32767, 8, "maximum degree of parallelism", True, True),
    (1540, "min memory per query (KB)", 1024, 512, 2147483647, 1024, "minimum memory per query (kBytes)", True, True),
    (1541, "query wait (s)", -1, -1, 2147483647, -1, "maximum time to wait for query memory (s)", True, True),
    (1543, "min server memory (MB)", 0, 0, 2147483647, 16, "Minimum size of server memory (MB)", True, True),
    (1544, "max server memory (MB)", 2147483647, 128, 2147483647, 2147483647, "Maximum size of server memory (MB)", True, True),
    (1545, "query governor cost limit", 0, 0, 2147483647, 0, "Maximum estimated cost allowed by query governor", True, True),
    (1546, "lightweight pooling", 0, 0, 1, 0, "User mode scheduler uses lightweight pooling", False, True),
    (1547, "scan for startup procs", 0, 0, 1, 0, "scan for startup stored procedures", False, True),
    (1549, "affinity64 mask", 0, -2147483648, 2147483647, 0, "affinity64 mask", True, True),
    (1550, "affinity I/O mask", 0, -2147483648, 2147483647, 0, "affinity I/O mask", False, True),
    (1551, "affinity64 I/O mask", 0, -2147483648, 2147483647, 0, "affinity64 I/O mask", False, True),
    (1555, "transform noise words", 0, 0, 1, 0, "Transform noise words for full-text query", True, True),
    (1556, "precompute rank", 0, 0, 1, 0, "Use precomputed rank for full-text query", True, True),
    (1557, "PH timeout (s)", 60, 1, 3600, 60, "DB connection timeout for full-text protocol handler (s)", True, True),
    (1562, "clr enabled", 0, 0, 1, 0, "CLR user code execution enabled in the server", True, False),
    (1563, "max full-text crawl range", 4, 0, 256, 4, "Maximum  crawl ranges allowed in full-text indexing", True, True),
    (1564, "ft notify bandwidth (min)", 0, 0, 32767, 0, "Number of reserved full-text notifications buffers", True, True),
    (1565, "ft notify bandwidth (max)", 100, 0, 32767, 100, "Max number of full-text notifications buffers", True, True),
    (1566, "ft crawl bandwidth (min)", 0, 0, 32767, 0, "Number of reserved full-text crawl buffers", True, True),
    (1567, "ft crawl bandwidth (max)", 100, 0, 32767, 100, "Max number of full-text crawl buffers", True, True),
    (1568, "default trace enabled", 1, 0, 1, 1, "Enable or disable the default trace", True, True),
    (1569, "blocked process threshold (s)", 0, 0, 86400, 0, "Blocked process reporting threshold", True, True),
    (1570, "in-doubt xact resolution", 0, 0, 2, 0, "Recovery policy for DTC transactions with unknown outcome", True, True),
    (1576, "remote admin connections", 0, 0, 1, 0, "Dedicated Admin Connections are allowed from remote clients", True, False),
    (1578, "EKM provider enabled", 0, 0, 1, 0, "Enable or disable EKM provider", True, True),
    (1579, "backup compression default", 0, 0, 1, 0, "Enable compression of backups by default", True, False),
    (1580, "filestream access level", 0, 0, 2, 0, "Sets the FILESTREAM access level", True, False),
    (1581, "optimize for ad hoc workloads", 0, 0, 1, 0, "When this option is set, plan cache size is further reduced for single-use adhoc OLTP workload.", True, True),
    (1582, "access check cache bucket count", 0, 0, 65536, 0, "Default hash bucket count for the access check result security cache", True, True),
    (1583, "access check cache quota", 0, 0, 2147483647, 0, "Default quota for the access check result security cache", True, True),
    (1584, "backup checksum default", 0, 0, 1, 0, "Enable checksum of backups by default", True, False),
    (1585, "automatic soft-NUMA disabled", 0, 0, 1, 0, "Automatic soft-NUMA is enabled by default", False, True),
    (1586, "external scripts enabled", 0, 0, 1, 0, "Allows execution of external scripts", True, False),
    (1587, "clr strict security", 1, 0, 1, 1, "CLR strict security enabled in the server", True, True),
    (1588, "column encryption enclave type", 0, 0, 2, 0, "Type of enclave used for computations on encrypted columns", False, False),
    (1589, "tempdb metadata memory-optimized", 0, 0, 1, 0, "Tempdb metadata memory-optimized is disabled by default.", False, True),
    (1591, "ADR cleaner retry timeout (min)", 15, 0, 32767, 15, "ADR cleaner retry timeout.", True, True),
    (1592, "ADR Preallocation Factor", 4, 0, 32767, 4, "ADR Preallocation Factor.", True, True),
    (1593, "version high part of SQL Server", 1114112, -2147483648, 2147483647, 1114112, "version high part of SQL Server that model database copied for", True, True),
    (1594, "version low part of SQL Server", 65536007, -2147483648, 2147483647, 65536007, "version low part of SQL Server that model database copied for", True, True),
    (1595, "Data processed daily limit in TB", 2147483647, 0, 2147483647, 2147483647, "SQL On-demand data processed daily limit in TB", True, False),
    (1596, "Data processed weekly limit in TB", 2147483647, 0, 2147483647, 2147483647, "SQL On-demand data processed weekly limit in TB", True, False),
    (1597, "Data processed monthly limit in TB", 2147483647, 0, 2147483647, 2147483647, "SQL On-demand data processed monthly limit in TB", True, False),
    (1598, "ADR Cleaner Thread Count", 1, 1, 32767, 1, "Max number of threads ADR cleaner can assign.", True, True),
    (1599, "hardware offload enabled", 0, 0, 1, 0, "Enable hardware offloading on the server", False, True),
    (1600, "hardware offload config", 0, 0, 255, 0, "Configure hardware offload accelerator", False, True),
    (1601, "hardware offload mode", 0, 0, 255, 0, "Configure hardware offload accelerator mode", False, True),
    (1602, "backup compression algorithm", 0, 0, 3, 0, "Configure default backup compression algorithm", True, False),
    (1603, "ADR cleaner lock timeout (s)", 5, 1, 32767, 5, "ADR cleaner lock timeout", True, True),
    (1606, "SLOG memory quota (%)", 75, 1, 100, 75, "SLOG memory quota percentage", True, True),
    (1609, "max RPC request params (KB)", 0, 0, 2147483647, 0, "Maximum memory for RPC request parameters (kBytes)", True, True),
    (16384, "Agent XPs", 0, 0, 1, 0, "Enable or disable Agent XPs", True, True),
    (16386, "Database Mail XPs", 0, 0, 1, 0, "Enable or disable Database Mail XPs", True, True),
    (16387, "SMO and DMO XPs", 1, 0, 1, 1, "Enable or disable SMO and DMO XPs", True, True),
    (16388, "Ole Automation Procedures", 0, 0, 1, 0, "Enable or disable Ole Automation Procedures", True, True),
    (16390, "xp_cmdshell", 0, 0, 1, 0, "Enable or disable command shell", True, True),
    (16391, "Ad Hoc Distributed Queries", 0, 0, 1, 0, "Enable or disable Ad Hoc Distributed Queries", True, True),
    (16392, "Replication XPs", 0, 0, 1, 0, "Enable or disable Replication XPs", True, True),
    (16393, "contained database authentication", 0, 0, 1, 0, "Enables contained databases and contained authentication", True, False),
    (16394, "hadoop connectivity", 0, 0, 8, 0, "Configure SQL Server to connect to external Hadoop or Microsoft Azure storage blob data sources through PolyBase", True, False),
    (16395, "polybase network encryption", 1, 0, 1, 1, "Configure SQL Server to encrypt control and data channels when using PolyBase", True, False),
    (16396, "remote data archive", 0, 0, 1, 0, "Allow the use of the REMOTE_DATA_ARCHIVE data access for databases", True, False),
    (16397, "allow polybase export", 0, 0, 1, 0, "Allows writing into an external table using PolyBase", True, False),
    (16398, "allow filesystem enumeration", 1, 0, 1, 1, "Allow enumeration of filesystem", True, True),
    (16399, "polybase enabled", 0, 0, 1, 0, "Configure SQL Server to connect to external data sources through PolyBase", True, False),
    (16400, "suppress recovery model errors", 0, 0, 1, 0, "Return warning instead of error for unsupported ALTER DATABASE SET RECOVERY command", True, True),
    (16401, "openrowset auto_create_statistics", 1, 0, 1, 1, "Enable or disable auto create statistics for openrowset sources.", True, True),
    (16402, "external rest endpoint enabled", 0, 0, 1, 0, "Enable or disable invocations of external REST endpoints", True, False),
    (16403, "external xtp dll gen util enabled", 0, 0, 1, 0, "Enable or disable using external xtp dll generation via HkDllGen.exe", True, False),
    (16404, "external AI runtimes enabled", 0, 0, 1, 0, "Enable or disable using external AI runtimes", True, False),
    (16405, "allow server scoped db credentials", 0, 0, 1, 0, "Enable or disable use of server managed identity in database scoped credentials", True, False)
]


def configurations() -> Table:
    """sys.configurations, every setting off or at its default.

    Object Explorer reads Agent XPs out of this before it decides what to do
    about SQL Server Agent, and reads several others while it builds the
    tree.
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
        rows=[list(one) for one in CONFIGURATIONS],
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


def policy_health_state() -> Table:
    """msdb.dbo.syspolicy_system_health_state, which holds no complaints.

    A client asks whether any policy has failed against this server, and no
    row is the answer: policies are off, nothing has been evaluated, and
    nothing is unhealthy. A table that is not there is a different answer
    and the client shows it as a warning.
    """
    return Table(
        name="syspolicy_system_health_state",
        columns=[
            Column("health_state_id", Integer(8)),
            Column("policy_id", Integer(4)),
            Column("last_run_date", DateTime()),
            Column("target_query_expression_with_id", NVarChar(400)),
            Column("target_query_expression", NVarChar(4000)),
            Column("result", Bit()),
        ],
        rows=[],
    )


def default_schema_views() -> dict[str, Table]:
    """Every system table served under dbo that says nothing about the data.

    sysobjects is not here because it describes what is served, and building
    it loads every source; it is asked for by name instead, the same split
    the sys views are under for the same reason.
    """
    return {
        "syspolicy_configuration": policy_configuration(),
        "syspolicy_system_health_state": policy_health_state(),
        "sysusers": sysusers(),
    }


# The two compatibility views from SQL Server 2000, which are still on a real
# server and which SSMS still reads. Object Explorer lists aggregate functions
# by joining them and filtering for type 'AF', and a server without them fails
# that whole query rather than answering it with the no rows it should have.
# Every value below was read off SQL Server 2025: a row states what is about
# the table, and the rest is what a real server reports for an ordinary one.
SYSOBJECTS: list[tuple[str, object, object]] = [
    ("name", NVarChar(128), None),
    ("id", Integer(4), None),
    ("xtype", NVarChar(2), "U"),
    ("uid", SmallInt(), 1),
    ("info", SmallInt(), 0),
    ("status", Integer(4), 0),
    ("base_schema_ver", Integer(4), 0),
    ("replinfo", Integer(4), 0),
    ("parent_obj", Integer(4), 0),
    ("crdate", DateTime(), STARTED),
    ("ftcatid", SmallInt(), 0),
    ("schema_ver", Integer(4), 0),
    ("stats_schema_ver", Integer(4), 0),
    ("type", NVarChar(2), "U"),
    ("userstat", SmallInt(), 1),
    ("sysstat", SmallInt(), 3),
    ("indexdel", SmallInt(), 0),
    ("refdate", DateTime(), STARTED),
    ("version", Integer(4), 0),
    ("deltrig", Integer(4), 0),
    ("instrig", Integer(4), 0),
    ("updtrig", Integer(4), 0),
    ("seltrig", Integer(4), 0),
    ("category", Integer(4), 0),
    ("cache", SmallInt(), 0),
]

SYSUSERS: list[tuple[str, object, object]] = [
    ("uid", SmallInt(), 1),
    ("status", SmallInt(), 0),
    ("name", NVarChar(128), SCHEMA_NAME),
    ("sid", VarBinary(85), b"\x01"),
    ("roles", VarBinary(2048), None),
    ("createdate", DateTime(), STARTED),
    ("updatedate", DateTime(), STARTED),
    ("altuid", SmallInt(), None),
    ("password", VarBinary(256), None),
    ("gid", SmallInt(), 0),
    ("environ", NVarChar(255), None),
    ("hasdbaccess", Integer(4), 1),
    ("islogin", Integer(4), 1),
    ("isntname", Integer(4), 0),
    ("isntgroup", Integer(4), 0),
    ("isntuser", Integer(4), 0),
    ("issqluser", Integer(4), 1),
    ("isaliased", Integer(4), 0),
    ("issqlrole", Integer(4), 0),
    ("isapprole", Integer(4), 0),
]


def sysobjects(tables: list[Table]) -> Table:
    """sysobjects, holding the tables served and nothing else.

    id is the number sys.objects and OBJECT_ID() give, so a client that read
    one and joined the other agrees with itself.
    """
    return _built("sysobjects", SYSOBJECTS, [
        {"name": table.name, "id": object_id(table.name)}
        for table in _in_order(tables)
    ])


def sysusers() -> Table:
    """sysusers, holding the one user everything here runs as."""
    return _built("sysusers", SYSUSERS, [{}])


# Every column of the views that describe what is served rather than the
# server, in the order a real server returns them, with what this reports for
# a table it holds. Read off SQL Server 2025 the same way the rest were: what
# is stated here is a value a real server gave for an ordinary user table,
# and what a row states is what is about that table.
SYS_TABLES: list[tuple[str, object, object]] = [
    ("name", NVarChar(128), None),
    ("object_id", Integer(4), None),
    ("principal_id", Integer(4), None),
    ("schema_id", Integer(4), 1),
    ("parent_object_id", Integer(4), 0),
    ("type", NVarChar(2), "U"),
    ("type_desc", NVarChar(60), "USER_TABLE"),
    ("create_date", DateTime(), STARTED),
    ("modify_date", DateTime(), STARTED),
    ("is_ms_shipped", Bit(), False),
    ("is_published", Bit(), False),
    ("is_schema_published", Bit(), False),
    ("lob_data_space_id", Integer(4), 0),
    ("filestream_data_space_id", Integer(4), None),
    ("max_column_id_used", Integer(4), 0),
    ("lock_on_bulk_load", Bit(), False),
    ("uses_ansi_nulls", Bit(), True),
    ("is_replicated", Bit(), False),
    ("has_replication_filter", Bit(), False),
    ("is_merge_published", Bit(), False),
    ("is_sync_tran_subscribed", Bit(), False),
    ("has_unchecked_assembly_data", Bit(), False),
    ("text_in_row_limit", Integer(4), 0),
    ("large_value_types_out_of_row", Bit(), False),
    ("is_tracked_by_cdc", Bit(), False),
    ("lock_escalation", Integer(1), 0),
    ("lock_escalation_desc", NVarChar(60), "TABLE"),
    ("is_filetable", Bit(), False),
    ("is_memory_optimized", Bit(), False),
    ("durability", Integer(1), 0),
    ("durability_desc", NVarChar(60), "SCHEMA_AND_DATA"),
    ("temporal_type", Integer(1), 0),
    ("temporal_type_desc", NVarChar(60), "NON_TEMPORAL_TABLE"),
    ("history_table_id", Integer(4), None),
    ("is_remote_data_archive_enabled", Bit(), False),
    ("is_external", Bit(), False),
    ("history_retention_period", Integer(4), None),
    ("history_retention_period_unit", Integer(4), None),
    ("history_retention_period_unit_desc", NVarChar(10), None),
    ("is_node", Bit(), False),
    ("is_edge", Bit(), False),
    ("data_retention_period", Integer(4), -1),
    ("data_retention_period_unit", Integer(4), -1),
    ("data_retention_period_unit_desc", NVarChar(10), "INFINITE"),
    ("ledger_type", Integer(1), 0),
    ("ledger_type_desc", NVarChar(60), "NON_LEDGER_TABLE"),
    ("ledger_view_id", Integer(4), None),
    ("is_dropped_ledger_table", Bit(), False),
]


SYS_COLUMNS: list[tuple[str, object, object]] = [
    ("object_id", Integer(4), None),
    ("name", NVarChar(128), None),
    ("column_id", Integer(4), None),
    ("system_type_id", Integer(1), None),
    ("user_type_id", Integer(4), None),
    ("max_length", SmallInt(), None),
    ("precision", Integer(1), 0),
    ("scale", Integer(1), 0),
    ("collation_name", NVarChar(128), None),
    ("is_nullable", Bit(), True),
    ("is_ansi_padded", Bit(), True),
    ("is_rowguidcol", Bit(), False),
    ("is_identity", Bit(), False),
    ("is_computed", Bit(), False),
    ("is_filestream", Bit(), False),
    ("is_replicated", Bit(), False),
    ("is_non_sql_subscribed", Bit(), False),
    ("is_merge_published", Bit(), False),
    ("is_dts_replicated", Bit(), False),
    ("is_xml_document", Bit(), False),
    ("xml_collection_id", Integer(4), 0),
    ("default_object_id", Integer(4), 0),
    ("rule_object_id", Integer(4), 0),
    ("is_sparse", Bit(), False),
    ("is_column_set", Bit(), False),
    ("generated_always_type", Integer(1), 0),
    ("generated_always_type_desc", NVarChar(60), "NOT_APPLICABLE"),
    ("encryption_type", Integer(4), None),
    ("encryption_type_desc", NVarChar(64), None),
    ("encryption_algorithm_name", NVarChar(128), None),
    ("column_encryption_key_id", Integer(4), None),
    ("column_encryption_key_database_name", NVarChar(128), None),
    ("is_hidden", Bit(), False),
    ("is_masked", Bit(), False),
    ("graph_type", Integer(4), None),
    ("graph_type_desc", NVarChar(60), None),
    ("is_data_deletion_filter_column", Bit(), False),
    ("ledger_view_column_type", Integer(4), None),
    ("ledger_view_column_type_desc", NVarChar(60), None),
    ("is_dropped_ledger_column", Bit(), False),
    ("vector_dimensions", Integer(4), None),
    ("vector_base_type", Integer(1), None),
    ("vector_base_type_desc", NVarChar(10), None),
]


SYS_INDEXES: list[tuple[str, object, object]] = [
    ("object_id", Integer(4), None),
    ("name", NVarChar(128), None),
    ("index_id", Integer(4), 0),
    ("type", Integer(1), 0),
    ("type_desc", NVarChar(60), "HEAP"),
    ("is_unique", Bit(), False),
    ("data_space_id", Integer(4), 1),
    ("ignore_dup_key", Bit(), False),
    ("is_primary_key", Bit(), False),
    ("is_unique_constraint", Bit(), False),
    ("fill_factor", Integer(1), 0),
    ("is_padded", Bit(), False),
    ("is_disabled", Bit(), False),
    ("is_hypothetical", Bit(), False),
    ("is_ignored_in_optimization", Bit(), False),
    ("allow_row_locks", Bit(), True),
    ("allow_page_locks", Bit(), True),
    ("has_filter", Bit(), False),
    ("filter_definition", NVarChar(4000), None),
    ("compression_delay", Integer(4), None),
    ("suppress_dup_key_messages", Bit(), False),
    ("auto_created", Bit(), False),
    ("optimize_for_sequential_key", Bit(), False),
]


SYS_OBJECTS: list[tuple[str, object, object]] = [
    ("name", NVarChar(128), None),
    ("object_id", Integer(4), None),
    ("principal_id", Integer(4), None),
    ("schema_id", Integer(4), 1),
    ("parent_object_id", Integer(4), 0),
    ("type", NVarChar(2), "U"),
    ("type_desc", NVarChar(60), "USER_TABLE"),
    ("create_date", DateTime(), STARTED),
    ("modify_date", DateTime(), STARTED),
    ("is_ms_shipped", Bit(), False),
    ("is_published", Bit(), False),
    ("is_schema_published", Bit(), False),
]


SYS_EXTENDED_PROPERTIES: list[tuple[str, object, object]] = [
    ("class", Integer(1), None),
    ("class_desc", NVarChar(60), None),
    ("major_id", Integer(4), None),
    ("minor_id", Integer(4), None),
    ("name", NVarChar(128), None),
    ("value", NVarChar(4000), None),
]


SYS_FILETABLES: list[tuple[str, object, object]] = [
    ("object_id", Integer(4), None),
    ("is_enabled", Bit(), None),
    ("directory_name", NVarChar(256), None),
    ("filename_collation_id", Integer(4), None),
    ("filename_collation_name", NVarChar(129), None),
]


SYS_DATA_SPACES: list[tuple[str, object, object]] = [
    ("name", NVarChar(128), None),
    ("data_space_id", Integer(4), None),
    ("type", NVarChar(2), None),
    ("type_desc", NVarChar(60), None),
    ("is_default", Bit(), None),
    ("is_system", Bit(), None),
]


SYS_SCHEMAS: list[tuple[str, object, object]] = [
    ("name", NVarChar(128), None),
    ("schema_id", Integer(4), None),
    ("principal_id", Integer(4), None),
]


SYS_TYPES: list[tuple[str, object, object]] = [
    ("name", NVarChar(128), None),
    ("system_type_id", Integer(1), None),
    ("user_type_id", Integer(4), None),
    ("schema_id", Integer(4), None),
    ("principal_id", Integer(4), None),
    ("max_length", SmallInt(), None),
    ("precision", Integer(1), None),
    ("scale", Integer(1), None),
    ("collation_name", NVarChar(128), None),
    ("is_nullable", Bit(), None),
    ("is_user_defined", Bit(), None),
    ("is_assembly_type", Bit(), None),
    ("default_object_id", Integer(4), None),
    ("rule_object_id", Integer(4), None),
    ("is_table_type", Bit(), None),
]


SYS_DATABASE_RECOVERY_STATUS: list[tuple[str, object, object]] = [
    ("database_id", Integer(4), None),
    ("database_guid", UniqueIdentifier(), None),
    ("family_guid", UniqueIdentifier(), None),
    ("last_log_backup_lsn", Float(8), None),
    ("recovery_fork_guid", UniqueIdentifier(), None),
    ("first_recovery_fork_guid", UniqueIdentifier(), None),
    ("fork_point_lsn", Float(8), None),
]


SYS_CHANGE_TRACKING_DATABASES: list[tuple[str, object, object]] = [
    ("database_id", Integer(4), None),
    ("is_auto_cleanup_on", Integer(1), None),
    ("retention_period", Integer(4), None),
    ("retention_period_units", Integer(1), None),
    ("retention_period_units_desc", NVarChar(60), None),
    ("max_cleanup_version", Integer(8), None),
]


SYS_DATABASE_FILESTREAM_OPTIONS: list[tuple[str, object, object]] = [
    ("database_id", Integer(4), None),
    ("non_transacted_access", Integer(1), None),
    ("non_transacted_access_desc", NVarChar(60), None),
    ("directory_name", NVarChar(256), None),
]


def sys_tables(tables: list[Table]) -> Table:
    """sys.tables, one row per table this serves.

    Not shipped by Microsoft, so a client files them under Tables rather than
    under System Tables. Every one is a plain heap in the default filegroup,
    which is what a table with no indexes is.
    """
    return _built("tables", SYS_TABLES, [
        {
            "name": table.name,
            "object_id": object_id(table.name),
            "max_column_id_used": len(table.columns),
        }
        for table in _in_order(tables)
    ])


def sys_objects(tables: list[Table]) -> Table:
    """sys.objects, which holds the tables and nothing else."""
    return _built("objects", SYS_OBJECTS, [
        {"name": table.name, "object_id": object_id(table.name)}
        for table in _in_order(tables)
    ])


def sys_columns(tables: list[Table]) -> Table:
    """sys.all_columns, one row per column of every table served."""
    said = []
    for table in _in_order(tables):
        for at, column in enumerate(table.columns, start=1):
            kind, length = _type_of(column)
            precision, scale = _PRECISION_AND_SCALE.get(kind, (0, 0))
            said.append({
                "object_id": object_id(table.name),
                "name": column.name,
                "column_id": at,
                "system_type_id": kind,
                "user_type_id": kind,
                "max_length": length,
                "precision": precision,
                "scale": scale,
                "collation_name": COLLATION_NAME if kind == _NVARCHAR else None,
            })
    return _built("all_columns", SYS_COLUMNS, said)


def sys_indexes(tables: list[Table]) -> Table:
    """sys.indexes, one heap per table.

    A table with no index still has a row here: index_id 0, type HEAP. That
    is how a client tells a table it can read from one it cannot find, and
    the Tables node reads three columns of it for every table it lists.
    """
    return _built("indexes", SYS_INDEXES, [
        {"object_id": object_id(table.name)} for table in _in_order(tables)
    ])


def sys_schemas() -> Table:
    """sys.schemas, holding the one schema everything here is in."""
    return _built("schemas", SYS_SCHEMAS,
                  [{"name": SCHEMA_NAME, "schema_id": 1, "principal_id": 1}])


def sys_data_spaces() -> Table:
    """sys.data_spaces, the filegroup every table is nominally in."""
    return _built("data_spaces", SYS_DATA_SPACES, [{
        "name": "PRIMARY", "data_space_id": 1, "type": "FG",
        "type_desc": "ROWS_FILEGROUP", "is_default": True, "is_system": False,
    }])


def sys_types() -> Table:
    """sys.types, the types a column here can have.

    A client reads this to find out what it is dealing with, and asks it
    questions like whether char collates as UTF-8. Every row is a type SQL
    Server has, with the numbers SQL Server gives it.
    """
    return _built("types", SYS_TYPES, [
        {
            "name": name, "system_type_id": number, "user_type_id": number,
            "schema_id": 4, "principal_id": None,
            "max_length": length, "precision": precision, "scale": scale,
            "collation_name": COLLATION_NAME if name in _COLLATED else None,
            "is_nullable": True, "is_user_defined": False,
            "is_assembly_type": False, "default_object_id": 0,
            "rule_object_id": 0, "is_table_type": False,
        }
        for name, number, length, precision, scale in TYPES_SERVED
    ])


def sys_extended_properties() -> Table:
    """sys.extended_properties, of which nothing here has any.

    Empty rather than absent: a client asks whether a table is marked as a
    tool's own, and no row is the answer.
    """
    return _built("extended_properties", SYS_EXTENDED_PROPERTIES, [])


def sys_filetables() -> Table:
    """sys.filetables. Nothing here is one, and the view still has to exist."""
    return _built("filetables", SYS_FILETABLES, [])


def sys_database_recovery_status() -> Table:
    """sys.database_recovery_status, joined to sys.databases by the tree."""
    return _built("database_recovery_status", SYS_DATABASE_RECOVERY_STATUS,
                  [{"database_id": DATABASE_ID}])


def sys_change_tracking_databases() -> Table:
    """sys.change_tracking_databases. Nothing here tracks changes."""
    return _built("change_tracking_databases", SYS_CHANGE_TRACKING_DATABASES, [])


def sys_database_filestream_options() -> Table:
    """sys.database_filestream_options, which this database has none of."""
    return _built("database_filestream_options",
                  SYS_DATABASE_FILESTREAM_OPTIONS,
                  [{"database_id": DATABASE_ID, "directory_name": None,
                    "non_transacted_access": 0,
                    "non_transacted_access_desc": "OFF"}])


def _in_order(tables: list[Table]) -> list[Table]:
    return sorted(tables, key=lambda one: one.name.lower())


# The type numbers SQL Server gives the types this serves, and everything
# else it reports about them. Read from sys.types on SQL Server 2025.
_NVARCHAR = 231
TYPES_SERVED = [
    ("uniqueidentifier", 36, 16, 0, 0),
    ("tinyint", 48, 1, 3, 0),
    ("smallint", 52, 2, 5, 0),
    ("int", 56, 4, 10, 0),
    ("datetime", 61, 8, 23, 3),
    ("float", 62, 8, 53, 0),
    ("bit", 104, 1, 1, 0),
    ("bigint", 127, 8, 19, 0),
    ("varbinary", 165, 8000, 0, 0),
    ("nvarchar", _NVARCHAR, 8000, 0, 0),
]
_COLLATED = {"nvarchar"}

# What sys.columns says of a column's precision and scale, which is what
# sys.types says of its type: an int is 10 and 0, a datetime 23 and 3, and
# text is nought and nought. Measured over a table of every type; this
# reported nought for all of them.
_PRECISION_AND_SCALE = {number: (precision, scale)
                        for _, number, _, precision, scale in TYPES_SERVED}

# What a column of ours is, in the numbers sys.all_columns reports. Length is
# in bytes, so text is twice its characters, which is what a real server says.
_TYPE_NUMBERS = {
    "Integer": lambda kind: (127 if kind.width == 8 else
                             48 if kind.width == 1 else
                             52 if kind.width == 2 else 56,
                             kind.width),
    "SmallInt": lambda kind: (52, 2),
    # A column of nothing but a written NULL, which is an int like any other.
    "UntypedNull": lambda kind: (56, 4),
    "Float": lambda kind: (62, 8),
    "Bit": lambda kind: (104, 1),
    "UniqueIdentifier": lambda kind: (36, 16),
    "DateTime": lambda kind: (61, 8),
    "VarBinary": lambda kind: (165, kind.size),
    "Binary": lambda kind: (173, kind.size),
    "NVarChar": lambda kind: (_NVARCHAR, kind.max_chars * 2),
}


def _type_of(column: Column) -> tuple[int, int]:
    """The type number and byte length a client reads for one of our columns."""
    found = _TYPE_NUMBERS.get(type(column.type).__name__)
    return found(column.type) if found else (_NVARCHAR, 8000)


# The file a database is kept in. There is no file: the rows come from
# whatever a configuration points at, and some of that is not on this
# machine at all. One row saying so, with a name and no path, because a
# client reads this to show where a database lives and an empty view makes
# it show nothing rather than nothing-to-show.
SYS_DATABASE_FILES: list[tuple[str, object, object]] = [
    ("file_id", Integer(4), 1),
    ("file_guid", UniqueIdentifier(), None),
    ("type", Integer(1), 0),
    ("type_desc", NVarChar(60), "ROWS"),
    ("data_space_id", Integer(4), 1),
    ("name", NVarChar(128), ""),
    ("physical_name", NVarChar(260), ""),
    ("state", Integer(1), 0),
    ("state_desc", NVarChar(60), "ONLINE"),
    ("size", Integer(4), 0),
    ("max_size", Integer(4), -1),
    ("growth", Integer(4), 0),
    ("is_media_read_only", Bit(), False),
    ("is_read_only", Bit(), True),
    ("is_sparse", Bit(), False),
    ("is_percent_growth", Bit(), False),
    ("is_name_reserved", Bit(), False),
    ("is_persistent_log_buffer", Bit(), False),
    ("create_lsn", Float(8), None),
    ("drop_lsn", Float(8), None),
    ("read_only_lsn", Float(8), None),
    ("read_write_lsn", Float(8), None),
    ("differential_base_lsn", Float(8), None),
    ("differential_base_guid", UniqueIdentifier(), None),
    ("differential_base_time", DateTime(), None),
    ("redo_start_lsn", Float(8), None),
    ("redo_start_fork_guid", UniqueIdentifier(), None),
    ("redo_target_lsn", Float(8), None),
    ("redo_target_fork_guid", UniqueIdentifier(), None),
    ("backup_lsn", Float(8), None),
]

# Who can log in, which here is whoever Windows says. One row for the login
# that asked, because a client reads its default database out of this and a
# login with no row has none.
SYS_SERVER_PRINCIPALS: list[tuple[str, object, object]] = [
    ("name", NVarChar(128), ""),
    ("principal_id", Integer(4), 1),
    ("sid", VarBinary(85), b""),
    ("type", NVarChar(1), "U"),
    ("type_desc", NVarChar(60), "WINDOWS_LOGIN"),
    ("is_disabled", Bit(), False),
    ("create_date", DateTime(), STARTED),
    ("modify_date", DateTime(), STARTED),
    ("default_database_name", NVarChar(128), ""),
    ("default_language_name", NVarChar(128), "us_english"),
    ("credential_id", Integer(4), None),
    ("owning_principal_id", Integer(4), None),
    ("is_fixed_role", Bit(), False),
    ("tenant_id", UniqueIdentifier(), None),
]


def sys_database_files(name: str) -> Table:
    """sys.database_files, the one file this database nominally has."""
    return _built("database_files", SYS_DATABASE_FILES,
                  [{"name": name, "physical_name": name}])


def sys_server_principals(login: str, database: str) -> Table:
    """sys.server_principals, holding whoever is asking."""
    return _built("server_principals", SYS_SERVER_PRINCIPALS,
                  [{"name": login, "default_database_name": database}])



# Nothing here is a view: a source is a table, and the one thing this does
# with a query is answer it. The view still exists, because a client that
# asks for the views of a database and is told there is no such thing shows
# an error where it should show an empty folder.
SYS_VIEWS: list[tuple[str, object, object]] = [
    ("name", NVarChar(128), None),
    ("object_id", Integer(4), None),
    ("principal_id", Integer(4), None),
    ("schema_id", Integer(4), None),
    ("parent_object_id", Integer(4), None),
    ("type", NVarChar(2), None),
    ("type_desc", NVarChar(60), None),
    ("create_date", DateTime(), None),
    ("modify_date", DateTime(), None),
    ("is_ms_shipped", Bit(), None),
    ("is_published", Bit(), None),
    ("is_schema_published", Bit(), None),
    ("is_replicated", Bit(), None),
    ("has_replication_filter", Bit(), None),
    ("has_opaque_metadata", Bit(), None),
    ("has_unchecked_assembly_data", Bit(), None),
    ("with_check_option", Bit(), None),
    ("is_date_correlation_view", Bit(), None),
    ("is_tracked_by_cdc", Bit(), None),
    ("has_snapshot", Bit(), None),
    ("ledger_view_type", Integer(1), None),
    ("ledger_view_type_desc", NVarChar(60), None),
    ("is_dropped_ledger_view", Bit(), None),
]


# Whether this server is in a failover cluster. It is not, and a real one
# that is not still has a row here saying so.
SYS_HADR_CLUSTER: list[tuple[str, object, object]] = [
    ("cluster_name", NVarChar(256), ""),
    ("quorum_type", Integer(1), 3),
    ("quorum_type_desc", NVarChar(60), "UNKNOWN_QUORUM"),
    ("quorum_state", Integer(1), 3),
    ("quorum_state_desc", NVarChar(60), "UNKNOWN_QUORUM_STATE"),
]


def sys_hadr_cluster() -> Table:
    """sys.dm_hadr_cluster, which names no cluster because there is none."""
    return _built("dm_hadr_cluster", SYS_HADR_CLUSTER, [{}])


def sys_views() -> Table:
    """sys.all_views, of which this serves none."""
    return _built("all_views", SYS_VIEWS, [])



# The rest of what a client reads while it works out the shape of a table,
# at the shape a real server has them, read off SQL Server 2025. Nothing
# here has a default, a foreign key, an index, a computed column, an
# identity, a synonym, an XML schema or a module, and no row is what says
# so. A view that is not there says something else, and a client asking
# about defaults and told there is no such thing as a default stops.
#
# The list is not a guess about what might be asked: it is what has been
# asked, taken from the log of real clients working through the tree.
OTHER_SYS_VIEWS: dict[str, list[tuple[str, object]]] = {
    "availability_groups": [
        ("group_id", UniqueIdentifier()),
        ("name", NVarChar(128)),
        ("resource_id", NVarChar(40)),
        ("resource_group_id", NVarChar(40)),
        ("failure_condition_level", Integer(4)),
        ("health_check_timeout", Integer(4)),
        ("automated_backup_preference", Integer(1)),
        ("automated_backup_preference_desc", NVarChar(60)),
        ("version", SmallInt()),
        ("basic_features", Bit()),
        ("dtc_support", Bit()),
        ("db_failover", Bit()),
        ("is_distributed", Bit()),
        ("cluster_type", Integer(1)),
        ("cluster_type_desc", NVarChar(60)),
        ("required_synchronized_secondaries_to_commit", Integer(4)),
        ("sequence_number", Integer(8)),
        ("is_contained", Bit()),
        ("cluster_connection_options", NVarChar(4000)),
    ],
    "availability_replicas": [
        ("replica_id", UniqueIdentifier()),
        ("group_id", UniqueIdentifier()),
        ("replica_metadata_id", Integer(4)),
        ("replica_server_name", NVarChar(256)),
        ("owner_sid", VarBinary(85)),
        ("endpoint_url", NVarChar(256)),
        ("availability_mode", Integer(1)),
        ("availability_mode_desc", NVarChar(60)),
        ("failover_mode", Integer(1)),
        ("failover_mode_desc", NVarChar(60)),
        ("session_timeout", Integer(4)),
        ("primary_role_allow_connections", Integer(1)),
        ("primary_role_allow_connections_desc", NVarChar(60)),
        ("secondary_role_allow_connections", Integer(1)),
        ("secondary_role_allow_connections_desc", NVarChar(60)),
        ("create_date", DateTime()),
        ("modify_date", DateTime()),
        ("backup_priority", Integer(4)),
        ("read_only_routing_url", NVarChar(256)),
        ("seeding_mode", Integer(1)),
        ("seeding_mode_desc", NVarChar(60)),
        ("read_write_routing_url", NVarChar(256)),
    ],
    "computed_columns": [
        ("object_id", Integer(4)),
        ("name", NVarChar(128)),
        ("column_id", Integer(4)),
        ("system_type_id", Integer(1)),
        ("user_type_id", Integer(4)),
        ("max_length", SmallInt()),
        ("precision", Integer(1)),
        ("scale", Integer(1)),
        ("collation_name", NVarChar(128)),
        ("is_nullable", Bit()),
        ("is_ansi_padded", Bit()),
        ("is_rowguidcol", Bit()),
        ("is_identity", Bit()),
        ("is_filestream", Bit()),
        ("is_replicated", Bit()),
        ("is_non_sql_subscribed", Bit()),
        ("is_merge_published", Bit()),
        ("is_dts_replicated", Bit()),
        ("is_xml_document", Bit()),
        ("xml_collection_id", Integer(4)),
        ("default_object_id", Integer(4)),
        ("rule_object_id", Integer(4)),
        ("definition", NVarChar(4000)),
        ("uses_database_collation", Bit()),
        ("is_persisted", Bit()),
        ("is_computed", Bit()),
        ("is_sparse", Bit()),
        ("is_column_set", Bit()),
        ("generated_always_type", Integer(1)),
        ("generated_always_type_desc", NVarChar(60)),
        ("encryption_type", Integer(4)),
        ("encryption_type_desc", NVarChar(64)),
        ("encryption_algorithm_name", NVarChar(128)),
        ("column_encryption_key_id", Integer(4)),
        ("column_encryption_key_database_name", NVarChar(128)),
        ("is_hidden", Bit()),
        ("is_masked", Bit()),
        ("graph_type", Integer(4)),
        ("graph_type_desc", NVarChar(60)),
        ("is_data_deletion_filter_column", Bit()),
        ("ledger_view_column_type", Integer(4)),
        ("ledger_view_column_type_desc", NVarChar(60)),
        ("is_dropped_ledger_column", Bit()),
        ("is_index_column_expression", Bit()),
    ],
    "database_principals": [
        ("name", NVarChar(128)),
        ("principal_id", Integer(4)),
        ("type", NVarChar(1)),
        ("type_desc", NVarChar(60)),
        ("default_schema_name", NVarChar(128)),
        ("create_date", DateTime()),
        ("modify_date", DateTime()),
        ("owning_principal_id", Integer(4)),
        ("sid", VarBinary(85)),
        ("is_fixed_role", Bit()),
        ("authentication_type", Integer(4)),
        ("authentication_type_desc", NVarChar(60)),
        ("default_language_name", NVarChar(128)),
        ("default_language_lcid", Integer(4)),
        ("allow_encrypted_value_modifications", Bit()),
        ("tenant_id", UniqueIdentifier()),
    ],
    "default_constraints": [
        ("name", NVarChar(128)),
        ("object_id", Integer(4)),
        ("principal_id", Integer(4)),
        ("schema_id", Integer(4)),
        ("parent_object_id", Integer(4)),
        ("type", NVarChar(2)),
        ("type_desc", NVarChar(60)),
        ("create_date", DateTime()),
        ("modify_date", DateTime()),
        ("is_ms_shipped", Bit()),
        ("is_published", Bit()),
        ("is_schema_published", Bit()),
        ("parent_column_id", Integer(4)),
        ("definition", NVarChar(4000)),
        ("is_system_named", Bit()),
    ],
    "dm_exec_connections": [
        ("session_id", Integer(4)),
        ("most_recent_session_id", Integer(4)),
        ("connect_time", DateTime()),
        ("net_transport", NVarChar(40)),
        ("protocol_type", NVarChar(40)),
        ("protocol_version", Integer(4)),
        ("endpoint_id", Integer(4)),
        ("encrypt_option", NVarChar(40)),
        ("auth_scheme", NVarChar(40)),
        ("node_affinity", SmallInt()),
        ("num_reads", Integer(4)),
        ("num_writes", Integer(4)),
        ("last_read", DateTime()),
        ("last_write", DateTime()),
        ("net_packet_size", Integer(4)),
        ("client_net_address", NVarChar(48)),
        ("client_tcp_port", Integer(4)),
        ("local_net_address", NVarChar(48)),
        ("local_tcp_port", Integer(4)),
        ("connection_id", UniqueIdentifier()),
        ("parent_connection_id", UniqueIdentifier()),
        ("most_recent_sql_handle", VarBinary(64)),
    ],
    "dm_hadr_database_replica_states": [
        ("database_id", Integer(4)),
        ("group_id", UniqueIdentifier()),
        ("replica_id", UniqueIdentifier()),
        ("group_database_id", UniqueIdentifier()),
        ("is_local", Bit()),
        ("is_primary_replica", Bit()),
        ("synchronization_state", Integer(1)),
        ("synchronization_state_desc", NVarChar(60)),
        ("is_commit_participant", Bit()),
        ("synchronization_health", Integer(1)),
        ("synchronization_health_desc", NVarChar(60)),
        ("database_state", Integer(1)),
        ("database_state_desc", NVarChar(60)),
        ("is_suspended", Bit()),
        ("suspend_reason", Integer(1)),
        ("suspend_reason_desc", NVarChar(60)),
        ("recovery_lsn", Float(8)),
        ("truncation_lsn", Float(8)),
        ("last_sent_lsn", Float(8)),
        ("last_sent_time", DateTime()),
        ("last_received_lsn", Float(8)),
        ("last_received_time", DateTime()),
        ("last_hardened_lsn", Float(8)),
        ("last_hardened_time", DateTime()),
        ("last_redone_lsn", Float(8)),
        ("last_redone_time", DateTime()),
        ("log_send_queue_size", Integer(8)),
        ("log_send_rate", Integer(8)),
        ("redo_queue_size", Integer(8)),
        ("redo_rate", Integer(8)),
        ("filestream_send_rate", Integer(8)),
        ("end_of_log_lsn", Float(8)),
        ("last_commit_lsn", Float(8)),
        ("last_commit_time", DateTime()),
        ("low_water_mark_for_ghosts", Integer(8)),
        ("secondary_lag_seconds", Integer(8)),
        ("quorum_commit_lsn", Float(8)),
        ("quorum_commit_time", DateTime()),
        ("is_internal", Bit()),
    ],
    "foreign_key_columns": [
        ("constraint_object_id", Integer(4)),
        ("constraint_column_id", Integer(4)),
        ("parent_object_id", Integer(4)),
        ("parent_column_id", Integer(4)),
        ("referenced_object_id", Integer(4)),
        ("referenced_column_id", Integer(4)),
    ],
    "foreign_keys": [
        ("name", NVarChar(128)),
        ("object_id", Integer(4)),
        ("principal_id", Integer(4)),
        ("schema_id", Integer(4)),
        ("parent_object_id", Integer(4)),
        ("type", NVarChar(2)),
        ("type_desc", NVarChar(60)),
        ("create_date", DateTime()),
        ("modify_date", DateTime()),
        ("is_ms_shipped", Bit()),
        ("is_published", Bit()),
        ("is_schema_published", Bit()),
        ("referenced_object_id", Integer(4)),
        ("key_index_id", Integer(4)),
        ("is_disabled", Bit()),
        ("is_not_for_replication", Bit()),
        ("is_not_trusted", Bit()),
        ("delete_referential_action", Integer(1)),
        ("delete_referential_action_desc", NVarChar(60)),
        ("update_referential_action", Integer(1)),
        ("update_referential_action_desc", NVarChar(60)),
        ("is_system_named", Bit()),
    ],
    "identity_columns": [
        ("object_id", Integer(4)),
        ("name", NVarChar(128)),
        ("column_id", Integer(4)),
        ("system_type_id", Integer(1)),
        ("user_type_id", Integer(4)),
        ("max_length", SmallInt()),
        ("precision", Integer(1)),
        ("scale", Integer(1)),
        ("collation_name", NVarChar(128)),
        ("is_nullable", Bit()),
        ("is_ansi_padded", Bit()),
        ("is_rowguidcol", Bit()),
        ("is_identity", Bit()),
        ("is_filestream", Bit()),
        ("is_replicated", Bit()),
        ("is_non_sql_subscribed", Bit()),
        ("is_merge_published", Bit()),
        ("is_dts_replicated", Bit()),
        ("is_xml_document", Bit()),
        ("xml_collection_id", Integer(4)),
        ("default_object_id", Integer(4)),
        ("rule_object_id", Integer(4)),
        ("seed_value", NVarChar(4000)),
        ("increment_value", NVarChar(4000)),
        ("last_value", NVarChar(4000)),
        ("is_not_for_replication", Bit()),
        ("is_computed", Bit()),
        ("is_sparse", Bit()),
        ("is_column_set", Bit()),
        ("generated_always_type", Integer(1)),
        ("generated_always_type_desc", NVarChar(60)),
        ("encryption_type", Integer(4)),
        ("encryption_type_desc", NVarChar(64)),
        ("encryption_algorithm_name", NVarChar(128)),
        ("column_encryption_key_id", Integer(4)),
        ("column_encryption_key_database_name", NVarChar(128)),
        ("is_hidden", Bit()),
        ("is_masked", Bit()),
        ("graph_type", Integer(4)),
        ("graph_type_desc", NVarChar(60)),
        ("is_data_deletion_filter_column", Bit()),
        ("ledger_view_column_type", Integer(4)),
        ("ledger_view_column_type_desc", NVarChar(60)),
        ("is_dropped_ledger_column", Bit()),
    ],
    "index_columns": [
        ("object_id", Integer(4)),
        ("index_id", Integer(4)),
        ("index_column_id", Integer(4)),
        ("column_id", Integer(4)),
        ("key_ordinal", Integer(1)),
        ("partition_ordinal", Integer(1)),
        ("is_descending_key", Bit()),
        ("is_included_column", Bit()),
        ("column_store_order_ordinal", Integer(1)),
        ("data_clustering_ordinal", Integer(1)),
    ],
    "master_files": [
        ("database_id", Integer(4)),
        ("file_id", Integer(4)),
        ("file_guid", UniqueIdentifier()),
        ("type", Integer(1)),
        ("type_desc", NVarChar(60)),
        ("data_space_id", Integer(4)),
        ("name", NVarChar(128)),
        ("physical_name", NVarChar(260)),
        ("state", Integer(1)),
        ("state_desc", NVarChar(60)),
        ("size", Integer(4)),
        ("max_size", Integer(4)),
        ("growth", Integer(4)),
        ("is_media_read_only", Bit()),
        ("is_read_only", Bit()),
        ("is_sparse", Bit()),
        ("is_percent_growth", Bit()),
        ("is_name_reserved", Bit()),
        ("is_persistent_log_buffer", Bit()),
        ("create_lsn", Float(8)),
        ("drop_lsn", Float(8)),
        ("read_only_lsn", Float(8)),
        ("read_write_lsn", Float(8)),
        ("differential_base_lsn", Float(8)),
        ("differential_base_guid", UniqueIdentifier()),
        ("differential_base_time", DateTime()),
        ("redo_start_lsn", Float(8)),
        ("redo_start_fork_guid", UniqueIdentifier()),
        ("redo_target_lsn", Float(8)),
        ("redo_target_fork_guid", UniqueIdentifier()),
        ("backup_lsn", Float(8)),
        ("credential_id", Integer(4)),
    ],
    "sql_modules": [
        ("object_id", Integer(4)),
        ("definition", NVarChar(4000)),
        ("uses_ansi_nulls", Bit()),
        ("uses_quoted_identifier", Bit()),
        ("is_schema_bound", Bit()),
        ("uses_database_collation", Bit()),
        ("is_recompiled", Bit()),
        ("null_on_null_input", Bit()),
        ("execute_as_principal_id", Integer(4)),
        ("uses_native_compilation", Bit()),
        ("inline_type", Bit()),
        ("is_inlineable", Bit()),
    ],
    "synonyms": [
        ("name", NVarChar(128)),
        ("object_id", Integer(4)),
        ("principal_id", Integer(4)),
        ("schema_id", Integer(4)),
        ("parent_object_id", Integer(4)),
        ("type", NVarChar(2)),
        ("type_desc", NVarChar(60)),
        ("create_date", DateTime()),
        ("modify_date", DateTime()),
        ("is_ms_shipped", Bit()),
        ("is_published", Bit()),
        ("is_schema_published", Bit()),
        ("base_object_name", NVarChar(1035)),
    ],
    "system_sql_modules": [
        ("object_id", Integer(4)),
        ("definition", NVarChar(4000)),
        ("uses_ansi_nulls", Bit()),
        ("uses_quoted_identifier", Bit()),
        ("is_schema_bound", Bit()),
        ("uses_database_collation", Bit()),
        ("is_recompiled", Bit()),
        ("null_on_null_input", Bit()),
        ("execute_as_principal_id", Integer(4)),
        ("uses_native_compilation", Bit()),
        ("inline_type", Bit()),
        ("is_inlineable", Bit()),
    ],
    "table_types": [
        ("name", NVarChar(128)),
        ("system_type_id", Integer(1)),
        ("user_type_id", Integer(4)),
        ("schema_id", Integer(4)),
        ("principal_id", Integer(4)),
        ("max_length", SmallInt()),
        ("precision", Integer(1)),
        ("scale", Integer(1)),
        ("collation_name", NVarChar(128)),
        ("is_nullable", Bit()),
        ("is_user_defined", Bit()),
        ("is_assembly_type", Bit()),
        ("default_object_id", Integer(4)),
        ("rule_object_id", Integer(4)),
        ("is_table_type", Bit()),
        ("type_table_object_id", Integer(4)),
        ("is_memory_optimized", Bit()),
    ],
    "xml_indexes": [
        ("object_id", Integer(4)),
        ("name", NVarChar(128)),
        ("index_id", Integer(4)),
        ("type", Integer(1)),
        ("type_desc", NVarChar(60)),
        ("is_unique", Bit()),
        ("data_space_id", Integer(4)),
        ("ignore_dup_key", Bit()),
        ("is_primary_key", Bit()),
        ("is_unique_constraint", Bit()),
        ("fill_factor", Integer(1)),
        ("is_padded", Bit()),
        ("is_disabled", Bit()),
        ("is_hypothetical", Bit()),
        ("is_ignored_in_optimization", Bit()),
        ("allow_row_locks", Bit()),
        ("allow_page_locks", Bit()),
        ("using_xml_index_id", Integer(4)),
        ("secondary_type", NVarChar(1)),
        ("secondary_type_desc", NVarChar(60)),
        ("has_filter", Bit()),
        ("filter_definition", NVarChar(4000)),
        ("xml_index_type", Integer(1)),
        ("xml_index_type_description", NVarChar(60)),
        ("path_id", Integer(4)),
        ("auto_created", Bit()),
    ],
    "xml_schema_collections": [
        ("xml_collection_id", Integer(4)),
        ("schema_id", Integer(4)),
        ("principal_id", Integer(4)),
        ("name", NVarChar(128)),
        ("create_date", DateTime()),
        ("modify_date", DateTime()),
    ],
}


def other_views(login: str, database: str) -> dict[str, Table]:
    """Those views, built. Two of them have a row; the rest have none."""
    built = {
        name: Table(name=name,
                    columns=[Column(one, kind) for one, kind in columns],
                    rows=[])
        for name, columns in OTHER_SYS_VIEWS.items()
    }
    # The user everything here runs as, which is dbo, and the file the
    # database nominally lives in. A client reads both by name and finds
    # nothing where an empty view would leave it guessing.
    built["database_principals"] = _built(
        "database_principals",
        [(one, kind, None) for one, kind in OTHER_SYS_VIEWS["database_principals"]],
        [{
            "name": SCHEMA_NAME, "principal_id": 1, "type": "S",
            "type_desc": "SQL_USER", "default_schema_name": SCHEMA_NAME,
            "create_date": STARTED, "modify_date": STARTED,
            "owning_principal_id": None, "sid": b"", "is_fixed_role": False,
            "authentication_type": 1, "authentication_type_desc": "INSTANCE",
            "allow_encrypted_value_modifications": False,
        }],
    )
    built["master_files"] = _built(
        "master_files",
        [(one, kind, None) for one, kind in OTHER_SYS_VIEWS["master_files"]],
        [{
            "database_id": DATABASE_ID, "file_id": 1, "type": 0,
            "type_desc": "ROWS", "data_space_id": 1, "name": database,
            "physical_name": database, "state": 0, "state_desc": "ONLINE",
            "size": 0, "max_size": -1, "growth": 0, "is_read_only": True,
        }],
    )
    return built


def object_views(tables: list[Table], login: str = "") -> dict[str, Table]:
    """The sys views that describe what is served, by lower-case name.

    Apart from the static ones because they cost a load of every source: a
    client asking what edition this is should not pull a CSV off disk and an
    API over the wire to be told.
    """
    built = [
        sys_tables(tables), sys_objects(tables), sys_columns(tables),
        sys_indexes(tables), sys_schemas(), sys_data_spaces(), sys_types(),
        sys_extended_properties(), sys_filetables(),
        sys_database_recovery_status(), sys_change_tracking_databases(),
        sys_database_filestream_options(),
        sys_views(), sys_hadr_cluster(),
        sys_database_files(DATABASE_NAME),
        sys_server_principals(login or "", DATABASE_NAME),
    ]
    served = {view.name.lower(): view for view in built}
    # sys.columns is sys.all_columns without the system objects, and there
    # are none here, so it is the same view under both names.
    served["columns"] = served["all_columns"]
    # sys.views is sys.all_views without the system ones, and there are none
    # of either, so it is the same empty view under both names.
    served["views"] = served["all_views"]
    served.update(other_views(login, DATABASE_NAME))
    return served


def system_views(database: str = "") -> dict[str, Table]:
    """Every view served under the sys schema, by lower-case name."""
    return {
        "dm_os_host_info": host_info(),
        "databases": databases(database),
        "database_mirroring": database_mirroring(),
        "configurations": configurations(),
    }
