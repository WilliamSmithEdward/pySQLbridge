"""The catalog procedures a SQL Server client calls to find out what is here.

Every client discovers tables the same way, and none of them do it by reading
INFORMATION_SCHEMA. Measured against this server:

    ODBC Driver 18, which is what Excel and Power Query use
        sys.sp_tables, then sys.sp_columns_100
    .NET SqlClient
        INFORMATION_SCHEMA.TABLES, then sys.sp_columns_managed
    MSOLEDBSQL, the OLE DB provider
        [database].[sys].sp_tables_rowset2, and the rowset family with it

Without these a client connects, authenticates, and then shows an empty table
picker, which looks like an empty server rather than a missing procedure.

The layouts are ODBC's, fixed since SQL Server implemented them, and clients
read the columns by position as much as by name. They are reproduced here
rather than approximated: a driver that finds SCALE where it expects RADIX
does not report a mismatch, it reports the wrong type.
"""

from __future__ import annotations

import fnmatch

from .tds.result import (
    CATALOG_COLUMN_FLAGS,
    Binary,
    Bit,
    Column,
    DateTime,
    Float,
    Integer,
    NVarChar,
    QueryResult,
    SmallInt,
    UniqueIdentifier,
)

# The one database this server has. A client asking to see the catalog is
# asking about this.
CATALOG = "pysqlbridge"
SCHEMA = "dbo"

# ODBC type codes. A driver maps the column to its own type system from these,
# so an int reported as SQL_WVARCHAR arrives in Excel as text.
SQL_INTEGER = 4
SQL_BIGINT = -5
SQL_FLOAT = 6
SQL_WVARCHAR = -9
SQL_WLONGVARCHAR = -10

# OLE DB's own type codes, which the rowset family reports instead. Not a
# different spelling of the same numbers: nvarchar is -9 to ODBC and 130 to
# OLE DB, and -9 is not a DBTYPE at all. Read off a real server rather than
# assumed, by running sp_columns_100_rowset2 and looking at what it said about
# columns whose types were known.
DBTYPE_I2 = 2
DBTYPE_I4 = 3
DBTYPE_R8 = 5
DBTYPE_BOOL = 11
DBTYPE_I8 = 20
DBTYPE_WSTR = 130
DBTYPE_DBTIMESTAMP = 135

# What OLE DB says about a column in COLUMN_FLAGS. Everything served here is
# readable, nullable, and not writable.
DBCOLUMNFLAGS_ISFIXEDLENGTH = 0x10
DBCOLUMNFLAGS_ISNULLABLE = 0x20
DBCOLUMNFLAGS_MAYBENULL = 0x40

# What sp_columns reports for a column this server can produce. Precision and
# length are what SQL Server states for the same type: length is bytes on the
# wire, precision is digits or characters.
_TYPES = {
    4: ("int", SQL_INTEGER, 10, 4, 0, 10, DBTYPE_I4, True),
    8: ("bigint", SQL_BIGINT, 19, 8, 0, 10, DBTYPE_I8, True),
    2: ("smallint", SQL_INTEGER, 5, 2, 0, 10, DBTYPE_I2, True),
    1: ("tinyint", SQL_INTEGER, 3, 1, 0, 10, DBTYPE_I2, True),
}

# Everything here is nullable, because a JSON record missing a key contributes
# a NULL and nothing declares otherwise.
NULLABLE = 1

_TEXT = NVarChar(128)
_LONG_TEXT = NVarChar(4000)
_SMALLINT = Integer(2)
_INT = Integer(4)


def _column(name: str, kind) -> Column:
    """One column of a catalog rowset.

    Flagged the way SQL Server flags its own, rather than the way a SELECT
    result is flagged. The difference is real: a SELECT of a CAST expression
    carries the computed bit, and a provider reading a catalog rowset that
    claims to be computed is being told something untrue about it.
    """
    return Column(name=name, type=kind, flags=CATALOG_COLUMN_FLAGS)


def _describe(kind):
    """How one column looks to a client.

    (type name, ODBC code, precision, length, scale, radix, DBTYPE, fixed).
    Both code systems, because the ODBC procedures and the OLE DB ones want
    different numbers for the same column.
    """
    if isinstance(kind, Integer):
        return _TYPES.get(kind.width, _TYPES[4])
    if isinstance(kind, Float):
        return ("float", SQL_FLOAT, 53, kind.width, None, 2, DBTYPE_R8, True)
    if isinstance(kind, NVarChar):
        if kind.is_max:
            return ("nvarchar", SQL_WLONGVARCHAR, 0, 0, None, None,
                    DBTYPE_WSTR, False)
        size = kind.max_chars or 0
        return ("nvarchar", SQL_WVARCHAR, size, size * 2, None, None,
                DBTYPE_WSTR, False)
    return ("nvarchar", SQL_WVARCHAR, 4000, 8000, None, None, DBTYPE_WSTR, False)


def _matches(name: str, pattern: object) -> bool:
    """Whether a name satisfies a catalog procedure's filter.

    The filters are SQL LIKE patterns, and a client asking for everything
    sends NULL, an empty string, or "%".
    """
    if pattern is None:
        return True
    text = str(pattern).strip()
    if not text or text == "%":
        return True
    return fnmatch.fnmatch(name.lower(), text.lower().replace("%", "*").replace("_", "?"))


# What each procedure calls its arguments, in order. Read off the real
# procedures rather than assumed, because the variants disagree:
# sp_columns_rowset starts with @table_name and sp_columns_100_rowset2 has no
# @table_name at all, so reading argument 0 as a table name filters the whole
# catalog by the string "dbo" and returns nothing.
SIGNATURES = {
    "sp_tables": ("table_name", "table_owner", "table_qualifier", "table_type"),
    "sp_columns": ("table_name", "table_owner", "table_qualifier",
                   "column_name", "odbcver"),
    "sp_columns_90": ("table_name", "table_owner", "table_qualifier",
                      "column_name", "odbcver"),
    "sp_columns_100": ("table_name", "table_owner", "table_qualifier",
                       "column_name", "odbcver"),
    "sp_columns_managed": ("table_qualifier", "table_owner", "table_name",
                           "column_name", "odbcver"),
    "sp_databases": (),

    "sp_tables_rowset": ("table_name", "table_schema", "table_type"),
    "sp_tables_rowset2": ("table_schema", "table_type"),
    "sp_tables_rowset_rmt": ("table_server", "table_catalog", "table_name",
                             "table_schema", "table_type"),
    "sp_tables_info_rowset": ("table_name", "table_schema", "table_type"),
    "sp_tables_info_rowset2": ("table_schema", "table_type"),
    "sp_tables_info_90_rowset": ("table_name", "table_schema", "table_type"),
    "sp_tables_info_90_rowset2": ("table_schema", "table_type"),
    "sp_columns_rowset": ("table_name", "table_schema", "column_name"),
    "sp_columns_rowset2": ("table_schema", "column_name"),
    "sp_columns_90_rowset": ("table_name", "table_schema", "column_name"),
    "sp_columns_90_rowset2": ("table_schema", "column_name"),
    "sp_columns_100_rowset": ("table_name", "table_schema", "column_name"),
    "sp_columns_100_rowset2": ("table_schema", "column_name"),
    "sp_indexes_rowset": ("table_name", "index_name", "table_schema"),
    "sp_indexes_rowset2": ("index_name", "table_schema"),
    "sp_indexes_90_rowset": ("table_name", "index_name", "table_schema"),
    "sp_indexes_90_rowset2": ("index_name", "table_schema"),
    "sp_indexes_100_rowset": ("table_name", "index_name", "table_schema"),
    "sp_indexes_100_rowset2": ("index_name", "table_schema"),
    "sp_primary_keys_rowset": ("table_name", "table_schema"),
    "sp_primary_keys_rowset2": ("table_schema",),
    "sp_schemata_rowset": ("schema_name", "schema_owner"),
    "sp_catalogs_rowset": ("catalog_name",),
    "sp_column_privileges_rowset": ("table_name", "table_schema",
                                    "column_name", "grantor", "grantee"),
    "sp_column_privileges_rowset2": ("table_schema", "column_name",
                                     "grantor", "grantee"),
    "sp_statistics_rowset": ("table_name", "table_schema"),
    "sp_statistics_rowset2": ("table_schema",),
    "sp_procedures_rowset": ("procedure_name", "procedure_schema"),
    "sp_procedures_rowset2": ("procedure_schema",),
    "sp_procedure_params_rowset": ("procedure_name", "procedure_schema",
                                   "parameter_name"),
    "sp_procedure_params_rowset2": ("procedure_schema", "parameter_name"),
    "sp_procedure_params_90_rowset2": ("procedure_schema", "parameter_name"),
    "sp_procedure_params_100_rowset2": ("procedure_schema", "parameter_name"),
    "sp_foreign_keys_rowset2": ("foreignkey_tab_name",
                                "foreignkey_tab_schema", "pk_table_name",
                                "pk_table_schema", "pk_table_catalog"),
    "sp_foreign_keys_rowset3": ("pk_table_schema", "pk_table_catalog",
                                "foreignkey_tab_schema",
                                "foreignkey_tab_catalog"),
    "sp_provider_types_rowset": ("data_type", "best_match"),
    "sp_provider_types_90_rowset": ("data_type", "best_match"),
    "sp_provider_types_100_rowset": ("data_type", "best_match"),
    "sp_catalogs_rowset2": ("catalog_name",),
    "sp_views_rowset": ("view_name", "view_schema"),
    "sp_views_rowset2": ("view_schema",),
    "sp_table_privileges_rowset": ("table_name", "table_schema", "grantor",
                                   "grantee"),
    "sp_table_privileges_rowset2": ("table_schema", "grantor", "grantee"),
}

# The 64-bit and remote spellings take the same arguments as the ones they
# are spelled after.
for _name, _signature in list(SIGNATURES.items()):
    if _name.endswith("_rowset") or _name.endswith("_rowset2"):
        SIGNATURES.setdefault(_name + "_64", _signature)
        SIGNATURES.setdefault(_name + "_rmt", _signature)


def bind(procedure: str, arguments: list, named: dict) -> dict:
    """Arguments as a map of name to value, however the client passed them.

    ODBC and OLE DB send them positionally, .NET sends them by name, and the
    positions mean different things in different variants of one procedure.
    Binding once, here, means each procedure below reads what it wants by
    name and never counts.
    """
    signature = SIGNATURES.get(normalise(procedure), ())
    bound = dict(zip(signature, arguments))
    for key, value in named.items():
        bound[str(key).lstrip("@").lower()] = value
    return bound


def sp_tables(catalog, given: dict) -> QueryResult:
    """Every table, in the layout ODBC's SQLTables expects."""
    wanted = given.get("table_name")
    kinds = given.get("table_type")

    if kinds is not None and "table" not in str(kinds).lower():
        rows: list[list[object]] = []
    else:
        rows = [
            [CATALOG, SCHEMA, name, "TABLE", None]
            for name in catalog.names
            if _matches(name, wanted)
        ]

    return QueryResult(
        columns=[
            _column("TABLE_QUALIFIER", _TEXT),
            _column("TABLE_OWNER", _TEXT),
            _column("TABLE_NAME", _TEXT),
            _column("TABLE_TYPE", NVarChar(32)),
            _column("REMARKS", _LONG_TEXT),
        ],
        rows=rows,
    )


def sp_columns(catalog, given: dict) -> QueryResult:
    """Every column of the named tables, in ODBC's SQLColumns layout."""
    wanted = given.get("table_name")
    column_filter = given.get("column_name")

    rows: list[list[object]] = []
    for table in catalog.shapes():
        if not _matches(table.name, wanted):
            continue
        for position, column in enumerate(table.columns, start=1):
            if not _matches(column.name, column_filter):
                continue
            (name, code, precision, length, scale, radix,
             _dbtype, _fixed) = _describe(column.type)
            rows.append([
                CATALOG, SCHEMA, table.name, column.name,
                code, name, precision, length, scale, radix,
                NULLABLE, None, None,
                code, None, length if code in (SQL_WVARCHAR, SQL_WLONGVARCHAR) else None,
                position, "YES", None,
            ])

    return QueryResult(
        columns=[
            _column("TABLE_QUALIFIER", _TEXT),
            _column("TABLE_OWNER", _TEXT),
            _column("TABLE_NAME", _TEXT),
            _column("COLUMN_NAME", _TEXT),
            _column("DATA_TYPE", _SMALLINT),
            _column("TYPE_NAME", _TEXT),
            _column("PRECISION", _INT),
            _column("LENGTH", _INT),
            _column("SCALE", _SMALLINT),
            _column("RADIX", _SMALLINT),
            _column("NULLABLE", _SMALLINT),
            _column("REMARKS", _LONG_TEXT),
            _column("COLUMN_DEF", _LONG_TEXT),
            _column("SQL_DATA_TYPE", _SMALLINT),
            _column("SQL_DATETIME_SUB", _SMALLINT),
            _column("CHAR_OCTET_LENGTH", _INT),
            _column("ORDINAL_POSITION", _INT),
            _column("IS_NULLABLE", NVarChar(10)),
            _column("SS_DATA_TYPE", Integer(1)),
        ],
        rows=rows,
    )


def sp_databases(catalog, given: dict) -> QueryResult:
    """The one database this server has."""
    return QueryResult(
        columns=[
            _column("DATABASE_NAME", _TEXT),
            _column("DATABASE_SIZE", _INT),
            _column("REMARKS", _LONG_TEXT),
        ],
        rows=[[CATALOG, 0, None]],
    )


# -- the OLE DB rowsets ---------------------------------------------------
#
# A second family with different layouts, because OLE DB defines its own
# schema rowsets rather than reusing ODBC's. Every layout below was read off
# SQL Server 2025 by executing the procedure and taking its result metadata,
# not from documentation: sp_tables_rowset2 ends with a TABLE_PROPID and two
# datetimes that no summary of it mentions, and its TABLE_GUID is a
# uniqueidentifier. A provider handed nvarchar where it expects one of those
# does not report a mismatch.

_GUID = UniqueIdentifier()
_WHEN = DateTime()
_FLAG = Bit()

def sp_tables_rowset(catalog, given: dict) -> QueryResult:
    """Every table, in the layout MSOLEDBSQL asks for by name."""
    wanted = given.get("table_name")
    kinds = given.get("table_type")

    if kinds is not None and "table" not in str(kinds).lower():
        rows: list[list[object]] = []
    else:
        rows = [
            [CATALOG, SCHEMA, name, "TABLE", None, None, None, None, None]
            for name in catalog.names
            if _matches(name, wanted)
        ]

    return QueryResult(
        columns=[
            _column("TABLE_CATALOG", _TEXT),
            _column("TABLE_SCHEMA", _TEXT),
            _column("TABLE_NAME", _TEXT),
            _column("TABLE_TYPE", NVarChar(30)),
            _column("TABLE_GUID", _GUID),
            _column("DESCRIPTION", NVarChar(1)),
            _column("TABLE_PROPID", _INT),
            _column("DATE_CREATED", _WHEN),
            _column("DATE_MODIFIED", _WHEN),
        ],
        rows=rows,
    )


def sp_tables_info_rowset(catalog, given: dict,
                          with_flags: bool = False) -> QueryResult:
    """The same tables, with the bookmark and cardinality columns.

    The _90_ spellings carry a TABLE_FLAGS column that the others do not, and
    a provider that asked for fifteen columns and got fourteen reports a
    catastrophic failure rather than a mismatch.
    """
    wanted = given.get("table_name")
    rows = [
        [CATALOG, SCHEMA, name, "TABLE", None, False, None, None, None, None,
         None, None, None, None] + ([None] if with_flags else [])
        for name in catalog.names
        if _matches(name, wanted)
    ]
    return QueryResult(
        columns=[
            _column("TABLE_CATALOG", _TEXT),
            _column("TABLE_SCHEMA", _TEXT),
            _column("TABLE_NAME", _TEXT),
            _column("TABLE_TYPE", NVarChar(30)),
            _column("TABLE_GUID", _GUID),
            _column("BOOKMARKS", _FLAG),
            _column("BOOKMARK_TYPE", _INT),
            _column("BOOKMARK_DATATYPE", _SMALLINT),
            _column("BOOKMARK_MAXIMUM_LENGTH", _INT),
            _column("BOOKMARK_INFORMATION", _INT),
            _column("TABLE_VERSION", Integer(8)),
            _column("CARDINALITY", _INT),
            _column("DESCRIPTION", NVarChar(1)),
            _column("TABLE_PROPID", _INT),
        ] + ([_column("TABLE_FLAGS", _INT)] if with_flags else []),
        rows=rows,
    )


def sp_tables_info_90_rowset(catalog, given: dict) -> QueryResult:
    """The tables-info rowset as the 90 spellings declare it."""
    return sp_tables_info_rowset(catalog, given, with_flags=True)


def sp_columns_rowset(catalog, given: dict) -> QueryResult:
    """Every column, in the OLE DB layout rather than the ODBC one.

    Forty-two columns, of which this fills the dozen that mean anything for a
    read-only source. The rest are declared with the types the real procedure
    declares and left NULL, because a provider reads them by position.
    """
    wanted = given.get("table_name")
    column_filter = given.get("column_name")

    rows: list[list[object]] = []
    for table in catalog.shapes():
        if not _matches(table.name, wanted):
            continue
        for position, column in enumerate(table.columns, start=1):
            if not _matches(column.name, column_filter):
                continue
            (_name, code, precision, length, scale, _radix,
             dbtype, fixed) = _describe(column.type)
            textual = code in (SQL_WVARCHAR, SQL_WLONGVARCHAR)
            # OLE DB describes a column with its own flags, not the ODBC
            # nullability code: fixed length matters to it, and everything
            # here is nullable and read-only.
            flags = (DBCOLUMNFLAGS_ISNULLABLE | DBCOLUMNFLAGS_MAYBENULL
                     | (DBCOLUMNFLAGS_ISFIXEDLENGTH if fixed else 0))
            rows.append([
                CATALOG, SCHEMA, table.name, column.name,
                None,                               # COLUMN_GUID
                None,                               # COLUMN_PROPID
                position,
                False,                              # COLUMN_HASDEFAULT
                None,                               # COLUMN_DEFAULT
                flags,                              # COLUMN_FLAGS
                True,                               # IS_NULLABLE
                dbtype,                             # DATA_TYPE
                None,                               # TYPE_GUID
                precision if textual else None,     # CHARACTER_MAXIMUM_LENGTH
                length if textual else None,        # CHARACTER_OCTET_LENGTH
                None if textual else precision,     # NUMERIC_PRECISION
                None if textual else (scale or 0),  # NUMERIC_SCALE
                None,                               # DATETIME_PRECISION
                None, None, None,                   # CHARACTER_SET_*
                None, None, None,                   # COLLATION_*
                None, None, None,                   # DOMAIN_*
                None,                               # DESCRIPTION
                None, None, None,                   # LCID, COMPFLAGS, SORTID
                None,                               # COLUMN_TDSCOLLATION
                False,                              # IS_COMPUTED
                None, None, None,                   # SS_XML_SCHEMACOLLECTION_*
                None, None, None, None,             # SS_UDT_*
                False,                              # SS_IS_SPARSE
                False,                              # SS_IS_COLUMN_SET
            ])

    return QueryResult(
        columns=[
            _column("TABLE_CATALOG", _TEXT),
            _column("TABLE_SCHEMA", _TEXT),
            _column("TABLE_NAME", _TEXT),
            _column("COLUMN_NAME", _TEXT),
            _column("COLUMN_GUID", _GUID),
            _column("COLUMN_PROPID", _INT),
            _column("ORDINAL_POSITION", _INT),
            _column("COLUMN_HASDEFAULT", _FLAG),
            _column("COLUMN_DEFAULT", NVarChar(2000)),
            _column("COLUMN_FLAGS", _INT),
            _column("IS_NULLABLE", _FLAG),
            _column("DATA_TYPE", SmallInt()),
            _column("TYPE_GUID", _GUID),
            _column("CHARACTER_MAXIMUM_LENGTH", _INT),
            _column("CHARACTER_OCTET_LENGTH", _INT),
            _column("NUMERIC_PRECISION", _SMALLINT),
            _column("NUMERIC_SCALE", _SMALLINT),
            _column("DATETIME_PRECISION", _INT),
            _column("CHARACTER_SET_CATALOG", _TEXT),
            _column("CHARACTER_SET_SCHEMA", _TEXT),
            _column("CHARACTER_SET_NAME", _TEXT),
            _column("COLLATION_CATALOG", _TEXT),
            _column("COLLATION_SCHEMA", _TEXT),
            _column("COLLATION_NAME", _TEXT),
            _column("DOMAIN_CATALOG", _TEXT),
            _column("DOMAIN_SCHEMA", _TEXT),
            _column("DOMAIN_NAME", _TEXT),
            _column("DESCRIPTION", NVarChar(1)),
            _column("COLUMN_LCID", _INT),
            _column("COLUMN_COMPFLAGS", _INT),
            _column("COLUMN_SORTID", _INT),
            _column("COLUMN_TDSCOLLATION", Binary(5)),
            _column("IS_COMPUTED", _FLAG),
            _column("SS_XML_SCHEMACOLLECTION_CATALOGNAME", _TEXT),
            _column("SS_XML_SCHEMACOLLECTION_SCHEMANAME", _TEXT),
            _column("SS_XML_SCHEMACOLLECTIONNAME", _TEXT),
            _column("SS_UDT_CATALOGNAME", _TEXT),
            _column("SS_UDT_SCHEMANAME", _TEXT),
            _column("SS_UDT_NAME", _TEXT),
            _column("SS_UDT_ASSEMBLY_TYPENAME", _LONG_TEXT),
            _column("SS_IS_SPARSE", _FLAG),
            _column("SS_IS_COLUMN_SET", _FLAG),
        ],
        rows=rows,
    )


def sp_indexes_rowset(catalog, given: dict) -> QueryResult:
    """No indexes. A table read over HTTP has none to report."""
    return QueryResult(
        columns=[
            _column("TABLE_CATALOG", _TEXT),
            _column("TABLE_SCHEMA", _TEXT),
            _column("TABLE_NAME", _TEXT),
            _column("INDEX_CATALOG", _TEXT),
            _column("INDEX_SCHEMA", _TEXT),
            _column("INDEX_NAME", _TEXT),
            _column("PRIMARY_KEY", _FLAG),
            _column("UNIQUE", _FLAG),
            _column("CLUSTERED", _FLAG),
            _column("TYPE", _SMALLINT),
            _column("FILL_FACTOR", _INT),
            _column("INITIAL_SIZE", _INT),
            _column("NULLS", _INT),
            _column("SORT_BOOKMARKS", _FLAG),
            _column("AUTO_UPDATE", _FLAG),
            _column("NULL_COLLATION", _INT),
            _column("ORDINAL_POSITION", _INT),
            _column("COLUMN_NAME", _TEXT),
            _column("COLUMN_GUID", _GUID),
            _column("COLUMN_PROPID", _INT),
            _column("COLLATION", _SMALLINT),
            _column("CARDINALITY", _INT),
            _column("PAGES", _INT),
            _column("FILTER_CONDITION", NVarChar(None)),
            _column("INTEGRATED", _FLAG),
            _column("STATUS", _INT),
        ],
        rows=[],
    )


def sp_primary_keys_rowset(catalog, given: dict) -> QueryResult:
    """No primary keys. Nothing here declares one."""
    return QueryResult(
        columns=[
            _column("TABLE_CATALOG", _TEXT),
            _column("TABLE_SCHEMA", _TEXT),
            _column("TABLE_NAME", _TEXT),
            _column("COLUMN_NAME", _TEXT),
            _column("COLUMN_GUID", _GUID),
            _column("COLUMN_PROPID", _INT),
            _column("ORDINAL", _INT),
            _column("PK_NAME", _TEXT),
        ],
        rows=[],
    )


def sp_schemata_rowset(catalog, given: dict) -> QueryResult:
    """The one schema everything lives in."""
    return QueryResult(
        columns=[
            _column("CATALOG_NAME", _TEXT),
            _column("SCHEMA_NAME", _TEXT),
            _column("SCHEMA_OWNER", _TEXT),
            _column("DEFAULT_CHARACTER_SET_CATALOG", _TEXT),
            _column("DEFAULT_CHARACTER_SET_SCHEMA", _TEXT),
            _column("DEFAULT_CHARACTER_SET_NAME", _TEXT),
        ],
        rows=[[CATALOG, SCHEMA, "dbo", None, None, None]],
    )


def sp_catalogs_rowset(catalog, given: dict) -> QueryResult:
    """The one catalog."""
    return QueryResult(
        columns=[
            _column("CATALOG_NAME", _TEXT),
            _column("DESCRIPTION", _LONG_TEXT),
        ],
        rows=[[CATALOG, None]],
    )


def sp_views_rowset(catalog, given: dict) -> QueryResult:
    """No views. Every table here is a table."""
    return QueryResult(
        columns=[
            _column("TABLE_CATALOG", _TEXT),
            _column("TABLE_SCHEMA", _TEXT),
            _column("TABLE_NAME", _TEXT),
            _column("VIEW_DEFINITION", _LONG_TEXT),
            _column("CHECK_OPTION", _FLAG),
            _column("IS_UPDATABLE", _FLAG),
            _column("DESCRIPTION", _LONG_TEXT),
            _column("DATE_CREATED", _WHEN),
            _column("DATE_MODIFIED", _WHEN),
        ],
        rows=[],
    )


# The types this server produces, as SQL Server describes the same ones.
# TYPE_NAME, DBTYPE, size, literal prefix and suffix, create params,
# searchable, unsigned, fixed precision, fixed length.
PROVIDER_TYPES = (
    ("smallint", DBTYPE_I2, 5, None, None, None, 3, 0, True, True),
    ("int", DBTYPE_I4, 10, None, None, None, 3, 0, True, True),
    ("bigint", DBTYPE_I8, 19, None, None, None, 3, 0, True, True),
    ("float", DBTYPE_R8, 15, None, None, None, 3, 0, False, True),
    ("bit", DBTYPE_BOOL, 1, None, None, None, 3, None, False, True),
    ("nvarchar", DBTYPE_WSTR, 4000, "N'", "'", "max length", 4, None,
     False, False),
)

# What a client may do with a value of a type. Three is comparable and
# sortable; four adds LIKE, which only the text type supports.
SEARCHABLE_BASIC = 3
SEARCHABLE_LIKE = 4


def sp_provider_types_rowset(catalog, given: dict) -> QueryResult:
    """Every type this server can put in a column."""
    rows = [
        [name, dbtype, size, prefix, suffix, params,
         True,               # IS_NULLABLE
         False,              # CASE_SENSITIVE
         searchable,
         unsigned,
         fixed_precision,
         0 if unsigned is None else 1,   # AUTO_UNIQUE_VALUE
         name,               # LOCAL_TYPE_NAME
         None, None,         # MINIMUM_SCALE, MAXIMUM_SCALE
         None,               # GUID
         None, None,         # TYPELIB, VERSION
         False,              # IS_LONG
         True,               # BEST_MATCH
         fixed_length]
        for (name, dbtype, size, prefix, suffix, params, searchable,
             unsigned, fixed_precision, fixed_length) in PROVIDER_TYPES
    ]
    return QueryResult(
        columns=[
            _column("TYPE_NAME", _TEXT),
            _column("DATA_TYPE", SmallInt()),
            _column("COLUMN_SIZE", _INT),
            _column("LITERAL_PREFIX", NVarChar(32)),
            _column("LITERAL_SUFFIX", NVarChar(32)),
            _column("CREATE_PARAMS", NVarChar(32)),
            _column("IS_NULLABLE", _FLAG),
            _column("CASE_SENSITIVE", _FLAG),
            _column("SEARCHABLE", _INT),
            _column("UNSIGNED_ATTRIBUTE", Integer(1)),
            _column("FIXED_PREC_SCALE", _FLAG),
            _column("AUTO_UNIQUE_VALUE", Integer(1)),
            _column("LOCAL_TYPE_NAME", _TEXT),
            _column("MINIMUM_SCALE", _SMALLINT),
            _column("MAXIMUM_SCALE", _SMALLINT),
            _column("GUID", _GUID),
            _column("TYPELIB", NVarChar(1)),
            _column("VERSION", NVarChar(1)),
            _column("IS_LONG", _FLAG),
            _column("BEST_MATCH", _FLAG),
            _column("IS_FIXEDLENGTH", _FLAG),
        ],
        rows=rows,
    )


def sp_procedures_rowset(catalog, given: dict) -> QueryResult:
    """No stored procedures. This server has catalog procedures and no others,
    and a client should not be offered them as things to execute."""
    return QueryResult(
        columns=[
            _column("PROCEDURE_CATALOG", _TEXT),
            _column("PROCEDURE_SCHEMA", _TEXT),
            _column("PROCEDURE_NAME", NVarChar(134)),
            _column("PROCEDURE_TYPE", _SMALLINT),
            _column("PROCEDURE_DEFINITION", NVarChar(1)),
            _column("DESCRIPTION", NVarChar(1)),
            _column("DATE_CREATED", _WHEN),
            _column("DATE_MODIFIED", _WHEN),
        ],
        rows=[],
    )


def sp_procedure_params_rowset(catalog, given: dict) -> QueryResult:
    """No procedure parameters, because there are no procedures."""
    return QueryResult(
        columns=[
            _column("PROCEDURE_CATALOG", _TEXT),
            _column("PROCEDURE_SCHEMA", _TEXT),
            _column("PROCEDURE_NAME", NVarChar(134)),
            _column("PARAMETER_NAME", _TEXT),
            _column("ORDINAL_POSITION", _SMALLINT),
            _column("PARAMETER_TYPE", _SMALLINT),
            _column("PARAMETER_HASDEFAULT", Integer(1)),
            _column("PARAMETER_DEFAULT", NVarChar(255)),
            _column("IS_NULLABLE", _FLAG),
            _column("DATA_TYPE", _SMALLINT),
            _column("CHARACTER_MAXIMUM_LENGTH", _INT),
            _column("CHARACTER_OCTET_LENGTH", _INT),
            _column("NUMERIC_PRECISION", _SMALLINT),
            _column("NUMERIC_SCALE", _SMALLINT),
            _column("DESCRIPTION", NVarChar(50)),
            _column("TYPE_NAME", _TEXT),
            _column("LOCAL_TYPE_NAME", _TEXT),
            _column("SS_XML_SCHEMACOLLECTION_CATALOGNAME", _TEXT),
            _column("SS_XML_SCHEMACOLLECTION_SCHEMANAME", _TEXT),
            _column("SS_XML_SCHEMACOLLECTIONNAME", _TEXT),
            _column("SS_UDT_CATALOGNAME", _TEXT),
            _column("SS_UDT_SCHEMANAME", _TEXT),
            _column("SS_UDT_NAME", _TEXT),
            _column("SS_UDT_ASSEMBLY_TYPENAME", _LONG_TEXT),
            _column("SS_TYPE_CATALOG_NAME", _TEXT),
            _column("SS_TYPE_SCHEMANAME", _TEXT),
            _column("SS_DATETIME_PRECISION", _INT),
        ],
        rows=[],
    )


def sp_foreign_keys_rowset(catalog, given: dict) -> QueryResult:
    """No foreign keys. Nothing here relates two tables by declaration."""
    return QueryResult(
        columns=[
            _column("PK_TABLE_CATALOG", _TEXT),
            _column("PK_TABLE_SCHEMA", _TEXT),
            _column("PK_TABLE_NAME", _TEXT),
            _column("PK_COLUMN_NAME", _TEXT),
            _column("PK_COLUMN_GUID", _GUID),
            _column("PK_COLUMN_PROPID", _INT),
            _column("FK_TABLE_CATALOG", _TEXT),
            _column("FK_TABLE_SCHEMA", _TEXT),
            _column("FK_TABLE_NAME", _TEXT),
            _column("FK_COLUMN_NAME", _TEXT),
            _column("FK_COLUMN_GUID", _GUID),
            _column("FK_COLUMN_PROPID", _INT),
            _column("ORDINAL", _INT),
            _column("UPDATE_RULE", NVarChar(11)),
            _column("DELETE_RULE", NVarChar(11)),
            _column("PK_NAME", _TEXT),
            _column("FK_NAME", _TEXT),
            _column("DEFERRABILITY", _SMALLINT),
        ],
        rows=[],
    )


def sp_column_privileges_rowset(catalog, given: dict) -> QueryResult:
    """Read on every column, granted to whoever is asking."""
    rows = [
        [None, None, CATALOG, SCHEMA, table.name, column.name, None, None,
         "SELECT", False]
        for table in catalog.shapes()
        if _matches(table.name, given.get("table_name"))
        for column in table.columns
        if _matches(column.name, given.get("column_name"))
    ]
    return QueryResult(
        columns=[
            _column("GRANTOR", _TEXT),
            _column("GRANTEE", _TEXT),
            _column("TABLE_CATALOG", _TEXT),
            _column("TABLE_SCHEMA", _TEXT),
            _column("TABLE_NAME", _TEXT),
            _column("COLUMN_NAME", _TEXT),
            _column("COLUMN_GUID", _GUID),
            _column("COLUMN_PROPID", _INT),
            _column("PRIVILEGE_TYPE", NVarChar(30)),
            _column("IS_GRANTABLE", _FLAG),
        ],
        rows=rows,
    )


def sp_statistics_rowset(catalog, given: dict) -> QueryResult:
    """How many rows each table holds.

    Answered from the shape rather than a full read, so asking a fifty-table
    catalog how big it is does not fetch every page of every collection. The
    count is what the first page held, which is a floor rather than a total,
    so it is left NULL instead of stated wrongly.
    """
    rows = [
        [CATALOG, SCHEMA, name, None]
        for name in catalog.names
        if _matches(name, given.get("table_name"))
    ]
    return QueryResult(
        columns=[
            _column("TABLE_CATALOG", _TEXT),
            _column("TABLE_SCHEMA", _TEXT),
            _column("TABLE_NAME", _TEXT),
            _column("CARDINALITY", _INT),
        ],
        rows=rows,
    )


def sp_table_privileges_rowset(catalog, given: dict) -> QueryResult:
    """Read on everything, granted to whoever is asking."""
    return QueryResult(
        columns=[
            _column("GRANTOR", _TEXT),
            _column("GRANTEE", _TEXT),
            _column("TABLE_CATALOG", _TEXT),
            _column("TABLE_SCHEMA", _TEXT),
            _column("TABLE_NAME", _TEXT),
            _column("PRIVILEGE_TYPE", NVarChar(30)),
            _column("IS_GRANTABLE", _FLAG),
        ],
        rows=[[None, None, CATALOG, SCHEMA, name, "SELECT", False]
              for name in catalog.names],
    )


# Name to implementation. The variants are real: a driver picks one by the
# server version it thinks it is talking to, and sp_columns_100 differs from
# sp_columns only in supporting types this server does not have.
PROCEDURES = {
    "sp_tables": sp_tables,
    # The OLE DB family. The suffixes are version variants of one
    # procedure: a provider picks one by the server version it believes
    # it is talking to, and they differ only in supporting types this
    # server does not have.
    "sp_tables_rowset": sp_tables_rowset,
    "sp_tables_rowset2": sp_tables_rowset,
    "sp_tables_rowset_rmt": sp_tables_rowset,
    "sp_tables_info_rowset": sp_tables_info_rowset,
    "sp_tables_info_rowset2": sp_tables_info_rowset,
    "sp_tables_info_rowset_64": sp_tables_info_rowset,
    "sp_tables_info_rowset2_64": sp_tables_info_rowset,
    "sp_tables_info_90_rowset": sp_tables_info_90_rowset,
    "sp_tables_info_90_rowset2": sp_tables_info_90_rowset,
    "sp_tables_info_90_rowset_64": sp_tables_info_90_rowset,
    "sp_tables_info_90_rowset2_64": sp_tables_info_90_rowset,
    "sp_columns_rowset": sp_columns_rowset,
    "sp_columns_rowset2": sp_columns_rowset,
    "sp_columns_rowset_rmt": sp_columns_rowset,
    "sp_columns_90_rowset": sp_columns_rowset,
    "sp_columns_90_rowset2": sp_columns_rowset,
    "sp_columns_100_rowset": sp_columns_rowset,
    "sp_columns_100_rowset2": sp_columns_rowset,
    "sp_indexes_rowset": sp_indexes_rowset,
    "sp_indexes_rowset2": sp_indexes_rowset,
    "sp_indexes_90_rowset": sp_indexes_rowset,
    "sp_indexes_90_rowset2": sp_indexes_rowset,
    "sp_indexes_100_rowset": sp_indexes_rowset,
    "sp_indexes_100_rowset2": sp_indexes_rowset,
    "sp_primary_keys_rowset": sp_primary_keys_rowset,
    "sp_primary_keys_rowset2": sp_primary_keys_rowset,
    "sp_schemata_rowset": sp_schemata_rowset,
    "sp_catalogs_rowset": sp_catalogs_rowset,
    "sp_catalogs_rowset2": sp_catalogs_rowset,
    "sp_views_rowset": sp_views_rowset,
    "sp_views_rowset2": sp_views_rowset,
    "sp_column_privileges_rowset": sp_column_privileges_rowset,
    "sp_column_privileges_rowset2": sp_column_privileges_rowset,
    "sp_statistics_rowset": sp_statistics_rowset,
    "sp_statistics_rowset2": sp_statistics_rowset,
    "sp_table_statistics_rowset": sp_statistics_rowset,
    "sp_table_statistics2_rowset": sp_statistics_rowset,
    "sp_procedures_rowset": sp_procedures_rowset,
    "sp_procedures_rowset2": sp_procedures_rowset,
    "sp_procedure_params_rowset": sp_procedure_params_rowset,
    "sp_procedure_params_rowset2": sp_procedure_params_rowset,
    "sp_procedure_params_90_rowset": sp_procedure_params_rowset,
    "sp_procedure_params_90_rowset2": sp_procedure_params_rowset,
    "sp_procedure_params_100_rowset": sp_procedure_params_rowset,
    "sp_procedure_params_100_rowset2": sp_procedure_params_rowset,
    "sp_foreign_keys_rowset": sp_foreign_keys_rowset,
    "sp_foreign_keys_rowset2": sp_foreign_keys_rowset,
    "sp_foreign_keys_rowset3": sp_foreign_keys_rowset,
    "sp_provider_types_rowset": sp_provider_types_rowset,
    "sp_provider_types_90_rowset": sp_provider_types_rowset,
    "sp_provider_types_100_rowset": sp_provider_types_rowset,
    "sp_table_privileges_rowset": sp_table_privileges_rowset,
    "sp_table_privileges_rowset2": sp_table_privileges_rowset,
    "sp_columns": sp_columns,
    "sp_columns_90": sp_columns,
    "sp_columns_100": sp_columns,
    "sp_columns_managed": sp_columns,
    "sp_databases": sp_databases,
}


def normalise(name: str) -> str:
    """A procedure name reduced to the part that identifies it.

    Clients qualify these every way there is: sp_tables, sys.sp_tables,
    [sys].sp_tables, master.dbo.sp_tables. All of them mean the same call.

    A trailing ;N is a group number, which SQL Server uses to hold several
    procedures under one name. MSOLEDBSQL asks for sp_foreign_keys_rowset;3,
    and the number is the variant: it means what the name with a 3 on the end
    means. Dropping it instead would answer the wrong shape.
    """
    cleaned = name.strip().replace("[", "").replace("]", "")
    cleaned = cleaned.rsplit(".", 1)[-1].strip().lower()
    stem, semicolon, group = cleaned.partition(";")
    if semicolon and group.strip().isdigit():
        number = group.strip()
        return stem if number == "1" else f"{stem}{number}"
    return stem


def known(name: str) -> bool:
    return normalise(name) in PROCEDURES


def run(name: str, catalog, arguments: list, named: dict) -> QueryResult:
    """Answer one catalog procedure call."""
    return PROCEDURES[normalise(name)](catalog, bind(name, arguments, named))
