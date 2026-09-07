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
    ("default_language_lcid", SmallInt(), None),
    ("default_language_name", NVarChar(128), None),
    ("default_fulltext_language_lcid", Integer(4), None),
    ("default_fulltext_language_name", NVarChar(128), None),
    ("is_nested_triggers_on", Bit(), None),
    ("is_transform_noise_words_on", Bit(), None),
    ("two_digit_year_cutoff", SmallInt(), None),
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
        "database_mirroring": database_mirroring(),
        "configurations": configurations(),
    }
