"""Boolean expressions, for WHERE clauses.

Small on purpose: comparisons, IS NULL, AND, OR, NOT and parentheses, over
column references, literals and parameters. That is the shape clients send, and
in particular it is exactly what a catalog query looks like:

    (TABLE_CATALOG = @Catalog or (@Catalog is null))
    and (TABLE_SCHEMA = @Owner or (@Owner is null))

Evaluation uses SQL's three-valued logic rather than Python's two, and that is
not a detail. In the clause above every parameter is normally NULL, so each
left-hand comparison is unknown rather than false, and only OR-ing it with a
true IS NULL makes the whole thing true. Treating unknown as false would return
no rows, which looks like an empty database rather than a bug.

A row passes only when the result is true. Unknown does not pass.
"""

from __future__ import annotations

import calendar
import dataclasses
import datetime
import decimal
import math
import os
import socket
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Mapping

from .information_schema import object_id

# Unknown is spelled None here, distinct from the Python False that a genuinely
# false comparison produces.
Unknown = None
Ternary = bool | None


class PredicateError(Exception):
    """An expression this project cannot parse or evaluate."""


_TOKEN = re.compile(
    r"""
      (?P<space>    \s+ )
    | (?P<hex>      0[xX][0-9a-fA-F]+ )
    | (?P<number>   -? \d+ (?: \.\d+ )? (?: [eE][-+]?\d+ )? )
    | (?P<string>   N?' (?: [^'] | '' )* ' )
    | (?P<param>    @ [A-Za-z0-9_@#$]+ )
    | (?P<bracketed> \[ (?: [^\]] | \]\] )* \] )
    | (?P<quoted>   " (?: [^"] | "" )* " )
    | (?P<operator> <> | != | >= | <= | = | < | > | \|\| | [-+*/%&|^] )
    | (?P<punct>    [(),.] )
    | (?P<word>     [A-Za-z_@#][A-Za-z0-9_@#$]* )
    """,
    re.VERBOSE,
)

_KEYWORDS = {
    "AND", "OR", "NOT", "IS", "NULL", "TRUE", "FALSE",
    "LIKE", "IN", "BETWEEN", "ESCAPE",
    "CASE", "WHEN", "THEN", "ELSE", "END", "AS",
}

# What CAST and CONVERT will produce. The names are SQL Server's; the values
# are what this actually serves, so a cast to any integer type is an integer.
# The aggregates, named here because a function call has to be recognised as
# one before anything can say whether the name is known at all. sql.py takes
# its own AGGREGATES from this.
AGGREGATE_NAMES = frozenset({"COUNT", "SUM", "MIN", "MAX", "AVG"})

# What a cast produces when it does not say how wide. SQL Server's default
# for CAST and CONVERT, measured: a 50 character string cast to nvarchar
# comes back with 30 characters.
DEFAULT_CAST_CHARS = 30

# The types that pad what they hold out to their declared width.
FIXED_WIDTH_TYPES = frozenset({"NCHAR", "CHAR"})

CAST_TYPES = {
    "INT": int, "INTEGER": int, "BIGINT": int, "SMALLINT": int,
    "TINYINT": int, "BIT": bool,
    "FLOAT": float, "REAL": float, "DECIMAL": float, "NUMERIC": float,
    "MONEY": float,
    "NVARCHAR": str, "VARCHAR": str, "NCHAR": str, "CHAR": str, "TEXT": str,
    "NTEXT": str, "SYSNAME": str,
    "DATETIME": datetime.datetime, "DATETIME2": datetime.datetime,
    "SMALLDATETIME": datetime.datetime, "DATE": datetime.datetime,
}

# Where a datetime counts from. A number cast to one is days since then,
# which is why CAST(0 AS datetime) is the first of January 1900 rather than
# an error, and why a client writes ISNULL(something, 0) and means "never".
DATETIME_EPOCH = datetime.datetime(1900, 1, 1)


# What SERVERPROPERTY answers. A client asks these before it will show a
# table list, and compares a few of them: SMO reads EDITION to find out
# whether it is talking to Azure, and EngineEdition to find out what kind of
# server this is. The version agrees with @@VERSION, because a client that
# read both and found them different would be right to complain.
SERVER_PROPERTIES = {
    "EDITION": "Developer Edition (64-bit)",
    "ENGINEEDITION": 3,
    "PRODUCTVERSION": "17.0.1000.0",
    "PRODUCTLEVEL": "RTM",
    "PRODUCTMAJORVERSION": "17",
    "PRODUCTMINORVERSION": "0",
    "PRODUCTBUILD": "1000",
    "PRODUCTUPDATELEVEL": None,
    "MACHINENAME": socket.gethostname(),
    "SERVERNAME": socket.gethostname(),
    "COMPUTERNAMEPHYSICALNETBIOS": socket.gethostname(),
    "INSTANCENAME": None,
    "ISCLUSTERED": 0,
    "ISHADRENABLED": 0,
    "ISINTEGRATEDSECURITYONLY": 1,
    "ISSINGLEUSER": 0,
    "ISXTPSUPPORTED": 1,
    "ISFULLTEXTINSTALLED": 0,
    # Everything about the one collation this serves. The numbers are not
    # chosen: they are what SQL Server 2025 reports for a server running
    # SQL_Latin1_General_CP1_CI_AS, which is the collation named above.
    "COLLATION": "SQL_Latin1_General_CP1_CI_AS",
    "COLLATIONID": 872468488,
    "COMPARISONSTYLE": 196609,
    "LCID": 1033,
    "SQLCHARSET": 1,
    "SQLCHARSETNAME": "iso_1",
    "SQLSORTORDER": 52,
    "SQLSORTORDERNAME": "nocase_iso",
    # What separates the parts of a path on the machine this runs on. A
    # client reads it before it takes a directory off a file name, and with
    # nothing to separate on it takes nothing.
    "PATHSEPARATOR": os.sep,
    "BUILDCLRVERSION": "v4.0.30319",
    "LICENSETYPE": "DISABLED",
    "NUMLICENSES": None,
    # The features this does not have. No rather than nothing: a client asks
    # whether a feature is installed and reads the answer as a yes or a no,
    # where NULL is neither, and it is the answer a real server gives only
    # for a property it has never heard of.
    "ISADVANCEDANALYTICSINSTALLED": 0,
    "ISBIGDATACLUSTER": 0,
    "ISLOCALDB": 0,
    "ISPOLYBASEINSTALLED": 0,
    "ISSERVERSUSPENDEDFORSNAPSHOTBACKUP": 0,
    "ISTEMPDBMETADATAMEMORYOPTIMIZED": 0,
    "SUSPENDEDDATABASECOUNT": 0,
    "FILESTREAMCONFIGUREDLEVEL": 0,
    "FILESTREAMEFFECTIVELEVEL": 0,
    # Running, which is what a real server reports whether or not
    # availability groups are enabled on it.
    "HADRMANAGERSTATUS": 1,
    # The resource database's version, which follows the version reported
    # above and is written the way a real server writes it.
    "RESOURCEVERSION": "17.00.1000",
    # This process, which is the one answering.
    "PROCESSID": os.getpid(),
}


# What CONNECTIONPROPERTY answers about the connection asking. A client reads
# net_transport to find out how it got here, and this only ever answers over
# a socket.
CONNECTION_PROPERTIES = {
    "NET_TRANSPORT": "TCP",
    "PHYSICAL_NET_TRANSPORT": "TCP",
    "PROTOCOL_TYPE": "TSQL",
    "AUTH_SCHEME": "NTLM",
    "LOCAL_NET_ADDRESS": "127.0.0.1",
    "CLIENT_NET_ADDRESS": "127.0.0.1",
    "SQLSERVICE": None,
}


def _connection_property(name: object) -> object:
    """One property of this connection, or NULL for one it does not have."""
    return CONNECTION_PROPERTIES.get(_text(name).strip().upper())


# Where a connection's own details are bound, so a function can answer about
# the one asking it. One reserved parameter rather than a dozen, because what
# a connection knows about itself grows and a query never names this.
CONTEXT = "@@__context"


def _about(params: Mapping[str, object]) -> dict:
    """What the connection asking knows about itself, or nothing."""
    found = params.get(CONTEXT) if params else None
    return found if isinstance(found, dict) else {}


def _object_id(about: dict, name: object) -> object:
    """A number for a table this serves, or NULL for anything else.

    A client asks about objects a real server has and this one does not, and
    NULL is the answer that says so; SSMS reads it to decide whether a
    feature is installed. For a table that is served, the number is made from
    the name, so it is the same number every time it is asked.
    """
    wanted = _text(name).replace("[", "").replace("]", "")
    wanted = wanted.rsplit(".", 1)[-1].lower()
    if wanted not in {one.lower() for one in about.get("tables", ())}:
        return None
    # The same number sys.tables reports, because a client reads one and
    # then looks the other up by it.
    return object_id(wanted)


# The two a client uses to reach this server again, as against the ones that
# describe the machine it is on. A real server answers both with the machine
# name and that works, because it listens on every address the name resolves
# to. This one usually listens on one, so the machine name would send a
# client somewhere this is not.
_REACHES_THE_SERVER = frozenset({"SERVERNAME"})

# What COLLATIONPROPERTY answers about the one collation this serves, read
# from SQL Server 2025 running it.
COLLATION_PROPERTIES = {
    "LCID": 1033,
    "CODEPAGE": 1252,
    "COMPARISONSTYLE": 196609,
    "VERSION": 0,
}

# What DATABASEPROPERTYEX answers about the database this serves. The names
# and shapes are a real server's; updateability is this server's own, and a
# client that reads it and hides writes is right to.
DATABASE_PROPERTIES = {
    "UPDATEABILITY": "READ_ONLY",
    "STATUS": "ONLINE",
    "COLLATION": "SQL_Latin1_General_CP1_CI_AS",
    "RECOVERY": "SIMPLE",
    "USERACCESS": "MULTI_USER",
    "ISAUTOCLOSE": 0,
    "ISINSTANDBY": 0,
    "ISREADONLY": 1,
    "LASTGOODCHECKDBTIME": None,
}

# What OBJECTPROPERTYEX answers about a table this serves. A table here is a
# plain one: not a view, not schema-bound, nothing generated.
OBJECT_PROPERTIES = {
    "BASETYPE": "U",
    "ISTABLE": 1,
    "ISUSERTABLE": 1,
    "OWNERID": 1,
    "SCHEMAID": 1,
    "TABLEHASCLUSTINDEX": 0,
    "TABLEHASPRIMARYKEY": 0,
    "TABLEHASINDEX": 0,
}


def _server_property(about: dict, name: object) -> object:
    """One property of the server, or NULL for one it does not have.

    NULL rather than an error for an unknown name, which is what a real
    server answers and what lets a client ask about a feature that may not
    be there.
    """
    wanted = _text(name).strip().upper()
    if wanted in _REACHES_THE_SERVER and about.get("server"):
        return about["server"]
    return SERVER_PROPERTIES.get(wanted)


# What each of these answers, given the connection's own details first. Kept
# apart from FUNCTIONS because these take that as well as their arguments.
CONTEXT_FUNCTIONS = {
    # With nothing to look up, whoever is asking. Given something to look
    # up, nobody: this server keeps no principals, so it cannot say who a
    # sid belongs to, and answering with the caller would name the wrong
    # person as the owner of everything.
    "SUSER_SNAME": lambda about, *rest: about.get("login") if not rest else None,
    "SUSER_NAME": lambda about, *rest: about.get("login") if not rest else None,
    "ORIGINAL_LOGIN": lambda about, *rest: about.get("login"),
    "USER_NAME": lambda about, *rest: about.get("user"),
    "SCHEMA_NAME": lambda about, *rest: about.get("schema"),
    "DB_NAME": lambda about, *rest: about.get("database"),
    "DB_ID": lambda about, *rest: 1,
    "HOST_NAME": lambda about, *rest: about.get("host"),
    "APP_NAME": lambda about, *rest: about.get("app"),
    # Yes, whatever is named. This serves one catalog and a login may name
    # any database to reach it, which the handshake already allows: SSMS
    # connects naming msdb and is served. Saying no to a name the login
    # accepts would be the inconsistent answer, and it is the one that makes
    # a client report that a feature is unavailable for want of permission.
    "HAS_DBACCESS": lambda about, name: 1,
    "OBJECT_ID": lambda about, name, *rest: _object_id(about, name),
    # No roles are kept, so nobody is in one. Claiming otherwise would have a
    # client offer what it cannot do.
    "IS_SRVROLEMEMBER": lambda about, *rest: 0,
    "IS_MEMBER": lambda about, *rest: 0,
    # No, whatever is asked. A real server says yes to a sysadmin here, and
    # SSMS asks before reading the availability views to fill in a database's
    # replication state. This server keeps no server state to look at, so a
    # yes would be followed by a question it cannot answer; no is both true
    # and the answer that has the client skip the question.
    "HAS_PERMS_BY_NAME": lambda about, *rest: 0,
    # One moment for the whole statement; see _now. SYSDATETIME is datetime2
    # on a real server and datetime here, which is the nearest this serves.
    "GETDATE": lambda about, *rest: _now(about),
    "CURRENT_TIMESTAMP": lambda about, *rest: _now(about),
    "SYSDATETIME": lambda about, *rest: _now(about),
    "GETUTCDATE": lambda about, *rest: _utc_now(about),
    "SYSUTCDATETIME": lambda about, *rest: _utc_now(about),
    # No sid for a name, because there are no principals to have one.
    "SID_BINARY": lambda about, *rest: None,
    "SUSER_SID": lambda about, *rest: None,
    "SUSER_ID": lambda about, *rest: None,
    # dbo, which is the only user there is.
    "USER_ID": lambda about, *rest: 1,
    # Policy automation is off, which is what syspolicy_configuration says
    # too, and there are no diagrams because nothing here draws any.
    "FN_SYSPOLICY_IS_AUTOMATION_ENABLED": lambda about, *rest: 0,
    "FN_DIAGRAMOBJECTS": lambda about, *rest: 0,
    # What a collation is, for the one collation this has. The numbers are
    # what SQL Server reports for SQL_Latin1_General_CP1_CI_AS.
    "COLLATIONPROPERTY": lambda about, name, wanted: COLLATION_PROPERTIES.get(
        _text(wanted).strip().upper()
    ),
    # Nothing here is schema-bound, indexed, or anything else a client asks
    # about an object it can see. NULL for an object this does not have,
    # which is what a real server answers and how a client tells.
    # What a client asks about the database it is in. Read-only is the one
    # that is not what a real server usually says, and it is what this is.
    "DATABASEPROPERTYEX": lambda about, name, wanted: DATABASE_PROPERTIES.get(
        _text(wanted).strip().upper()
    ),
    "OBJECTPROPERTY": lambda about, target, wanted: (
        None if _object_id(about, target) is None
        else OBJECT_PROPERTIES.get(_text(wanted).strip().upper(), 0)
    ),
    "OBJECTPROPERTYEX": lambda about, target, wanted: (
        None if _object_id(about, target) is None
        else OBJECT_PROPERTIES.get(_text(wanted).strip().upper(), 0)
    ),
    # Here rather than beside the other functions because one of the
    # properties it answers is how to reach this server, which is something
    # only the connection knows.
    "SERVERPROPERTY": _server_property,
}


def _quotename(value: object, using: str) -> object:
    """A name wrapped in the delimiter asked for, doubling it inside.

    Brackets by default, which is how SSMS builds the urn it identifies a
    server by, and a quote when it asks for one.
    """
    if value is None:
        return None
    closing = {"[": "]", "]": "]"}.get(using, using)
    text = _text(value).replace(closing, closing * 2)
    return f"{'[' if closing == ']' else closing}{text}{closing}"


def _as_datetime(value: object) -> datetime.datetime:
    """A value read as a moment, the way SQL Server reads one.

    A number is days since 1900, whole and fractional. Text is a date
    written out. Anything already a moment is itself.
    """
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime(value.year, value.month, value.day)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return DATETIME_EPOCH + datetime.timedelta(days=float(value))
    return datetime.datetime.fromisoformat(_text(value).strip())


# How SQL Server writes a datetime when nothing says otherwise: the month in
# English, the day and the hour each right-aligned in two, and no seconds.
# Measured, because "Dec 25 2026  1:05AM" has two spaces in it and only one
# of them is obvious.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _written_moment(value: datetime.datetime) -> str:
    hour = value.hour % 12 or 12
    return (f"{_MONTHS[value.month - 1]} {value.day:>2} {value.year} "
            f"{hour:>2}:{value.minute:02d}{'AM' if value.hour < 12 else 'PM'}")


def _text(value: object) -> str:
    if isinstance(value, datetime.datetime):
        return _written_moment(value)
    return "" if value is None else str(value)


def _strict(produce, *arguments):
    """A function that is NULL whenever any argument is.

    Most of the string and maths functions behave this way, including in the
    arguments that are not the value: REPLACE('abc', 'b', NULL) is NULL, not
    'ac'.
    """
    if any(argument is None for argument in arguments):
        return None
    return produce()


# What text has to spell to be read as a whole number: a sign, digits, and
# nothing else. Space around it does not count, and a decimal point does.
_WHOLE_NUMBER = re.compile(r"[+-]?\d+\Z")


def _number(value: object) -> float | int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    try:
        text = str(value).strip()
        if not text:
            # Blank text is zero rather than an error, which is what makes
            # '' + 1 come out as 1 and CAST('' AS int) come out as 0.
            # Measured; it is not what any other language would do.
            return 0
        return int(text) if text.lstrip("-+").isdigit() else float(text)
    except (TypeError, ValueError):
        raise PredicateError(f"{value!r} is not a number") from None


def _as_integer(value: object) -> int:
    """A value read as a whole number, for a column or a cast that is one.

    A number truncates toward zero, so 2.7 and -2.7 are 2 and -2. Text has
    to spell a whole number and is refused otherwise: CAST('2.0' AS int) is
    an error where CAST(2.0 AS int) is 2, and so are '2e2', '1,000' and
    '0x10'. Measured, all of them; the split between a number that rounds
    and text that does not is not a rule anyone would guess.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return 0
    if not _WHOLE_NUMBER.match(text):
        raise PredicateError(f"{value!r} is not a whole number")
    return int(text)


# What SQL Server calls each type in the message it refuses SUBSTRING with.
_ARGUMENT_TYPES = {bool: "bit", int: "int", float: "numeric"}


def _substring(value, start, length):
    """SUBSTRING, counting from one.

    A start before the string is not clamped to it: the characters that would
    have been there still spend the length. SUBSTRING('abc', 0, 2) covers
    positions 0 and 1, of which only 1 exists, and is 'a'.

    A number is refused rather than read as its digits, which is SQL Server
    alone among the string functions: LEFT, LEN, UPPER and CHARINDEX all take
    one. SUBSTRING(12345, 2, 2) is an error there and 23 anywhere that
    stringifies first, so a query written against a real server and run here
    would quietly differ.
    """
    if isinstance(value, (int, float)):
        kind = _ARGUMENT_TYPES[type(value)]
        raise PredicateError(
            f"argument data type {kind} is invalid for argument 1 of "
            f"substring function"
        )
    text = _text(value)
    begin = int(_number(start))
    span = int(_number(length))
    if span < 0:
        raise PredicateError("SUBSTRING was given a negative length")
    end = begin + span - 1
    return text[max(begin - 1, 0):max(end, 0)]


def _replace(value, search, replacement):
    """REPLACE, which does nothing when there is nothing to search for.

    Python replaces an empty string at every position, so REPLACE(abc, empty,
    x) is xaxbxcx there and abc on a real server.
    """
    text, needle = _text(value), _text(search)
    return text if not needle else text.replace(needle, _text(replacement))


def _charindex(needle, hay, start=None):
    """Where one string appears in another, counting from one, or zero.

    An empty needle is nowhere rather than everywhere: SQL Server answers 0,
    where a find of an empty string answers with the position it started at.
    """
    wanted = _text(needle)
    if not wanted:
        return 0
    begin = int(_number(start)) - 1 if start is not None else 0
    return _text(hay).lower().find(wanted.lower(), max(begin, 0)) + 1


def _whole(value, toward):
    """FLOOR and CEILING, which return the type they were given.

    SQL Server declares FLOOR(a float) as float, so it answers 10.0 rather
    than 10, and a client reading the column type sees the difference even
    where the rendered number does not.
    """
    number = _number(value)
    return toward(number) if isinstance(number, int) else float(toward(number))


def _round(value, digits=0):
    """ROUND, which sends a half away from zero rather than to even.

    Python rounds 2.5 to 2 and 3.5 to 4; SQL Server rounds both up.
    """
    number = _number(value)
    places = int(_number(digits))
    scale = decimal.Decimal(10) ** places
    scaled = decimal.Decimal(str(number)) * scale
    rounded = scaled.quantize(decimal.Decimal(1), rounding=decimal.ROUND_HALF_UP)
    result = rounded / scale
    return int(result) if isinstance(number, int) else float(result)


def _bitwise_and(a, b):
    """The bits two whole numbers share.

    SSMS takes @@microsoftversion apart with these to find the major, minor
    and build numbers, and puts a database status back together with the
    other two, so a server that cannot do them reports no version and no
    state.
    """
    return int(_number(a)) & int(_number(b))


def _truncated_divide(a, b):
    """Integer division that truncates toward zero, the way T-SQL does.

    Python floors, so -7 / 2 is -4 there and -3 here.
    """
    quotient = abs(a) // abs(b)
    return -quotient if (a < 0) != (b < 0) else quotient


def _remainder(a, b):
    """The remainder, which takes the sign of the dividend.

    Python takes the sign of the divisor, so -7 % 3 is 2 there and -1 here.
    """
    return a - b * _truncated_divide(a, b)


def _numeric(value):
    """A value as a number, if it is one or can be read as one.

    Returns None when it cannot, so the caller can decide: SQL Server would
    convert the text and fail loudly, which is not the same as concatenating.
    """
    try:
        return _number(value)
    except PredicateError:
        return None


# What a part of a date is called, and the abbreviations SQL Server takes for
# it. Measured, and worth reading twice: y is the day of the year and d is the
# day of the month, which are one letter apart and are not the same thing.
DATE_PARTS = {
    "YEAR": "year", "YY": "year", "YYYY": "year",
    "QUARTER": "quarter", "QQ": "quarter", "Q": "quarter",
    "MONTH": "month", "MM": "month", "M": "month",
    "DAYOFYEAR": "dayofyear", "DY": "dayofyear", "Y": "dayofyear",
    "DAY": "day", "DD": "day", "D": "day",
    "WEEK": "week", "WK": "week", "WW": "week",
    "WEEKDAY": "weekday", "DW": "weekday", "W": "weekday",
    "HOUR": "hour", "HH": "hour",
    "MINUTE": "minute", "MI": "minute", "N": "minute",
    "SECOND": "second", "SS": "second", "S": "second",
    "MILLISECOND": "millisecond", "MS": "millisecond",
}

# The functions whose first argument names a part of a date instead of being
# a value. It is written as a bare word, which would otherwise read as a
# column, so the parser reads it as a name and hands it over as text.
DATE_PART_FUNCTIONS = frozenset({"DATEADD", "DATEDIFF", "DATEPART", "DATENAME"})

# Functions a query writes with no brackets at all, the way it writes a
# column. CURRENT_TIMESTAMP is the standard spelling of GETDATE().
NILADIC_FUNCTIONS = frozenset({"CURRENT_TIMESTAMP"})

# What a datetime holds. Outside it is an overflow rather than a date, which
# is why the day before the first of January 1753 is an error and not a date.
DATETIME_FIRST = datetime.datetime(1753, 1, 1)
DATETIME_LAST = datetime.datetime(9999, 12, 31, 23, 59, 59, 997000)

# A Sunday, to count weeks from: the first of January 1900 was a Monday.
# Weeks begin on Sunday here because @@DATEFIRST is 7, which is what a server
# installed for English reports and what was measured.
_A_SUNDAY = datetime.date(1900, 1, 7).toordinal()

_MONTH_NAMES = ("January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November",
                "December")

# In Python's order, where Monday is nought.
_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
              "Saturday", "Sunday")

# How many milliseconds each part of a time is worth, for the parts that
# divide evenly. The rest are counted on the calendar instead.
_PART_MILLISECONDS = {
    "hour": 3_600_000, "minute": 60_000, "second": 1_000, "millisecond": 1,
}


def _since_1900(moment: datetime.datetime) -> int:
    """Whole milliseconds from the start of 1900, rounded down.

    Down rather than toward zero, so that the count of whole units between
    two moments is the same before 1900 as after it. timedelta normalises to
    a negative day and a positive remainder, which is exactly that.
    """
    delta = moment - DATETIME_EPOCH
    return ((delta.days * 86_400 + delta.seconds) * 1000
            + delta.microseconds // 1000)


def _week_of(moment: datetime.datetime) -> int:
    """Which week of its year this falls in.

    Week one is the one holding the first of January however few days that
    is, and a new week starts at every Sunday after it. Measured: 2026 opens
    on a Thursday, so the fourth is a Sunday and already week two, and the
    thirty-first of December is in week fifty-three.
    """
    into = (datetime.date(moment.year, 1, 1).weekday() + 1) % 7
    return (moment.timetuple().tm_yday + into - 1) // 7 + 1


def _weekday_of(moment: datetime.datetime) -> int:
    """The day of the week, counting Sunday as one."""
    return (moment.weekday() + 1) % 7 + 1


def _months_along(moment: datetime.datetime, months: int) -> datetime.datetime:
    """The same day of the month some months away, held back to a short one.

    A month after the thirty-first of January is the twenty-eighth of
    February, not the third of March, and a year after the twenty-ninth of
    February is the twenty-eighth. Measured, both.
    """
    year, month = divmod(moment.year * 12 + moment.month - 1 + months, 12)
    month += 1
    return moment.replace(
        year=year, month=month,
        day=min(moment.day, calendar.monthrange(year, month)[1]),
    )


def _date_part(part: str, value: object) -> int:
    moment = _as_datetime(value)
    if part == "year":
        return moment.year
    if part == "quarter":
        return (moment.month - 1) // 3 + 1
    if part == "month":
        return moment.month
    if part == "dayofyear":
        return moment.timetuple().tm_yday
    if part == "day":
        return moment.day
    if part == "week":
        return _week_of(moment)
    if part == "weekday":
        return _weekday_of(moment)
    if part == "hour":
        return moment.hour
    if part == "minute":
        return moment.minute
    if part == "second":
        return moment.second
    return moment.microsecond // 1000


def _date_name(part: str, value: object) -> str:
    """The same as DATEPART, written out, and only two of them are words."""
    moment = _as_datetime(value)
    if part == "month":
        return _MONTH_NAMES[moment.month - 1]
    if part == "weekday":
        return _DAY_NAMES[moment.weekday()]
    return str(_date_part(part, moment))


def _date_add(part: str, count: object, value: object) -> datetime.datetime:
    """A date moved along by some number of one part of it.

    The number is truncated toward zero rather than rounded, so a day and
    nine tenths moves one day forward and minus a day and nine tenths moves
    one day back.
    """
    if count is None:
        # Not NULL, which every other argument would give. Measured.
        raise PredicateError(
            "Argument data type NULL is invalid for argument 2 of dateadd "
            "function."
        )
    moment = _as_datetime(value)
    moved = int(_number(count))
    try:
        if part == "year":
            result = _months_along(moment, moved * 12)
        elif part == "quarter":
            result = _months_along(moment, moved * 3)
        elif part == "month":
            result = _months_along(moment, moved)
        elif part == "week":
            result = moment + datetime.timedelta(days=moved * 7)
        elif part in ("day", "dayofyear", "weekday"):
            # The three of them move a date by days. Measured; dayofyear and
            # weekday are parts to read, not units to count in.
            result = moment + datetime.timedelta(days=moved)
        else:
            result = moment + datetime.timedelta(
                milliseconds=moved * _PART_MILLISECONDS[part]
            )
    except (OverflowError, ValueError):
        result = None
    if result is None or not DATETIME_FIRST <= result <= DATETIME_LAST:
        raise PredicateError(
            "Adding a value to a 'datetime' column caused an overflow."
        )
    return result


def _date_diff(part: str, start: object, end: object) -> int:
    """How many boundaries of one part lie between two moments.

    Not elapsed time. A minute before midnight to a minute after it is one
    day, and midnight to a minute before the next is none. The count is what
    a report means by "how many days ago", and it is what SQL Server counts.
    """
    first, last = _as_datetime(start), _as_datetime(end)
    if part == "year":
        count = last.year - first.year
    elif part == "quarter":
        count = ((last.year * 4 + (last.month - 1) // 3)
                 - (first.year * 4 + (first.month - 1) // 3))
    elif part == "month":
        count = (last.year * 12 + last.month) - (first.year * 12 + first.month)
    elif part == "week":
        # Weeks start on Sunday, so this counts the Sundays in between.
        count = ((last.date().toordinal() - _A_SUNDAY) // 7
                 - (first.date().toordinal() - _A_SUNDAY) // 7)
    elif part in ("day", "dayofyear", "weekday"):
        count = (last.date() - first.date()).days
    else:
        worth = _PART_MILLISECONDS[part]
        count = _since_1900(last) // worth - _since_1900(first) // worth
    if not -(2 ** 31) <= count < 2 ** 31:
        raise PredicateError(
            "The datediff function resulted in an overflow. The number of "
            "dateparts separating two date/time instances is too large. Try "
            "to use datediff with a less precise datepart."
        )
    return count


def _end_of_month(value: object, months: object = 0) -> datetime.datetime:
    """The last day of a month, at midnight, some months from a date."""
    moment = _months_along(_as_datetime(value).replace(day=1),
                           int(_number(months if months is not None else 0)))
    return datetime.datetime(
        moment.year, moment.month,
        calendar.monthrange(moment.year, moment.month)[1],
    )


def _now(about: dict) -> datetime.datetime:
    """The moment this statement is being answered at.

    One moment for the whole statement, taken when it arrived, because
    GETDATE() is the same value in every row of one answer on a real server.
    A row judged against its own slightly later now would be a filter that
    moved while it ran.
    """
    return about.get("now") or datetime.datetime.now()


def _utc_now(about: dict) -> datetime.datetime:
    """The same moment, said in UTC, which is what GETUTCDATE answers."""
    return _now(about).astimezone(datetime.timezone.utc).replace(tzinfo=None)


# The functions a query is likely to use on data this serves. Each takes the
# already-evaluated arguments. NULL handling is per function: most of these
# return NULL for a NULL input, which is what SQL Server does.
FUNCTIONS = {
    "LEN": lambda v: None if v is None else len(_text(v).rstrip()),
    "DATALENGTH": lambda v: None if v is None else len(_text(v)),
    "UPPER": lambda v: None if v is None else _text(v).upper(),
    "LOWER": lambda v: None if v is None else _text(v).lower(),
    "LTRIM": lambda v: None if v is None else _text(v).lstrip(),
    "RTRIM": lambda v: None if v is None else _text(v).rstrip(),
    "TRIM": lambda v: None if v is None else _text(v).strip(),
    "REVERSE": lambda v: None if v is None else _text(v)[::-1],
    "LEFT": lambda v, n: _strict(
        lambda: _text(v)[:int(_number(n))], v, n
    ),
    "RIGHT": lambda v, n: _strict(
        lambda: _text(v)[-int(_number(n)):] if int(_number(n)) else "", v, n
    ),
    "SUBSTRING": lambda v, a, b: _strict(lambda: _substring(v, a, b), v, a, b),
    "REPLACE": lambda v, a, b: _strict(lambda: _replace(v, a, b), v, a, b),
    "CHARINDEX": lambda needle, hay, *rest: _strict(
        lambda: _charindex(needle, hay, *rest), needle, hay, *rest
    ),
    "CONCAT": lambda *values: "".join(_text(v) for v in values),
    "QUOTENAME": lambda value, *rest: _quotename(
        value, _text(rest[0]) if rest else "["
    ),
    # Nothing here indexes anything, and saying so is the answer.
    "FULLTEXTSERVICEPROPERTY": lambda name: 0,
    "CONNECTIONPROPERTY": _connection_property,
    "ISNULL": lambda a, b: b if a is None else a,
    "COALESCE": lambda *values: next(
        (v for v in values if v is not None), None
    ),
    "NULLIF": lambda a, b: None if a == b else a,
    "IIF": lambda test, yes, no: yes if test else no,
    "ABS": lambda v: None if v is None else abs(_number(v)),
    "SIGN": lambda v: None if v is None else (
        0 if _number(v) == 0 else (1 if _number(v) > 0 else -1)
    ),
    "FLOOR": lambda v: None if v is None else _whole(v, math.floor),
    "CEILING": lambda v: None if v is None else _whole(v, math.ceil),
    "ROUND": lambda v, *rest: _strict(lambda: _round(v, *rest), v, *rest),
    # POWER also keeps the scale of what it raised; see Call.evaluate.
    "POWER": lambda a, b: _strict(lambda: _number(a) ** _number(b), a, b),
    "SQRT": lambda v: None if v is None else math.sqrt(_number(v)),
    "SPACE": lambda n: " " * int(_number(n)),
    "STR": lambda v, *rest: None if v is None else _text(v),
    # Dates. The first argument of the four that take a part of one is the
    # name of that part, which the parser reads as a word and passes as text.
    "DATEADD": lambda part, count, v: (
        None if v is None else _date_add(part, count, v)),
    "DATEDIFF": lambda part, a, b: (
        None if a is None or b is None else _date_diff(part, a, b)),
    "DATEPART": lambda part, v: None if v is None else _date_part(part, v),
    "DATENAME": lambda part, v: None if v is None else _date_name(part, v),
    "YEAR": lambda v: None if v is None else _as_datetime(v).year,
    "MONTH": lambda v: None if v is None else _as_datetime(v).month,
    "DAY": lambda v: None if v is None else _as_datetime(v).day,
    "EOMONTH": lambda v, *rest: None if v is None else _end_of_month(v, *rest),
}


# What arithmetic does, and what + means depends on its operands: two numbers
# add and anything with text concatenates, which is what SQL Server does with
# a string.
def _plus(a, b):
    """+ concatenates two strings and adds anything else.

    A string beside a number is addition, not concatenation: int outranks
    varchar in SQL Server's type precedence, so the text is converted and
    '1' + 2 is 3. Two strings stay strings.
    """
    if isinstance(a, str) and isinstance(b, str):
        return a + b
    if isinstance(a, str) or isinstance(b, str):
        left, right = _numeric(a), _numeric(b)
        if left is None or right is None:
            raise PredicateError(
                f"cannot add {a!r} and {b!r}: one is text that is not a number"
            )
        return left + right
    return _number(a) + _number(b)


ARITHMETIC = {
    "&": _bitwise_and,
    "|": lambda a, b: int(_number(a)) | int(_number(b)),
    "^": lambda a, b: int(_number(a)) ^ int(_number(b)),
    "+": _plus,
    "||": lambda a, b: _text(a) + _text(b),
    "-": lambda a, b: _number(a) - _number(b),
    "*": lambda a, b: _number(a) * _number(b),
    "/": lambda a, b: _divide(_number(a), _number(b)),
    "%": lambda a, b: _modulo(_number(a), _number(b)),
}


def _divide(a, b):
    if b == 0:
        raise PredicateError("divide by zero error encountered")
    if isinstance(a, int) and isinstance(b, int):
        return _truncated_divide(a, b)
    return a / b


def _modulo(a, b):
    if b == 0:
        raise PredicateError("divide by zero error encountered")
    if isinstance(a, int) and isinstance(b, int):
        return _remainder(a, b)
    return math.fmod(a, b)


@dataclass(frozen=True)
class Token:
    kind: str
    text: str


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    at = 0
    while at < len(text):
        match = _TOKEN.match(text, at)
        if not match:
            raise PredicateError(f"cannot read {text[at:at + 20]!r}")
        at = match.end()
        kind = match.lastgroup
        if kind == "space":
            continue
        value = match.group()
        if kind == "word" and value.upper() in _KEYWORDS:
            kind = "keyword"
            value = value.upper()
        tokens.append(Token(kind, value))
    return tokens


# The expression tree. Each node knows how to evaluate itself against a row,
# which keeps the walk next to the shape it walks.


@dataclass(frozen=True)
class Column:
    """A column reference, with whatever qualified it.

    The qualifier is kept rather than discarded because a join needs it: u.id
    and o.id are two columns. It is tried first and the bare name second, so a
    single-table query writing dbo.people.id still finds id.
    """

    name: str
    qualified: str | None = None

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> object:
        for wanted in (self.qualified, self.name):
            if wanted is None:
                continue
            for key, value in row.items():
                if key.lower() == wanted.lower():
                    return value
        raise PredicateError(f"invalid column name '{self.qualified or self.name}'")


@dataclass(frozen=True)
class Arithmetic:
    """An operator with two operands, or one for a leading minus.

    Division by zero gives NULL rather than raising. SQL Server raises, but a
    query that dies partway through a scan leaves a client with neither an
    answer nor the rows it already had, and this is a read-only bridge.
    """

    operator: str
    left: object
    right: object

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> object:
        left = self.left.evaluate(row, params)
        right = self.right.evaluate(row, params)
        if left is None or right is None:
            return None
        return ARITHMETIC[self.operator](left, right)


@dataclass(frozen=True)
class Negate:
    operand: object

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> object:
        value = self.operand.evaluate(row, params)
        return None if value is None else -_number(value)


@dataclass(frozen=True)
class Case:
    """CASE, in both its forms.

    With an operand each WHEN is compared to it; without one each WHEN is a
    condition in its own right. Both stop at the first that holds, and a CASE
    with nothing matching and no ELSE is NULL.
    """

    branches: tuple
    otherwise: object = None
    operand: object = None

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> object:
        subject = self.operand.evaluate(row, params) if self.operand else None
        for test, result in self.branches:
            if self.operand is not None:
                against = test.evaluate(row, params)
                if subject is not None and against is not None and compare(
                    "=", subject, against
                ):
                    return result.evaluate(row, params)
            elif test.evaluate(row, params) is True:
                return result.evaluate(row, params)
        return self.otherwise.evaluate(row, params) if self.otherwise else None


def converted(value: object, to: str) -> object:
    """A value converted to a named type, the way a cast converts it.

    The conversion on its own, with none of the sizing a cast does after it:
    a union brings the branches of one column to a single type and needs
    exactly this and nothing else. Text comes back unsized, because the
    length of a number written out is not something either branch declared.
    """
    if value is None:
        return None
    convert = CAST_TYPES[to]
    try:
        if convert is int:
            return _as_integer(value)
        if convert is float:
            return float(_number(value))
        if convert is bool:
            return bool(_number(value))
        if convert is datetime.datetime:
            return _as_datetime(value)
    except (PredicateError, ValueError):
        raise PredicateError(f"cannot convert {value!r} to {to}") from None
    return _text(value)


@dataclass(frozen=True)
class Cast:
    """CAST(x AS type) and CONVERT(type, x), which mean the same thing here.

    The size is part of the type rather than decoration: a cast to nvarchar(3)
    produces three characters. Measured against SQL Server, and the two ways
    it can not fit are different. Text is truncated, quietly, which is what
    makes CAST(name AS nvarchar(3)) a way of shortening a column. A number
    that will not fit is an arithmetic overflow instead, because nobody asks
    for the first three digits of a number by casting it.
    """

    operand: object
    to: str
    size: int | None = None

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> object:
        value = self.operand.evaluate(row, params)
        if value is None:
            return None
        text = converted(value, self.to)
        if not isinstance(text, str):
            return text

        width = self.declared_size
        if len(text) > width:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                raise PredicateError(
                    f"arithmetic overflow error converting expression to data "
                    f"type {self.to.lower()}"
                )
            text = text[:width]
        if self.to in FIXED_WIDTH_TYPES and self.size is not None:
            # A char is its declared width whatever it holds.
            text = text.ljust(width)
        return text

    @property
    def declared_size(self) -> int:
        """How many characters this cast produces.

        Thirty when the cast did not say, which is SQL Server's default for
        CAST and CONVERT and is easy to hit by accident: casting a 50
        character name to nvarchar returns 30 of it.
        """
        return DEFAULT_CAST_CHARS if self.size is None else self.size


@dataclass(frozen=True)
class Call:
    """One of the functions in FUNCTIONS, applied to evaluated arguments."""

    function: str
    arguments: tuple

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> object:
        values = [argument.evaluate(row, params) for argument in self.arguments]
        if self.function == "POWER" and None not in values:
            # POWER returns the type of what it raised. A decimal literal
            # keeps its places, so POWER(2.0, 0.5) is 1.4 where
            # POWER(2.0E0, 0.5) is 1.4142...
            places = getattr(self.arguments[0], "places", None)
            raised = _number(values[0]) ** _number(values[1])
            return _round(raised, places) if places is not None else raised
        try:
            if self.function in CONTEXT_FUNCTIONS:
                return CONTEXT_FUNCTIONS[self.function](_about(params), *values)
            return FUNCTIONS[self.function](*values)
        except PredicateError:
            raise
        except TypeError:
            raise PredicateError(
                f"{self.function} was given {len(values)} argument(s), which "
                f"is not a number of them it takes"
            ) from None
        except (ValueError, ArithmeticError) as exc:
            raise PredicateError(f"{self.function}: {exc}") from None


@dataclass(frozen=True)
class Aggregate:
    """An aggregate named in a HAVING, read back from the grouped row.

    A HAVING runs after the groups are formed, so the value it wants has
    already been computed. This looks it up rather than computing it again,
    which is also what keeps HAVING COUNT(*) and SELECT COUNT(*) in agreement.
    """

    function: str
    argument: str

    @property
    def key(self) -> str:
        return f"{self.function}({self.argument})"

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> object:
        for name, value in row.items():
            if name.lower() == self.key.lower():
                return value
        # Named where it is used rather than named as a HAVING: an ORDER BY
        # may reach here too, and a message about the wrong clause sends
        # whoever reads it to the wrong line of their query.
        raise PredicateError(
            f"{self.key} is not in the select list, and this computes an "
            f"aggregate only for the columns that are"
        )


@dataclass(frozen=True)
class Literal:
    """A written value, and how many decimal places it was written with.

    The places matter for one function. SQL Server types 2.0 as decimal(2,1)
    rather than float, and POWER returns its first argument's type, so
    POWER(2.0, 0.5) is 1.4 and POWER(2.0E0, 0.5) is 1.4142... Nothing else
    measured carries the scale through: 1.0 / 3 and 1.5 * 1.5 agree either
    way, so this is tracked and used in the one place it shows.
    """

    value: object
    places: int | None = None

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> object:
        return self.value


@dataclass(frozen=True)
class ParameterRef:
    name: str

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> object:
        for key, value in params.items():
            if key.lstrip("@").lower() == self.name.lstrip("@").lower():
                return value
        # An undeclared parameter is null rather than an error: clients send
        # clauses guarded by IS NULL precisely so an absent value is harmless.
        return None


_COMPARISONS = {
    "=":  lambda a, b: a == b,
    "<>": lambda a, b: a != b,
    "!=": lambda a, b: a != b,
    "<":  lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">":  lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


def collated(value: object) -> object:
    """A value as the declared collation compares it.

    Every text column here is declared SQL_Latin1_General_CP1_CI_AS, sort id
    0x34, which is case-insensitive and accent-sensitive. Comparing text
    exactly would mean telling a client one thing and doing another: WHERE
    name = 'ADA' finds ada on a real server with this collation.

    Trailing spaces go too. SQL Server pads the shorter side of a comparison,
    so 'a' = 'a  ' is true; LIKE is the exception and does its own matching.
    """
    if isinstance(value, str):
        return value.rstrip(" ").casefold()
    return value


def compare(operator: str, left: object, right: object) -> bool:
    """One comparison, under the collation and SQL's type coercion.

    Text beside a number converts to a number rather than the other way
    round, because int outranks varchar in SQL Server's type precedence: the
    rule that makes rank IN (1, '2') match a rank of 2, and the same one that
    makes '1' + 2 into 3.
    """
    if isinstance(left, str) != isinstance(right, str):
        as_numbers = (_numeric(left), _numeric(right))
        if None not in as_numbers:
            return _COMPARISONS[operator](*as_numbers)

    left, right = collated(left), collated(right)
    try:
        return _COMPARISONS[operator](left, right)
    except TypeError:
        # Two values of kinds that cannot be ordered against each other. Text
        # is the last resort rather than an error, the way SQL converts.
        return _COMPARISONS[operator](str(left), str(right))


# LIKE, translated to a regular expression once and kept. A pattern usually
# comes from the query rather than the data, so the same one is used for every
# row of a scan.
@lru_cache(maxsize=256)
def like_pattern(pattern: str, escape: str | None) -> re.Pattern:
    """A LIKE pattern as a regular expression.

    T-SQL wildcards: % is any run of characters, _ is exactly one, and a
    bracketed set is one character from it, negated with a leading ^. An
    escape character, when the query names one, makes the next character
    literal whatever it is.
    """
    out = ["\\A"]
    at = 0
    while at < len(pattern):
        char = pattern[at]
        if escape and char == escape and at + 1 < len(pattern):
            out.append(re.escape(pattern[at + 1]))
            at += 2
            continue
        if char == "%":
            out.append(".*")
        elif char == "_":
            out.append(".")
        elif char == "[":
            end = pattern.find("]", at + 1)
            if end < 0:
                out.append(re.escape(char))
            else:
                body = pattern[at + 1:end]
                negated = body.startswith("^")
                if negated:
                    body = body[1:]
                # The body is a set of characters and ranges. Only ] and \
                # need escaping inside one.
                body = body.replace("\\", "\\\\").replace("]", "\\]")
                out.append(f"[{'^' if negated else ''}{body}]")
                at = end + 1
                continue
        else:
            out.append(re.escape(char))
        at += 1
    out.append("\\Z")
    return re.compile("".join(out), re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class Like:
    """value LIKE pattern, with the wildcards T-SQL defines."""

    operand: object
    pattern: object
    negated: bool = False
    escape: object = None

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> Ternary:
        value = self.operand.evaluate(row, params)
        pattern = self.pattern.evaluate(row, params)
        if value is None or pattern is None:
            return Unknown
        escape = None
        if self.escape is not None:
            found = self.escape.evaluate(row, params)
            escape = str(found)[:1] if found else None
        matched = bool(like_pattern(str(pattern), escape).match(str(value)))
        return not matched if self.negated else matched


@dataclass(frozen=True)
class In:
    """value IN (a, b, c).

    Unknown rather than false when the value is not found and one of the
    candidates is NULL, because NULL might have been the match.
    """

    operand: object
    values: tuple
    negated: bool = False

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> Ternary:
        value = self.operand.evaluate(row, params)
        if value is None:
            return Unknown
        unknown = False
        for candidate in self.values:
            found = candidate.evaluate(row, params)
            # A subquery binds the whole column to one parameter, so a single
            # entry in the list may itself be many values.
            offered = found if isinstance(found, (list, tuple)) else [found]
            for other in offered:
                if other is None:
                    unknown = True
                elif compare("=", value, other):
                    return not self.negated
        if unknown:
            return Unknown
        return self.negated


@dataclass(frozen=True)
class Between:
    """value BETWEEN low AND high, which is inclusive at both ends."""

    operand: object
    low: object
    high: object
    negated: bool = False

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> Ternary:
        value = self.operand.evaluate(row, params)
        low = self.low.evaluate(row, params)
        high = self.high.evaluate(row, params)
        if value is None or low is None or high is None:
            return Unknown
        inside = compare(">=", value, low) and compare("<=", value, high)
        return not inside if self.negated else inside


@dataclass(frozen=True)
class Comparison:
    left: object
    operator: str
    right: object

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> Ternary:
        left = self.left.evaluate(row, params)
        right = self.right.evaluate(row, params)
        if left is None or right is None:
            return Unknown
        return compare(self.operator, left, right)


@dataclass(frozen=True)
class IsNull:
    operand: object
    negated: bool = False

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> Ternary:
        # IS NULL is the one test that is never unknown: that is its purpose.
        is_null = self.operand.evaluate(row, params) is None
        return not is_null if self.negated else is_null


@dataclass(frozen=True)
class And:
    left: object
    right: object

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> Ternary:
        left = self.left.evaluate(row, params)
        if left is False:
            return False          # false and anything is false, even unknown
        right = self.right.evaluate(row, params)
        if right is False:
            return False
        if left is Unknown or right is Unknown:
            return Unknown
        return True


@dataclass(frozen=True)
class Or:
    left: object
    right: object

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> Ternary:
        left = self.left.evaluate(row, params)
        if left is True:
            return True           # true or anything is true, even unknown
        right = self.right.evaluate(row, params)
        if right is True:
            return True
        if left is Unknown or right is Unknown:
            return Unknown
        return False


@dataclass(frozen=True)
class Not:
    operand: object

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> Ternary:
        value = self.operand.evaluate(row, params)
        return Unknown if value is Unknown else not value


class _Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.at = 0

    def peek(self) -> Token | None:
        return self.tokens[self.at] if self.at < len(self.tokens) else None

    def take(self) -> Token:
        token = self.peek()
        if token is None:
            raise PredicateError("expression ended early")
        self.at += 1
        return token

    def accept(self, kind: str, text: str | None = None) -> Token | None:
        token = self.peek()
        if token and token.kind == kind and (text is None or token.text == text):
            return self.take()
        return None

    def parse(self) -> object:
        node = self.parse_or()
        if self.peek():
            raise PredicateError(f"unexpected {self.peek().text!r} in the condition")
        return node

    def parse_or(self) -> object:
        node = self.parse_and()
        while self.accept("keyword", "OR"):
            node = Or(node, self.parse_and())
        return node

    def parse_and(self) -> object:
        node = self.parse_not()
        while self.accept("keyword", "AND"):
            node = And(node, self.parse_not())
        return node

    def parse_not(self) -> object:
        if self.accept("keyword", "NOT"):
            return Not(self.parse_not())
        return self.parse_primary()

    def parse_primary(self) -> object:
        if self.accept("punct", "("):
            node = self.parse_or()
            if not self.accept("punct", ")"):
                raise PredicateError("a bracket was opened and not closed")
            return node

        left = self.parse_operand()

        if self.accept("keyword", "IS"):
            negated = bool(self.accept("keyword", "NOT"))
            if not self.accept("keyword", "NULL"):
                raise PredicateError("IS must be followed by NULL or NOT NULL")
            return IsNull(left, negated)

        # NOT sits between the value and the operator in these three, unlike
        # every other place it appears.
        negated = bool(self.accept("keyword", "NOT"))
        for keyword, build in (
            ("LIKE", self.parse_like),
            ("IN", self.parse_in),
            ("BETWEEN", self.parse_between),
        ):
            if self.accept("keyword", keyword):
                return build(left, negated)
        if negated:
            raise PredicateError(
                "NOT here must be followed by LIKE, IN or BETWEEN"
            )

        operator = self.accept("operator")
        if not operator:
            token = self.peek()
            raise PredicateError(
                f"expected a comparison after {getattr(left, 'name', left)!r}"
                + (f", found {token.text!r}" if token else "")
            )
        return Comparison(left, operator.text, self.parse_operand())

    def parse_like(self, left: object, negated: bool) -> object:
        pattern = self.parse_operand()
        escape = self.parse_operand() if self.accept("keyword", "ESCAPE") else None
        return Like(left, pattern, negated, escape)

    def parse_in(self, left: object, negated: bool) -> object:
        if not self.accept("punct", "("):
            raise PredicateError("IN must be followed by a bracketed list")
        values = []
        while True:
            values.append(self.parse_operand())
            if self.accept("punct", ","):
                continue
            if self.accept("punct", ")"):
                break
            token = self.peek()
            raise PredicateError(
                "IN list expects a comma or a closing bracket"
                + (f", found {token.text!r}" if token else "")
            )
        if not values:
            raise PredicateError("IN needs at least one value")
        return In(left, tuple(values), negated)

    def parse_between(self, left: object, negated: bool) -> object:
        low = self.parse_operand()
        if not self.accept("keyword", "AND"):
            raise PredicateError("BETWEEN needs AND between its two bounds")
        return Between(left, low, self.parse_operand(), negated)

    def parse_operand(self) -> object:
        """An expression that produces a value rather than a truth."""
        node = self.parse_term()
        while True:
            operator = self.peek()
            if (operator and operator.kind == "operator"
                    and operator.text in ("+", "-", "||", "&", "|", "^")):
                self.take()
                node = Arithmetic(operator.text, node, self.parse_term())
                continue
            return node

    def parse_term(self) -> object:
        node = self.parse_unary()
        while True:
            operator = self.peek()
            if (operator and operator.kind == "operator"
                    and operator.text in ("*", "/", "%")):
                self.take()
                node = Arithmetic(operator.text, node, self.parse_unary())
                continue
            return node

    def parse_unary(self) -> object:
        operator = self.peek()
        if operator and operator.kind == "operator" and operator.text in ("-", "+"):
            self.take()
            operand = self.parse_unary()
            return Negate(operand) if operator.text == "-" else operand
        return self.parse_value()

    def parse_case(self) -> object:
        """CASE, either compared against an operand or a run of conditions."""
        operand = None
        if not (self.peek() and self.peek().kind == "keyword"
                and self.peek().text == "WHEN"):
            operand = self.parse_operand()

        branches = []
        while self.accept("keyword", "WHEN"):
            test = self.parse_operand() if operand is not None else self.parse_or()
            if not self.accept("keyword", "THEN"):
                raise PredicateError("each WHEN in a CASE needs a THEN")
            branches.append((test, self.parse_operand()))
        if not branches:
            raise PredicateError("a CASE needs at least one WHEN")

        otherwise = self.parse_operand() if self.accept("keyword", "ELSE") else None
        if not self.accept("keyword", "END"):
            raise PredicateError("a CASE must be closed with END")
        return Case(tuple(branches), otherwise, operand)

    def parse_cast(self, reversed_arguments: bool) -> object:
        """CAST(x AS type), or CONVERT(type, x), which says it the other way."""
        if not self.accept("punct", "("):
            raise PredicateError("CAST and CONVERT need brackets")
        if reversed_arguments:
            to, size = self._type_name()
            if not self.accept("punct", ","):
                raise PredicateError("CONVERT needs a comma after the type")
            operand = self.parse_operand()
            while self.accept("punct", ","):        # a style, which is ignored
                self.parse_operand()
        else:
            operand = self.parse_operand()
            if not self.accept("keyword", "AS"):
                raise PredicateError("CAST needs AS between the value and the type")
            to, size = self._type_name()
        if not self.accept("punct", ")"):
            raise PredicateError("CAST( was opened and not closed")
        return Cast(operand, to, size)

    def _type_name(self) -> tuple[str, int | None]:
        token = self.take()
        name = token.text.upper()
        if name not in CAST_TYPES:
            raise PredicateError(
                f"'{token.text}' is not a type this converts to; it has "
                f"{', '.join(sorted(set(CAST_TYPES)))}"
            )
        size = None
        if self.accept("punct", "("):
            first = self.peek()
            if first is not None and first.kind == "number":
                try:
                    size = int(first.text)
                except ValueError:
                    size = None
            while not self.accept("punct", ")"):
                if self.peek() is None:
                    raise PredicateError(f"{name}( was opened and not closed")
                self.take()
        # MAX is spelled as a word, so it parses as no size at all, which is
        # what it means here: nothing is truncated.
        return name, size

    def parse_value(self) -> object:
        token = self.take()

        if token.kind == "hex":
            # A binary literal, which SSMS writes for a version mask and for
            # a status it wants back as an int.
            return Literal(int(token.text, 16))
        if token.kind == "number":
            text = token.text
            if any(c in text for c in "eE"):
                return Literal(float(text))          # a float literal
            if "." in text:
                return Literal(float(text), len(text.rsplit(".", 1)[1]))
            return Literal(int(text))
        if token.kind == "string":
            body = token.text.lstrip("Nn")[1:-1]
            return Literal(body.replace("''", "'"))
        if token.kind == "param":
            return ParameterRef(token.text)
        if token.kind == "bracketed":
            return self._qualified(token.text[1:-1].replace("]]", "]"))
        if token.kind == "quoted":
            return self._qualified(token.text[1:-1].replace('""', '"'))
        if token.kind == "keyword" and token.text in ("TRUE", "FALSE"):
            return Literal(token.text == "TRUE")
        if token.kind == "keyword" and token.text == "NULL":
            return Literal(None)
        if token.kind == "keyword" and token.text == "CASE":
            return self.parse_case()
        if token.kind == "word" and token.text.upper() in ("CAST", "CONVERT"):
            return self.parse_cast(token.text.upper() == "CONVERT")
        if token.kind == "punct" and token.text == "(":
            inner = self.parse_operand()
            if not self.accept("punct", ")"):
                raise PredicateError("a bracket was opened and not closed")
            return inner
        if token.kind == "word":
            following = self.peek()
            if following and following.kind == "punct" and following.text == "(":
                name = token.text.upper()
                if name in FUNCTIONS or name in CONTEXT_FUNCTIONS:
                    return self._call(name)
                return self._aggregate(token.text)
            if token.text.upper() in NILADIC_FUNCTIONS:
                # Written with no brackets, the way a column is, so it has to
                # be recognised here or it reads as one and answers NULL.
                return Call(token.text.upper(), ())
            return self._qualified(token.text)

        raise PredicateError(f"expected a value, found {token.text!r}")

    def parse_argument(self) -> object:
        """An argument, which may be a condition rather than a value.

        IIF takes one, and there is no way to tell which a function wants
        without knowing the function, so both are tried. The condition first,
        because a value that happens to parse as one is a comparison and
        should be read as what it says.
        """
        mark = self.at
        try:
            return self.parse_or()
        except PredicateError:
            self.at = mark
            return self.parse_operand()

    def _call(self, function: str) -> Call:
        self.take()                                   # the opening bracket
        arguments = []
        if function in DATE_PART_FUNCTIONS:
            arguments.append(Literal(self._part_of_a_date(function)))
            if not self.accept("punct", ","):
                raise PredicateError(
                    f"{function} needs a comma after the part of the date it "
                    f"is asked about"
                )
        if not self.accept("punct", ")"):
            while True:
                arguments.append(self.parse_argument())
                if self.accept("punct", ","):
                    continue
                if self.accept("punct", ")"):
                    break
                raise PredicateError(f"{function}( was opened and not closed")
        return Call(function, tuple(arguments))

    def _part_of_a_date(self, function: str) -> str:
        """The bare word naming a part of a date, as its full name.

        A word rather than a string, so it cannot be read as a value: year
        would be a column, and 3 would be nothing at all.
        """
        token = self.peek()
        if token is None or token.kind not in ("word", "keyword"):
            raise PredicateError(
                f"{function} needs the part of the date it is asked about, "
                f"as in {function}(year, ...)"
            )
        self.take()
        part = DATE_PARTS.get(token.text.upper())
        if part is None:
            raise PredicateError(
                f"'{token.text}' is not a recognized datepart option."
            )
        return part

    def _aggregate(self, function: str) -> Aggregate:
        """An aggregate call, read back from the row a group produced."""
        if function.upper() not in AGGREGATE_NAMES:
            raise PredicateError(
                f"'{function}' is not a function this server knows; it has "
                f"{', '.join(sorted(set(FUNCTIONS) | set(CONTEXT_FUNCTIONS)))} "
                f"and the aggregates "
                f"{', '.join(sorted(AGGREGATE_NAMES))}"
            )
        self.take()                                   # the opening bracket
        argument = "*"
        if self.accept("operator", "*"):
            pass
        else:
            token = self.peek()
            if token and not (token.kind == "punct" and token.text == ")"):
                inner = self.parse_operand()
                argument = getattr(inner, "name", None) or str(
                    getattr(inner, "value", "")
                )
        if not self.accept("punct", ")"):
            raise PredicateError(f"{function}( was opened and not closed")
        return Aggregate(function.upper(), argument)

    def _qualified(self, name: str) -> object:
        """Read a dotted reference, keeping both the last part and the whole.

        A call is a reference too: a client writes a function by its whole
        name, msdb.dbo.fn_syspolicy_is_automation_enabled(), and the database
        and schema in front of it say where it lives rather than what it is.
        """
        parts = [name]
        while self.accept("punct", "."):
            part = self.take()
            if part.kind == "bracketed":
                parts.append(part.text[1:-1].replace("]]", "]"))
            elif part.kind == "quoted":
                parts.append(part.text[1:-1].replace('""', '"'))
            else:
                parts.append(part.text)

        following = self.peek()
        if following and following.kind == "punct" and following.text == "(":
            called = parts[-1].upper()
            if called in FUNCTIONS or called in CONTEXT_FUNCTIONS:
                return self._call(called)
            raise PredicateError(
                f"'{parts[-1]}' is not a function this server knows"
            )

        # Only the last two matter: a joined column is named alias.column, and
        # anything in front of that is a schema or database.
        return Column(parts[-1], ".".join(parts[-2:]) if len(parts) > 1 else None)


def _mentions(node: object, kinds: tuple) -> bool:
    """Whether any part of an expression is one of these node types.

    Walked over the dataclass fields rather than case by case, so a node type
    added later is covered without being listed here.
    """
    if isinstance(node, kinds):
        return True
    if dataclasses.is_dataclass(node):
        return any(
            _mentions(getattr(node, field.name), kinds)
            for field in dataclasses.fields(node)
        )
    if isinstance(node, (list, tuple)):
        return any(_mentions(part, kinds) for part in node)
    return False


@dataclass(frozen=True)
class Deferred:
    """A value somebody else works out, once per row, when asked.

    What a correlated subquery becomes. The expression layer has no idea what
    a catalog is and should not learn: it holds a name for the error messages
    and a function, and asks that function about the row in front of it.
    """

    name: str
    produce: object

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> object:
        return self.produce(row, params)


def rewrite(node: object, change) -> object:
    """A copy of an expression with change applied to every part of it.

    change is given each node and returns a replacement or None to leave it
    alone. Walked over the dataclass fields rather than case by case, so a
    node type added later is rebuilt without being listed here.
    """
    replacement = change(node)
    if replacement is not None:
        return replacement
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        return dataclasses.replace(node, **{
            field.name: rewrite(getattr(node, field.name), change)
            for field in dataclasses.fields(node)
            if field.init
        })
    if isinstance(node, tuple):
        return tuple(rewrite(part, change) for part in node)
    if isinstance(node, list):
        return [rewrite(part, change) for part in node]
    return node


def with_deferred(node: object, deferred: dict) -> object:
    """The same expression with named parameters standing for deferred values."""

    def change(part):
        if isinstance(part, ParameterRef):
            return deferred.get(part.name.lstrip("@").lower())
        return None

    return rewrite(node, change)


def as_parameters(node: object, named: dict) -> object:
    """The same expression with the named columns read as parameters instead.

    How a subquery stops reading the row around it and starts reading a value
    bound for it: the reference is resolved once per outer row and handed in.
    """

    def change(part):
        if isinstance(part, Column):
            wanted = (part.qualified or part.name).lower()
            if wanted in named:
                return ParameterRef(named[wanted])
        return None

    return rewrite(node, change)


def _all_of(node: object, kind: type) -> list:
    """Every node of one type inside an expression, in no particular order."""
    found: list = []
    if isinstance(node, kind):
        found.append(node)
    elif dataclasses.is_dataclass(node):
        for field in dataclasses.fields(node):
            found.extend(_all_of(getattr(node, field.name), kind))
    elif isinstance(node, (list, tuple)):
        for part in node:
            found.extend(_all_of(part, kind))
    return found


def columns_in(node: object) -> list:
    """Every column reference an expression makes."""
    return _all_of(node, Column)


def aggregates_in(node: object) -> list:
    """Every aggregate an expression names."""
    return _all_of(node, Aggregate)


def reads_the_row(node: object) -> bool:
    """Whether an expression takes anything from the row it is given.

    What decides whether a select-list entry has to be grouped: an entry that
    reads no column reports the same value for every row, so it can stand
    beside an aggregate. A scalar subquery is such an entry once it has been
    lifted, because what is left of it is a parameter.
    """
    return _mentions(node, (Column, Aggregate, Deferred))


# What each function returns, whatever it was given. A name mapped to an int
# is the argument whose type it takes instead: ABS(a float) is a float and
# ABS(an int) is an int, and ISNULL takes the type of the value it replaces.
FUNCTION_KINDS: dict[str, object] = {
    "LEN": int, "DATALENGTH": int, "CHARINDEX": int, "SIGN": int,
    "UPPER": str, "LOWER": str, "LTRIM": str, "RTRIM": str, "TRIM": str,
    "REVERSE": str, "LEFT": str, "RIGHT": str, "SUBSTRING": str,
    "REPLACE": str, "CONCAT": str, "SPACE": str, "STR": str,
    "SQRT": float, "POWER": float,
    "ABS": 0, "FLOOR": 0, "CEILING": 0, "ROUND": 0,
    "ISNULL": 0, "COALESCE": 0, "NULLIF": 0, "IIF": 1,
    "YEAR": int, "MONTH": int, "DAY": int,
    "DATEPART": int, "DATEDIFF": int, "DATENAME": str,
    "DATEADD": datetime.datetime, "EOMONTH": datetime.datetime,
    "GETDATE": datetime.datetime, "GETUTCDATE": datetime.datetime,
    "SYSDATETIME": datetime.datetime, "SYSUTCDATETIME": datetime.datetime,
    "CURRENT_TIMESTAMP": datetime.datetime,
}


def result_kind(node: object, columns: dict | None = None) -> type | None:
    """The type an expression produces, or None where it cannot be said.

    Only consulted when a computed column has no values to be read from,
    which is the one case where they cannot decide: every value is NULL, or
    the WHERE kept no rows at all, and a real server still declares
    CAST(NULL AS int) an int because the cast says so.

    columns maps the names in scope to what they hold, so an expression over
    a table can be answered too: score * 2 is a float because score is one.
    Without it a column says nothing.

    Deliberately not a type system. It says what it is sure of and stops, and
    None is a legitimate answer that costs a text column of NULLs.
    """
    if isinstance(node, Column):
        return _column_kind(node, columns or {})
    if isinstance(node, Literal):
        # A written NULL comes back as NoneType, which is not the same answer
        # as None: it says the value has no type of its own yet and will take
        # one from whatever it is used with. None says nothing is known.
        return type(node.value)
    if isinstance(node, Cast):
        return CAST_TYPES.get(node.to)
    if isinstance(node, Negate):
        return result_kind(node.operand, columns)
    if isinstance(node, Aggregate):
        # COUNT is a count whatever it counted. The rest are the type of
        # what they reduced, which is a column, so unknown here.
        return int if node.function == "COUNT" else None
    if isinstance(node, Case):
        results = [result for _, result in node.branches]
        if node.otherwise is not None:
            results.append(node.otherwise)
        return _one_kind([result_kind(part, columns) for part in results])
    if isinstance(node, Call):
        return _call_kind(node, columns)
    if isinstance(node, Arithmetic):
        return _arithmetic_kind(node, columns)
    return None


def _one_kind(kinds: list) -> type | None:
    """The type several branches agree on, widening int beside float.

    A branch that is only NULL decides nothing and takes the type of the
    others, which is how ISNULL(NULL, 1) is an int. One branch whose type is
    unknown makes the whole thing unknown, because it could be anything.
    """
    if any(kind is None for kind in kinds):
        return None
    typed = [kind for kind in kinds if kind is not type(None)]
    if not typed:
        return type(None)
    if all(kind is typed[0] for kind in typed):
        return typed[0]
    if all(kind in (int, float) for kind in typed):
        return float
    return None


def _column_kind(node, columns: dict) -> type | None:
    """What a named column holds, by its qualified name and then its bare one.

    The same order the row itself is read in, so u.name and name reach the
    same column here as they do there.
    """
    for wanted in (node.qualified, node.name):
        if wanted and wanted.lower() in columns:
            return columns[wanted.lower()]
    if node.qualified:
        _, dot, bare = node.qualified.lower().rpartition(".")
        if dot and bare in columns:
            return columns[bare]
    return None


def _call_kind(node, columns: dict | None = None) -> type | None:
    declared = FUNCTION_KINDS.get(node.function.upper())
    if declared is None:
        return None
    if isinstance(declared, int):
        # The type of one of its arguments rather than one of its own.
        at = declared
        if at >= len(node.arguments):
            return None
        if node.function.upper() in ("ISNULL", "COALESCE", "NULLIF"):
            return _one_kind(
                [result_kind(a, columns) for a in node.arguments]
            )
        return result_kind(node.arguments[at], columns)
    return declared


def _arithmetic_kind(node, columns: dict | None = None) -> type | None:
    """Arithmetic over numbers is a number; over text, + is text.

    A NULL beside a number takes the number's type, which is what SQL Server
    does with 1 + NULL: the untyped side becomes an int and so does the sum.
    One side whose type is unknown makes the answer unknown, rather than the
    other side's: a column times 2 is whatever that column holds.
    """
    sides = [result_kind(node.left, columns),
             result_kind(node.right, columns)]
    if any(kind is None for kind in sides):
        return None
    typed = [kind for kind in sides if kind is not type(None)]
    if not typed:
        return type(None)
    if str in typed:
        # Concatenation, or an addition SQL Server would refuse; either way
        # this is not the place to decide it.
        return str if node.operator == "+" and all(k is str for k in typed) else None
    return float if float in typed else int


def is_constant(node: object) -> bool:
    """Whether an expression works out the same for every query.

    A parameter counts as varying: SQL Server refuses ORDER BY @p with its own
    message about variables rather than the one about constants, and a lifted
    subquery is a parameter that ORDER BY does take. So does a deferred
    value, which is a different answer for every row by construction.
    """
    return not _mentions(node, (Column, ParameterRef, Aggregate, Deferred))


def parse_expression(text: str) -> object:
    """Parse a value expression: arithmetic, CASE, CAST, a function, a column."""
    tokens = tokenize(text)
    if not tokens:
        raise PredicateError("empty expression")
    parser = _Parser(tokens)
    node = parser.parse_operand()
    if parser.peek():
        raise PredicateError(f"unexpected {parser.peek().text!r} in the expression")
    return node


def parse_predicate(text: str) -> object:
    """Parse a WHERE condition into something that can evaluate a row."""
    tokens = tokenize(text)
    if not tokens:
        raise PredicateError("empty condition")
    return _Parser(tokens).parse()


def matches(
    node: object, row: Mapping[str, object], parameters: Mapping[str, object]
) -> bool:
    """Whether a row satisfies the condition.

    Only true passes. Unknown does not, which is what SQL does and why a
    comparison against NULL filters a row out rather than keeping it.
    """
    return node.evaluate(row, parameters) is True
