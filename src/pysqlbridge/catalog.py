"""The tables a connection can see, and the handler that answers from them.

This is where the three halves meet: sql.py decides what was asked, source.py
supplies the data, and tds.result puts it on the wire. Keeping the join here
means neither of the other two has to know about the third.

Table names match case-insensitively, because SQL Server's default collation
does and a client that round-trips a name through its own interface may not
preserve the case the file had.
"""

from __future__ import annotations

import concurrent.futures
import difflib
import json
import datetime
import re
import socket
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import (
    aggregate,
    discover,
    information_schema,
    precedence,
    procedures,
    window,
)
from .credentials import credential
from .http_source import (
    DEFAULT_MAX_PAGES,
    DEFAULT_MAX_ROWS,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_TTL_SECONDS,
    FORMATS,
    STRATEGIES,
    ChildSource,
    HttpSource,
    Paging,
    StaticSource,
)
from .predicate import (
    Column as PredicateColumn,
    CONTEXT,
    Deferred,
    ALREADY_AN_OBJECT,
    NO_SUCH_TABLE_TO_DROP,
    A_COLUMN_WITH_NO_NAME,
    NO_NAME_AT_ALL,
    DOES_NOT_MATCH_THE_TABLE,
    NO_SUCH_COLUMN,
    ONE_COLUMN_ONLY,
    TOO_FEW_TO_INSERT,
    TOO_MANY_TO_INSERT,
    SYNTAX_ERROR,
    UNCLOSED_QUOTATION,
    NEAR_A_KEYWORD,
    MISSING_END_COMMENT,
    UNDECLARED_VARIABLE,
    UNDECLARED_TABLE_VARIABLE,
    ASSIGNING_AND_READING,
    PredicateError,
    brought_to_one_type,
    aggregates_in,
    one_spelling,
    Comparison,
    ParameterRef,
    _Candidates,
    _parameter_name,
    all_of,
    as_parameters,
    conjuncts,
    mentions_a_parameter,
    collated,
    columns_in,
    is_constant,
    matches,
    parse_expression,
    parse_predicate,
    result_kind,
    with_deferred,
)
from .source import (
    DECLARED_FOR,
    PYTHON_FOR,
    SourceError,
    Table,
    from_access,
    from_csv,
    from_excel,
    from_json,
    from_markup,
    holdings,
)
from .sql import (
    SelectItem,
    SqlError,
    end_of_branch,
    parse_select,
    values_written,
    declarations,
    malformed,
    select_assignments,
    unbound,
    skip_quoted as _skip_quoted,
    statements as _statements,
    without_comments,
)
from .tds.result import (
    Bit,
    Column,
    Float,
    Integer,
    NVarChar,
    Query,
    QueryError,
    QueryResult,
)

# The words a statement can begin with, which is how the end of an IF
# condition is found: T-SQL needs no semicolon between a condition and the
# statement it guards, and no condition ends with one of these.
STATEMENT_WORDS = frozenset({
    "SELECT", "EXEC", "EXECUTE", "SET", "DECLARE", "PRINT", "RETURN",
    "BEGIN", "WITH", "INSERT", "UPDATE", "DELETE", "RAISERROR", "THROW",
    "COMMIT", "ROLLBACK", "SAVE",
})

# A statement that produces rows, and one that gives a variable a value.
# DECLARE with no assignment leaves the variable null, which is what an
# undeclared parameter already answers, so it needs no handling of its own.
# The brackets are how a client writes each part of a combination; what is
# inside them is still a select, and parse_select takes them off.
_READS = re.compile(r"\s*(?:\(\s*)*(SELECT|WITH)\b", re.IGNORECASE)
_IF = re.compile(r"\s*IF\s+", re.IGNORECASE)
_BEGIN = re.compile(r"\s*BEGIN\b", re.IGNORECASE)
_CREATE_TEMP = re.compile(
    r"\s*CREATE\s+TABLE\s+(#[A-Za-z0-9_@#$]+)\s*\((.*)\)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_DROP_TEMP = re.compile(r"\s*DROP\s+TABLE\s+(#[A-Za-z0-9_@#$]+)\s*$", re.IGNORECASE)
# A select that makes the table it fills, rather than filling one already
# made. The INTO sits between the column list and the FROM, and taking it out
# leaves an ordinary select.
_SELECT_INTO = re.compile(
    r"(\s*SELECT\b.*?)\s+INTO\s+(#[A-Za-z0-9_@#$]+)(?:\s+(FROM\b.*))?$",
    re.IGNORECASE | re.DOTALL,
)
_INSERT_TEMP = re.compile(
    r"\s*INSERT\s+(?:INTO\s+)?(#[A-Za-z0-9_@#$]+)\s*"
    r"(?:\(([^)]*)\))?\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)
# What has to be run, as against a setup statement that can be ignored.
# Not only the reads: a session builds a table of its own before it reads it,
# and a write has to be run to be refused.
_RUNS = re.compile(
    r"\s*(?:\(\s*)*(SELECT|WITH|IF|EXEC|EXECUTE|CREATE|INSERT|DROP|BEGIN"
    r"|UPDATE|DELETE|MERGE|TRUNCATE|ALTER|GRANT|REVOKE|DENY"
    r"|COMMIT|ROLLBACK|SAVE|RAISERROR|THROW)\b",
    re.IGNORECASE,
)

# The statements that begin, end and mark a transaction, and the name each
# carries where it has one. A name is a word, a bracketed name, or a variable
# holding one. COMMIT takes a name and, measured, does nothing with it; WITH
# MARK and DELAYED_DURABILITY say how a real server should log a transaction,
# and there is no log here to say it to.
_TRANSACTION_NAME = r"(\[[^\]]*\]|@?[A-Za-z_#][A-Za-z0-9_@#$]*)"
#
# Each may end in a semicolon, because the branch of an IF keeps the one that
# ended it: IF @@TRANCOUNT = 0 BEGIN TRAN; SELECT ... hands the branch over
# as BEGIN TRAN; with its semicolon still on.
_BEGIN_TRANSACTION = re.compile(
    r"\s*BEGIN\s+TRAN(?:SACTION)?(?:\s+" + _TRANSACTION_NAME
    + r"(?:\s+WITH\s+MARK(?:\s+N?'(?:[^']|'')*')?)?)?\s*;?\s*$",
    re.IGNORECASE,
)
_COMMIT = re.compile(
    r"\s*COMMIT(?:\s+WORK|\s+TRAN(?:SACTION)?(?:\s+" + _TRANSACTION_NAME
    + r")?)?(?:\s+WITH\s*\(.*\))?\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_ROLLBACK = re.compile(
    r"\s*ROLLBACK(?:\s+WORK|\s+TRAN(?:SACTION)?(?:\s+" + _TRANSACTION_NAME
    + r")?)?\s*;?\s*$",
    re.IGNORECASE,
)
_SAVE = re.compile(
    r"\s*SAVE\s+TRAN(?:SACTION)?\s+" + _TRANSACTION_NAME + r"\s*;?\s*$",
    re.IGNORECASE,
)

# A statement that changes something, and the name of what it changes. The
# session's own #temp tables are the one thing here that is written, and they
# are matched before this; anything else has to be refused rather than passed
# over, because a client told its DELETE succeeded would be right to believe
# the rows were gone. Nothing a real client sends reaches this: every write
# in a captured SSMS session names a #temp table.
# What a table can be called where a statement names one. A bracketed name
# may hold anything, spaces and dots included, so the brackets are read as a
# pair rather than as two more characters of the name: DROP TABLE [my table]
# names one table, and reporting back that 'my' is unchanged names nothing.
_NAME = r"(?:\[[^\]]*\]|[A-Za-z0-9_@#$]+)(?:\.(?:\[[^\]]*\]|[A-Za-z0-9_@#$]+))*"
_WRITES = re.compile(
    r"\s*(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|DROP|ALTER|CREATE)\b"
    r"(?:\s+(?:INTO|FROM|TABLE|VIEW|PROCEDURE|PROC|INDEX|FUNCTION|TRIGGER"
    r"|SCHEMA|DATABASE))?"
    r"\s+(" + _NAME + r")",
    re.IGNORECASE,
)
# A statement that changes who may read something. The object it names comes
# after ON, and the whole statement is refused whether or not it does: there
# is nothing here to grant, and every source is read-only for everyone.
_PERMISSION = re.compile(
    r"\s*(GRANT|REVOKE|DENY)\b(?:.*?\bON\s+(" + _NAME + r"))?",
    re.IGNORECASE | re.DOTALL,
)
_ELSE = re.compile(r"\s*ELSE\b", re.IGNORECASE)
# A block that says what to do when something in it fails.
# An error a client raised on purpose. Only the bracketed form: the old
# spelling without brackets has been deprecated for twenty years and no
# client writes it.
_RAISERROR = re.compile(r"\s*RAISERROR\s*\(", re.IGNORECASE)
# The other way a client raises one, and the one a CATCH is usually written
# around. Its arguments are not bracketed, and there are three of them or
# none: measured, a real server refuses two while it compiles.
_THROW = re.compile(r"\s*THROW\b(.*)$", re.IGNORECASE | re.DOTALL)
# The one SET whose answer a batch depends on. Every other one is passed
# over, which is what a client sending dozens of them before it will talk
# to a server needs.
_SET_XACT_ABORT = re.compile(
    r"\s*SET\s+XACT_ABORT\s+(ON|OFF)\s*;?\s*$", re.IGNORECASE)

_TRY = re.compile(r"\s*BEGIN\s+TRY\b", re.IGNORECASE)
_END_TRY = re.compile(r"\s*END\s+TRY\b", re.IGNORECASE)
_BEGIN_CATCH = re.compile(r"\s*BEGIN\s+CATCH\b", re.IGNORECASE)
_END_CATCH = re.compile(r"\s*END\s+CATCH\b", re.IGNORECASE)
# EXEC of a procedure that writes its answer back into a variable, which is
# how a client reads the registry: the value comes out through the last
# argument rather than as a row.
_EXEC_OUTPUT = re.compile(
    r"\s*EXEC(?:UTE)?\s+(?:\[?[A-Za-z0-9_]+\]?\.){0,2}"
    r"\[?(xp_instance_regread|xp_regread)\]?\s+(.*?)"
    r",\s*(@[A-Za-z0-9_@#$]+)\s+OUTPUT\s*$",
    re.IGNORECASE | re.DOTALL,
)

# What this answers when a client reads a registry value. There is no
# registry: nothing here was installed, and none of these settings exist to
# be read. Answering nothing at all is still the wrong shape, because the
# batch that asks is one statement and a client that cannot run it loses
# every other value in it, including the edition and the version. So the
# procedure runs and gives back null, which is what the batch is written to
# cope with, except where this server does know the answer.
REGISTRY_VALUES = {
    # Windows authentication only, which is the whole of what this does and
    # what SERVERPROPERTY('IsIntegratedSecurityOnly') already says.
    "LOGINMODE": 1,
    # Nothing is audited and nothing is logged, so there are no logs to keep.
    "AUDITLEVEL": 0,
    "NUMERRORLOGS": 0,
}
# DECLARE @v <type>, with or without a value after it. The type is worth
# keeping on its own: a variable that ends up null has nothing else to say
# what kind of column it makes, and a real server still knows.
_DECLARES = re.compile(
    r"\s*DECLARE\s+(@[A-Za-z0-9_@#$]+)\s+(?:AS\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*(?:\s*\([^)]*\))?)",
    re.IGNORECASE,
)

# Where the kinds a batch declared are kept, beside the values themselves.
# One reserved parameter, for the same reason the connection's details are
# one: what travels with a statement should travel with its parameters.
DECLARED = "@@__declared"

_EXEC_NAME = re.compile(
    r"\s*EXEC(?:UTE)?\s+([A-Za-z0-9_@#$.\[\]]+)\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)
_EXEC_LITERAL = re.compile(
    r"\s*EXEC(?:UTE)?\s*\(\s*N?'(.*)'\s*\)\s*$", re.IGNORECASE | re.DOTALL
)
# The same thing said the other way. A client sends it constantly, wrapped in
# a TRY so that a server which cannot run it says nothing rather than failing,
# which is how this went unnoticed: the CATCH answered and the probe came back
# empty. Whatever follows the statement is its declarations and its arguments,
# and the named ones among them are values, the same as over RPC.
_EXEC_SP = re.compile(
    r"\s*EXEC(?:UTE)?\s+(?:\[?[A-Za-z0-9_]+\]?\.){0,2}\[?sp_executesql\]?\s+N?'",
    re.IGNORECASE,
)
# SET @a += 2 and the rest of the compound operators, each of which means
# the variable with that operator applied to all of what follows it.
_COMPOUND_SET = re.compile(
    r"\s*SET\s+(@[A-Za-z0-9_@#$]+)\s*([-+*/%&|^])=\s*(.+?)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)
# SET, DECLARE and SELECT all give a variable a value, and a client uses
# whichever suits: SSMS declares one and selects into it in the same breath.
_ASSIGNMENT = re.compile(
    r"\s*(?:SET|DECLARE|SELECT)\s+(@[A-Za-z0-9_@#$]+)\s*(?:AS\s+)?"
    r"(?:[A-Za-z_][A-Za-z0-9_]*\s*(?:\([^)]*\))?\s*)?=\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)

# The schema everything this serves is in, and the one a client writes when
# it qualifies a name at all.
DEFAULT_SCHEMA = "dbo"

# SQL Server's "invalid object name". Clients already know how to present it,
# and a missing table here is the same thing to a user.
INVALID_OBJECT_NAME = 208

# Where SQL Server's user-defined range starts. Unsupported syntax is this
# project's own complaint rather than one of the server's.
UNSUPPORTED = 50000

# Loading sources is network wait, not work, so the pool can be wider than
# the machine has cores. Bounded anyway: a config with two hundred tables
# should not open two hundred sockets at once.
MAX_PARALLEL_LOADS = 12

# A join can multiply its inputs. This is the ceiling on what one will build
# before refusing, so a condition that matches everything against everything
# fails with a message rather than by exhausting memory.
MAX_JOIN_ROWS = 1_000_000

# How many distinct answers one correlated subquery may need. It runs once
# per distinct value it is asked about rather than once per row, so a
# thousand rows sharing twelve keys cost twelve; this is the bound on a query
# that genuinely asks for a million different ones.
MAX_CORRELATED_ANSWERS = 10_000

# How deep a query may nest its subqueries and named queries. A WITH that
# names itself is the case this catches, and it catches it with a message
# rather than with a stack overflow.
MAX_NESTING = 16

# What the @@ variables answer. Clients send these to work out what they are
# talking to, and refusing them makes a connection look broken over something
# that costs nothing to answer. The banner is SQL Server shaped because that
# is what a client parses, and names this project because a person reading it
# should not be misled.
SERVER_VARIABLES = {
    "@@VERSION": (
        "Microsoft SQL Server 2025 - 17.0.1000.0 (X64), served by pysqlbridge"
    ),
    "@@SERVERNAME": None,          # filled in per server below
    "@@SPID": 51,
    "@@LANGUAGE": "us_english",
    "@@MAX_PRECISION": 38,
    # 17.0.1000 packed the way a client unpacks it: major, minor, build.
    "@@MICROSOFTVERSION": (17 << 24) + (0 << 16) + 1000,
    "@@NESTLEVEL": 0,
    # What a connection that has not run anything yet reports. What one that
    # has reports is kept per connection under ROWCOUNT below, because this
    # is the one @@ variable whose answer is about the connection rather than
    # about the server.
    "@@ROWCOUNT": 0,
    "@@TRANCOUNT": 0,
    "@@OPTIONS": 0,
}

# Where a connection keeps the number of rows its last statement produced.
#
# Measured on SQL Server 2025, statement by statement: a read sets it to the
# rows it returned, and to nought when it returned none; an assignment sets
# it to one, and to the rows read where it read some; a bare DECLARE leaves
# it alone; and everything else, PRINT and CREATE TABLE among them, sets it
# to nought.
ROWCOUNT = "@@__rowcount"

# Where a connection keeps the number of the last statement that failed, for
# @@ERROR to read. Measured on SQL Server 2025: a statement that fails leaves
# its number here, and the next statement to finish puts it back to nought,
# so IF @@ERROR <> 0 only sees the statement just before it. A DECLARE with
# no value is the one statement that leaves it standing, which is the same
# exception @@ROWCOUNT makes for it; a DECLARE that gives a value is an
# assignment and clears it like the rest.
#
# An IF and an EXEC of text leave it to the statements they run: measured,
# IF 1 = 1 BEGIN COMMIT END still reads 3902 afterwards, while an IF whose
# condition is false reads nought. A TRY clears it once its CATCH is done,
# because handling the error is what a TRY is for.
ERROR_NUMBER = "@@__error"

# The error that sent this connection into the CATCH it is running, for
# ERROR_NUMBER() and the rest to read, or None outside one.
#
# Kept apart from ERROR_NUMBER above, which looks like the same thing and is
# not: measured, a statement running inside a CATCH puts @@ERROR back to
# nought while ERROR_NUMBER() still answers the number that sent it there.
# Reading @@ERROR for both would have been right until the CATCH held more
# than one statement, and wrong quietly after that.
CAUGHT = "@@__caught"

# Whether this connection asked for XACT_ABORT. Measured on SQL Server 2025:
# with it on, the errors a batch would otherwise carry on past end it
# instead, and what the statements before them answered is still kept. It
# belongs to the connection rather than the batch and survives into the next
# one, and SET XACT_ABORT OFF puts it back. Clients do set it.
XACT_ABORT = "@@__xact_abort"

# Where a connection keeps its transactions: how many are open, what the
# outermost was called, and the savepoints marked inside it, newest last.
#
# Measured on SQL Server 2025. BEGIN TRAN adds one and COMMIT takes one away;
# ROLLBACK ends them all. Only the outermost transaction's name is kept, so
# rolling back to an inner one's is refused. Savepoints are a stack: rolling
# back to one releases it and every one marked after it, a name marked twice
# is rolled back to twice, and a savepoint is found before a transaction of
# the same name. A savepoint's name is matched without regard to case, as
# this server matches all text, and a transaction's name exactly.
TRANSACTION = "@@__transaction"

# What SQL Server answers when a transaction statement has nothing to act
# on, measured, numbers and words both. 628 ends the batch on a real server
# and ends it here; the other three let the rest of the batch run, and now
# do that here as well.
COMMIT_WITHOUT_BEGIN = 3902
ROLLBACK_WITHOUT_BEGIN = 3903
SAVE_WITHOUT_BEGIN = 628
NO_SUCH_SAVEPOINT = 6401

# What a failed statement does to the batch around it. Measured on SQL
# Server 2025 one number at a time, each of them as
#
#     sqlcmd -Q "<the failing statement>; SELECT 'CONTINUED'"
#
# After these the rest of the batch still runs, and what it answers reaches
# the client behind the error, so the error is one of the answers rather
# than the whole of it. Every other error this server raises ends the batch,
# which is what a compile error does on a real server too.
THE_BATCH_GOES_ON = frozenset({
    220,    # arithmetic overflow converting to a narrower type
    244,    # the same, converting text
    248,    # text that overflowed the column it was cast to
    517,    # a datetime past what the type can hold
    535,    # a DATEDIFF that does not fit an int
    2812,   # no such stored procedure
    3701,   # dropping a table that is not there
    3902,   # COMMIT with nothing open
    3903,   # ROLLBACK with nothing open
    6401,   # rolling back to a name that was never marked
    8115,   # arithmetic overflow
    8134,   # divide by zero
    9828,   # TRANSLATE with lists of different lengths
})

# What SET XACT_ABORT ON does to the set above: every one of those ends the
# batch instead of letting it run on, except this. Measured one number at a
# time with the setting on, each of them as
#
#     sqlcmd -Q "SELECT 'BEFORE'; <the failing statement>; SELECT 'AFTER'"
#
# which answers BEFORE and then the error for twelve of the thirteen, and
# reaches AFTER only for 3701. That is the one of them a real server reports
# at severity 11 rather than 16. What answered before the error is kept
# either way, so the setting changes where a batch stops and not what it
# keeps. The first guess written down here was that it converted the whole
# set wholesale; measuring each one is what found the exception.
#
# An error a client asked for is not the setting's to change: measured, a
# RAISERROR still lets the batch run on with it on, and a THROW still ends
# the batch. Both say so on the error itself, which is read first, so
# neither reaches this.
THE_SETTING_DOES_NOT_REACH = frozenset({3701})

# The errors a real server sends an empty result set in front of. It has
# bound the query and started running it by the time one of these is hit,
# so the column metadata has already gone out; a batch that failed while
# binding, an invalid object or column, sent none, because it never got
# that far. Every one is an error from evaluating a row's expression.
# Measured on SQL Server 2025 one at a time, each as
#
#     ExecuteReader("SELECT <expression that fails> AS bad")
#
# and reading whether a result set of nought rows and its columns arrived
# before the error. 208 and 207 sent nothing, and 1/0 with a bad table
# named after it sent nothing too, which is why this is a set of numbers
# rather than a guess from whether the shape can be worked out.
KEEPS_THE_COLUMN_SHAPE = frozenset({
    220,    # arithmetic overflow for a type
    241,    # a datetime that would not convert
    244,    # text that overflowed a one-byte integer column
    245,    # text that would not convert to a number
    248,    # text that overflowed an int column
    281,    # a CONVERT style that is not one
    517,    # a datetime past what the type holds
    535,    # a DATEDIFF that does not fit
    3623,   # an invalid floating point operation
    8114,   # converting one data type to another
    8115,   # arithmetic overflow
    8134,   # divide by zero
    8169,   # text that is not a uniqueidentifier
    9828,   # TRANSLATE with lists of different lengths
})

# And these end the batch, but only once it has run that far. What the
# statements before them answered has already reached the client, so it is
# kept and the error is the last thing read. Measured the same way, as
#
#     sqlcmd -Q "SELECT 'BEFORE'; <the failing statement>; SELECT 'AFTER'"
#
# which returns BEFORE and then the error for every one of these. A compile
# error is the other case entirely: a real server runs none of the batch, so
# a syntax error returns neither, and nothing is kept for one here either.
THE_BATCH_ENDS_AFTER = frozenset({
    208,    # no such table, found when the statement runs, not at compile
    241,    # text that is not a datetime
    245,    # a conversion that failed
    281,    # a CONVERT style that is not one
    628,    # SAVE with nothing open
    2714,   # creating a temp table that is already there
    3623,   # an invalid floating point operation
    8114,   # a conversion error
    8169,   # text that is not a uniqueidentifier
})

# Of those, the ones that end the batch around an EXEC of text as well as
# the text itself. Everything else raised inside EXEC('...') or
# sp_executesql ends only the text, and the batch that ran it carries on to
# its next statement. Measured the same way, one at a time, as
#
#     sqlcmd -Q "EXEC sp_executesql N'<the failing statement>'; SELECT 'AFTER'"
#
# which returns AFTER for 208, 2812, 3902 and 8134 and stops without it for
# these. 208 is the one that surprises: it ends a batch where it is written,
# but inside an EXEC of text it ends only the text.
ESCAPES_WRITTEN_OUT = frozenset({241, 245, 281, 628, 3623, 8114, 8169})

# The errors of a batch that did not compile. None of them touches a
# transaction an earlier batch opened: measured one at a time, with XACT_ABORT
# off and on, as BEGIN TRAN and then a batch failing each way, which leaves
# @@TRANCOUNT at 1 for all seven. Nothing ran, so there was nothing to undo.
SETTLED_WHILE_COMPILING = frozenset({
    SYNTAX_ERROR,               # 102, incorrect syntax near a token
    UNCLOSED_QUOTATION,         # 105, a quote left open
    MISSING_END_COMMENT,        # 113, a comment left open
    UNDECLARED_VARIABLE,        # 137, a variable nothing declared
    ASSIGNING_AND_READING,      # 141, a SELECT that assigns and reads
    NEAR_A_KEYWORD,             # 156, a keyword where a name or value goes
    UNDECLARED_TABLE_VARIABLE,  # 1087, the same for a table variable
})

# What a RAISERROR with a message of its own reports. The same number this
# uses for something it cannot do, which is why a raised error is marked as
# raised: measured, a batch carries on past a RAISERROR and stops at a
# refusal, and the number alone cannot tell the two apart.
RAISED_BY_A_CLIENT = 50000

# Below this a RAISERROR is not an error. Measured: severity 10 raises
# nothing, leaves @@ERROR at nought and the batch running, where 11, 16 and
# 18 all report and all carry on.
LOWEST_SEVERITY_THAT_RAISES = 11

# What a THROW does that a RAISERROR does not: it ends the batch. Measured
# on SQL Server 2025 as
#
#     sqlcmd -Q "SELECT 'before'; THROW 51000, 'x', 7; SELECT 'after'"
#
# which answers 'before', then the error, and never reaches 'after', where
# the same batch written with RAISERROR reaches it. What answered before it
# is still kept. Inside an EXEC of text it ends the batch that ran the text
# as well, where a RAISERROR in one leaves that batch running. The severity
# is 16 whatever number it is given, and the state is the third argument.
SEVERITY_OF_A_THROW = 16

# The range a THROW's number has to fall in, and what a real server answers
# for one outside it. Measured: severity 16 state 10, the batch keeps what
# it had already answered and stops there, and a TRY around it does catch
# it, so this is settled while the batch runs rather than while it compiles.
LOWEST_NUMBER_A_THROW_MAY_RAISE = 50000
HIGHEST_NUMBER_A_THROW_MAY_RAISE = 2147483647
THROWN_NUMBER_OUT_OF_RANGE = 35100

# A bare THROW with no CATCH around it to give it an error. Measured at
# severity 15, and a real server settles it while compiling: nothing in the
# batch answers, and a TRY around it does not catch it. A number in neither
# of the two sets above already ends a batch exactly that way, so this is
# left to decide itself rather than carrying a mark.
NOTHING_TO_RETHROW = 10704


class _Transactions:
    """A connection's open transactions, kept under TRANSACTION above."""

    def __init__(self) -> None:
        self.count = 0
        self.name: str | None = None
        self.savepoints: list[str] = []

    def end(self) -> None:
        self.count, self.name, self.savepoints = 0, None, []


def _transactions(session: dict | None) -> _Transactions:
    """The connection's transactions, made the first time they are asked."""
    if session is None:
        return _Transactions()
    held = session.get(TRANSACTION)
    if held is None:
        held = session[TRANSACTION] = _Transactions()
    return held


def _connection_variables(session: dict | None) -> dict:
    """The @@ variables, as this connection answers them.

    Most are about the server. Two are about the connection and kept on it:
    @@ROWCOUNT, which a client asks after a read to find out how much came
    back, and @@TRANCOUNT, which it asks before deciding whether to COMMIT.
    Answering nought to either whatever had just happened told the client
    something untrue. A read sees these and so does an IF's condition,
    because IF @@TRANCOUNT > 0 is how the second is usually asked.
    """
    known = {name: value for name, value in SERVER_VARIABLES.items()
             if value is not None}
    held = session or {}
    known["@@SERVERNAME"] = held.get("server") or socket.gethostname()
    known["@@ROWCOUNT"] = held.get(ROWCOUNT, 0)
    known["@@ERROR"] = held.get(ERROR_NUMBER, 0)
    known["@@TRANCOUNT"] = (
        _transactions(session).count if session is not None else 0
    )
    return known


def _transaction_statement(written: str, parameters: dict,
                           session: dict | None) -> bool:
    """Begin, end or mark a transaction, or False for any other statement.

    Nothing here is written, so there is nothing to commit and nothing to
    roll back. What is kept is the count, because a client asks for it and
    decides what to send next by the answer, and the names, because rolling
    back to one that is not there is an error a client can be relying on.
    """
    begun = _BEGIN_TRANSACTION.match(written)
    committed = None if begun else _COMMIT.match(written)
    rolled = None if begun or committed else _ROLLBACK.match(written)
    saved = None if begun or committed or rolled else _SAVE.match(written)
    if not (begun or committed or rolled or saved):
        return False

    held = _transactions(session)
    if begun:
        held.count += 1
        if held.count == 1:
            held.name = _transaction_name(begun.group(1), parameters)
            held.savepoints = []
    elif committed:
        if not held.count:
            raise QueryError(
                "The COMMIT TRANSACTION request has no corresponding BEGIN "
                "TRANSACTION.",
                number=COMMIT_WITHOUT_BEGIN,
            )
        held.count -= 1
        if not held.count:
            held.end()
    elif rolled:
        if not held.count:
            raise QueryError(
                "The ROLLBACK TRANSACTION request has no corresponding BEGIN "
                "TRANSACTION.",
                number=ROLLBACK_WITHOUT_BEGIN,
            )
        named = _transaction_name(rolled.group(1), parameters)
        marked = [one.lower() for one in held.savepoints]
        if named is not None and named.lower() in marked:
            # The newest savepoint of that name, and every one after it.
            at = len(marked) - 1 - marked[::-1].index(named.lower())
            del held.savepoints[at:]
        elif named is None or named == held.name:
            held.end()
        else:
            raise QueryError(
                f"Cannot roll back {named}. No transaction or savepoint of "
                f"that name was found.",
                number=NO_SUCH_SAVEPOINT,
            )
    else:
        if not held.count:
            raise QueryError(
                "Cannot issue SAVE TRANSACTION when there is no active "
                "transaction.",
                number=SAVE_WITHOUT_BEGIN,
            )
        held.savepoints.append(_transaction_name(saved.group(1), parameters))

    if session is not None:
        # Measured: each of them leaves @@ROWCOUNT at nought.
        session[ROWCOUNT] = 0
    return True


def _transaction_name(written: str | None, parameters: dict) -> str | None:
    """A transaction or savepoint name as written, or the one a variable holds."""
    if written is None:
        return None
    if written.startswith("[") and written.endswith("]"):
        return written[1:-1]
    if written.startswith("@") and written in parameters:
        value = parameters[written]
        return None if value is None else str(value)
    return written

# SQL Server's "could not find stored procedure". A client that asked for one
# and got silence has no way to tell that from an empty answer.
STORED_PROCEDURE_NOT_FOUND = 2812

# A source that exists but could not be read this time. Distinct from a
# missing table, because the fix is different: check the URL, not the
# spelling.
SOURCE_UNAVAILABLE = 50001


@dataclass
class Catalog:
    """The set of tables served to clients.

    Sources rather than tables, because an HTTP source refetches once its cache
    expires. Everything answers the same question, load(), so the catalog never
    has to know which kind it is holding.
    """

    sources: dict[str, object] = field(default_factory=dict)

    def add(self, table: Table) -> None:
        """Serve a table that was read once and will not change."""
        self.add_source(StaticSource(table))

    def add_source(self, source: object) -> None:
        key = source.name.lower()
        if key in self.sources:
            raise SourceError(
                f"two sources are both called '{source.name}'; table names must "
                f"be unique, so give one of them an explicit name"
            )
        self.sources[key] = source

    @property
    def tables(self) -> dict[str, Table]:
        """Every table, loading any that need it."""
        return {key: source.load() for key, source in self.sources.items()}

    def warm(self) -> list[str]:
        """Fetch every source's shape now, so the first client does not wait.

        Returns the names that could not be loaded. They stay in the catalog:
        a source that is down at startup may be up by the first query.
        """
        failed = []
        for source, table in zip(self.sources.values(), self.load_all()):
            if not table.columns and source.name == table.name:
                failed.append(source.name)
        return failed

    def load_all(self) -> list[Table]:
        """Every table's shape, fetched at once rather than one after another.

        Measured on seven API sources: 1322 ms of fetching in sequence against
        724 ms for the slowest one alone. It is all network wait, so threads
        help even though the work is not CPU-bound.

        Shapes, not full tables. This feeds the catalog views and the startup
        warm, both of which want to know what exists and what its columns are.
        Reading every page of every source to answer that took 23 seconds on a
        catalog of 65 discovered tables, nearly all of it spent paginating
        collections nobody had asked for. A query goes through get(), which
        reads the whole table.

        A source that cannot be reached comes back as a table with no columns
        rather than raising, because one unreachable API should not hide every
        table that does work.
        """
        sources = list(self.sources.values())
        if len(sources) < 2:
            shapes = [_safe_load(source) for source in sources]
        else:
            workers = min(len(sources), MAX_PARALLEL_LOADS)
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="pysqlbridge-source"
            ) as pool:
                shapes = list(pool.map(_safe_load, sources))

        # A response has now been seen, so any arrays inside its rows are
        # known and the tables they make can join the catalog.
        added = self._register_children(sources)
        return shapes + [_safe_load(source) for source in added]

    def _register_children(self, sources: list) -> list:
        """Add a table for every array found inside a source's rows.

        Named parent_column. A name a person already gave to something else
        wins, because a configuration is a decision and this is an inference.
        """
        added = []
        for source in list(sources):
            try:
                found = source.children()
            except (SourceError, AttributeError):
                continue
            for column, table in found.items():
                if table.name.lower() in self.sources:
                    continue
                child = ChildSource(parent=source, column=column,
                                    name=table.name)
                self.sources[table.name.lower()] = child
                added.append(child)
        return added

    def views(self) -> dict[str, Table]:
        """The catalog views, rebuilt from whatever is currently served."""
        return information_schema.build(self.load_all())

    def get(self, name: str, schema: str | None = None,
            parameters: dict | None = None) -> Table:
        if schema and schema.upper() == information_schema.SYS_PREFIX:
            served = information_schema.system_views(procedures.CATALOG)
            view = served.get(name.lower())
            if view is not None:
                return view
            # The views that describe what is served rather than the server.
            # Built only when one is asked for, because building them loads
            # every source, and a client asking what edition this is should
            # not pull a CSV off disk to be told.
            about = (parameters or {}).get(CONTEXT) or {}
            built = information_schema.object_views(
                self.load_all(), about.get("login") or "")
            view = built.get(name.lower())
            if view is not None:
                return view
            raise QueryError(
                f"invalid object name 'sys.{name}'. This server has: "
                f"{', '.join(sorted(set(served) | set(built)))}",
                number=INVALID_OBJECT_NAME,
            )
        if schema and schema.upper() == information_schema.SCHEMA_PREFIX:
            view = self.views().get(name.lower())
            if view is not None:
                return view
            available = ", ".join(sorted(v.name for v in self.views().values()))
            raise QueryError(
                f"invalid object name '{information_schema.SCHEMA_PREFIX}.{name}'. "
                f"This server has: {available}",
                number=INVALID_OBJECT_NAME,
            )
        if schema and schema.lower() == DEFAULT_SCHEMA:
            # dbo is the schema everything here is in, so a name qualified
            # by it is the same name. A few system tables live there too,
            # and a client reads them while building its tree.
            system = self._system_table(name)
            if system is not None:
                return system

        source = self.sources.get(name.lower())
        if source is None:
            # dbo is also the default schema, so an unqualified name is one
            # of those system tables when nothing served answers to it. After
            # the sources, because a name a person gave a table is theirs.
            system = self._system_table(name)
            if system is not None:
                return system
            known = ", ".join(sorted(s.name for s in self.sources.values())) or "none"
            raise QueryError(
                f"invalid object name '{name}'. This server has: {known}",
                number=INVALID_OBJECT_NAME,
            )
        try:
            return source.load()
        except SourceError as exc:
            # Not a missing table. Saying so lets the user fix the URL rather
            # than hunt for a typo in the name.
            raise QueryError(
                f"table '{name}' could not be loaded: {exc}",
                number=SOURCE_UNAVAILABLE,
            ) from exc

    def _system_table(self, name: str) -> Table | None:
        """A system table served under dbo, if that is what this name is.

        Two tiers, like the sys views and for the same reason: the second
        describes what is served and has to load it to answer, and every
        other name is answered by the first without touching a source.
        """
        served = information_schema.default_schema_views()
        if name.lower() in served:
            return served[name.lower()]
        if name.lower() == "sysobjects":
            return information_schema.sysobjects(self.load_all())
        return None

    @property
    def names(self) -> list[str]:
        return sorted(source.name for source in self.sources.values())

    def shapes(self) -> list[Table]:
        """Every table's columns, in name order, for the catalog procedures."""
        return _sorted_by_name(self.load_all())

    def resolve(self, select, named=None, depth: int = 0,
                parameters=None) -> Table:
        """The table a SELECT reads from: joins, named queries and all."""
        if depth > MAX_NESTING:
            raise SourceError(
                f"this query nests more than {MAX_NESTING} deep; a named "
                f"query that refers to itself does that"
            )
        named = dict(named or {})

        if select.values_rows:
            table = _values_table(select.values_rows, select.values_columns,
                                  select.alias or "", parameters or {})
        elif select.derived is not None:
            table = self.materialise(
                select.derived, named, depth + 1, select.table, parameters
            )
        else:
            table = self._named(select.table, select.schema, named,
                                parameters)

        if not select.joins:
            return self._applying(table, select.applies, named, depth,
                                  parameters)

        left = _renamed(table, select.alias or select.table)
        for join in select.joins:
            right = _renamed(
                self._join_table(join, named, depth, parameters), join.name
            )
            left = _join(left, right, join, parameters or {})
        return _unqualified(left)

    def _join_table(self, join, named, depth: int, parameters) -> Table:
        """The table on the right of a join: named, or written in brackets.

        The two bracketed forms reach here the same way they reach the
        FROM, and are answered the same way: values are worked out where
        they stand, and a derived select is run and kept.
        """
        if join.values_rows:
            return _values_table(join.values_rows, join.values_columns,
                                 join.name, parameters or {})
        if join.derived is not None:
            return self.materialise(join.derived, named, depth + 1,
                                    join.name, parameters)
        return self._named(join.table, join.schema, named, parameters)

    def _named(self, name: str, schema: str | None, named: dict,
               parameters: dict | None = None) -> Table:
        """A table by name, preferring one the query defined itself."""
        if not schema and name.lower() in named:
            return named[name.lower()]
        return self.get(name, schema, parameters)

    def materialise(self, select, named=None, depth: int = 0,
                    name: str = "", parameters=None) -> Table:
        """Run a SELECT and keep the answer as a table.

        This is what a CTE, a derived table and a subquery all reduce to. It
        goes through answer() so a named query is filtered, grouped and
        ordered exactly as the same text would be at the top level, and with
        the same parameters: a variable read inside one is the variable the
        statement around it declared.
        """
        result = self.answer(
            Query(sql="", parameters=dict(parameters or {})),
            select=select, named=named, depth=depth,
        )
        return Table(
            name=name or "subquery",
            columns=list(result.columns),
            rows=[list(row) for row in result.rows],
        )

    def _about(self, query) -> dict:
        """What a function that asks about the connection is told.

        The connection supplies who is asking; this supplies what they
        reached. Everything in it is something this server actually knows,
        so a client that asks is not told a story.
        """
        session = query.session or {}
        return {
            "login": session.get("login"),
            "app": session.get("app"),
            "host": session.get("host"),
            "server": session.get("server"),
            "database": procedures.CATALOG,
            "user": "dbo",
            "schema": "dbo",
            "now": session.get("now"),
            "tables": tuple(source.name for source in self.sources.values()),
            **_caught(session.get(CAUGHT)),
        }

    def _combined(self, select, query, named, depth) -> QueryResult:
        """Answer a statement built from several SELECTs combined into one.

        Each part is answered on its own and the results are merged, which is
        what the operators mean. Three things belong to the statement rather
        than to any one part, and SQL Server puts all three at the end: the
        ORDER BY, the OFFSET and the FETCH. They are taken off the last part
        and applied to the whole.

        Column names come from the first part, and each column's type is
        settled across every part; see precedence. UNION, EXCEPT and INTERSECT
        each drop repeated rows; only UNION ALL keeps them.
        """
        parts = _parts(select)
        last = parts[-1][1]
        answers = []
        for _, part in parts:
            alone = replace(part, combine=(), order_by=(), offset=0, fetch=None)
            answers.append(self._read(alone, query, named, depth + 1))

        columns = answers[0].columns
        for answer in answers[1:]:
            if len(answer.columns) != len(columns):
                raise QueryError(
                    f"all queries combined using a UNION, INTERSECT or EXCEPT "
                    f"operator must have an equal number of expressions in "
                    f"their target lists; this one has {len(columns)} and "
                    f"{len(answer.columns)}",
                    number=UNSUPPORTED,
                )

        columns, branches = _one_type_per_column(columns, answers)
        rows = branches[0]
        for (kind, _), other in zip(parts[1:], branches[1:]):
            if kind == "UNION ALL":
                rows = rows + other
            elif kind == "UNION":
                rows = _distinct(rows + other)
            else:
                theirs = {_signature(row) for row in other}
                wanted = kind == "INTERSECT"
                rows = _distinct(
                    [row for row in rows if (_signature(row) in theirs) == wanted]
                )

        if last.order_by:
            names = [column.name for column in columns]
            known = {name.lower() for name in names}
            for key in last.order_by:
                # The qualifier is not part of the name here. A union's
                # columns are headed by the first select's names, and SQL
                # Server takes ORDER BY f.id as ordering by the column
                # called id however the select list wrote it. Measured: it
                # accepts a qualifier that belongs to only one side, and
                # refuses only a name no output column has.
                wanted = key.column.rsplit(".", 1)[-1].lower()
                if key.position is None and wanted not in known:
                    raise QueryError(
                        f"ORDER BY items must appear in the select list if the "
                        f"statement contains a UNION, INTERSECT or EXCEPT "
                        f"operator; '{key.column}' does not",
                        number=UNSUPPORTED,
                    )
            try:
                rows = _sorted(rows, names, last.order_by,
                               parameters=query.parameters)
            except SourceError as exc:
                raise QueryError(str(exc), number=_number_of(exc)) from exc

        if last.offset:
            rows = rows[last.offset:]
        if last.fetch is not None:
            rows = rows[:last.fetch]
        return QueryResult(columns=columns, rows=rows)

    def _subqueries(self, select, named, depth, parameters=None):
        """Answer each subquery, or arrange for it to be answered per row.

        A subquery that names nothing outside itself has one answer for the
        whole statement, so it is run once here and bound to its parameter. A
        subquery that reads the row around it has an answer per row, so what
        goes into the expression is a value that will ask for one.

        Returns what was bound, what type each produced, and the per-row
        values still to be worked out.
        """
        bound: dict[str, object] = {}
        kinds: dict[str, type] = {}
        deferred: dict[str, object] = {}
        for subquery in select.subqueries:
            try:
                inner = parse_select(subquery.sql)
            except SqlError as exc:
                raise _refused(exc) from exc

            outer = _reads_the_outer_row(inner)
            if outer:
                deferred[subquery.parameter.lstrip("@").lower()] = (
                    self._per_row(subquery, inner, outer, named, depth)
                )
                continue

            answer = self.answer(
                Query(sql=subquery.sql, parameters=dict(parameters or {})),
                select=inner, named=named, depth=depth + 1,
            )
            bound[subquery.parameter], kind = _one_answer(answer, subquery)
            if kind is not None:
                kinds[subquery.parameter] = kind
        return bound, kinds, deferred

    def _applying(self, table: Table, applies: tuple, named, depth,
                  parameters) -> Table:
        """Each APPLY joined to the table, whichever form it takes.

        The values written into a query are worked out where they stand;
        a select is run again for every row, which needs a catalog and so
        happens here rather than beside them.
        """
        for apply in applies:
            if apply.sql is None:
                table = _applied(table, (apply,))
                continue
            table = self._applied_select(table, apply, named, depth, parameters)
        return table

    def _applied_select(self, table: Table, apply, named, depth,
                        parameters) -> Table:
        """A select run for every row of a table, its answers beside them.

        The references to the row around it are rewritten into parameters,
        the same way a correlated subquery's are, so what runs is an ordinary
        select against values handed to it. Answers are kept by the values
        they were asked about, because a table of a thousand rows with twelve
        distinct keys should ask twelve times.
        """
        try:
            inner = parse_select(apply.sql)
        except SqlError as exc:
            raise _refused(exc) from exc

        outer = _reads_the_outer_row(inner)
        places = {
            (column.qualified or column.name).lower(): f"@__applied_{at}"
            for at, column in enumerate(outer)
        }
        wanted = list(dict.fromkeys(places))
        reading = replace(
            inner,
            where=as_parameters(inner.where, places),
            having=as_parameters(inner.having, places),
            items=as_parameters(inner.items, places),
            order_by=as_parameters(inner.order_by, places),
        )
        by_name = dict(zip(wanted, outer))
        names = table.column_names
        answers: dict[tuple, object] = {}

        def ask(row):
            named_row = dict(zip(names, row))
            key = tuple(by_name[one].evaluate(named_row, parameters or {})
                        for one in wanted)
            if key not in answers:
                if len(answers) >= MAX_CORRELATED_ANSWERS:
                    raise QueryError(
                        f"the applied select would be answered more than "
                        f"{MAX_CORRELATED_ANSWERS} times, once for each "
                        f"distinct value it was asked about",
                        number=UNSUPPORTED,
                    )
                asked = dict(parameters or {})
                asked.update({places[one]: value
                              for one, value in zip(wanted, key)})
                answers[key] = self.answer(
                    Query(sql=apply.sql, parameters=asked), select=reading,
                    named=named, depth=depth + 1,
                )
            return answers[key]

        shape = ask(table.rows[0]) if table.rows else self.answer(
            Query(sql=apply.sql,
                  parameters={**(parameters or {}),
                              **{one: None for one in places.values()}}),
            select=reading, named=named, depth=depth + 1,
        )
        columns = list(table.columns) + [
            Column(f"{apply.alias}.{column.name}", column.type)
            for column in shape.columns
        ]
        empty = [None] * len(shape.columns)

        built: list = []
        for row in table.rows:
            found = ask(row)
            for other in found.rows:
                built.append(list(row) + list(other))
            if apply.keep_unmatched and not found.rows:
                built.append(list(row) + empty)
            _check_size(len(built), apply)
        return _typed(Table(name=table.name, columns=columns, rows=built),
                      len(table.columns))

    def _per_row(self, subquery, inner, outer, named, depth) -> Deferred:
        """A value that answers this subquery for whichever row it is shown.

        The references to the outer query are rewritten into parameters, so
        the subquery itself is an ordinary one against values handed to it.
        Answers are kept by the values they were asked about: a correlated
        subquery over a thousand rows with twelve distinct keys runs twelve
        times, and a bound stops a query that would run it a million.
        """
        names = {
            (column.qualified or column.name).lower(): f"@__outer_{at}"
            for at, column in enumerate(outer)
        }
        wanted = list(dict.fromkeys(names))          # in order, without repeats
        reading = replace(
            inner,
            where=as_parameters(inner.where, names),
            having=as_parameters(inner.having, names),
            items=as_parameters(inner.items, names),
            order_by=as_parameters(inner.order_by, names),
        )
        matched = self._matching_once(subquery, reading, names, wanted, outer,
                                      named, depth)
        if matched is not None:
            return Deferred(name=subquery.parameter, produce=matched)

        answers: dict[tuple, object] = {}

        def produce(row, parameters, _by=dict(zip(wanted, outer))):
            key = tuple(
                _by[name].evaluate(row, parameters or {}) for name in wanted
            )
            if key in answers:
                return answers[key]
            if len(answers) >= MAX_CORRELATED_ANSWERS:
                raise QueryError(
                    f"the subquery {subquery.sql!r} would be answered more "
                    f"than {MAX_CORRELATED_ANSWERS} times, once for each "
                    f"distinct value it was asked about",
                    number=UNSUPPORTED,
                )
            asked = dict(parameters or {})
            asked.update({names[name]: value for name, value in zip(wanted, key)})
            answer = self.answer(
                Query(sql=subquery.sql, parameters=asked), select=reading,
                named=named, depth=depth + 1,
            )
            answers[key] = _one_answer(answer, subquery)[0]
            return answers[key]

        return Deferred(name=subquery.parameter, produce=produce)

    def _matching_once(self, subquery, reading, names, wanted, outer,
                       named, depth):
        """EXISTS answered by reading the inner table once, where it can be.

        WHERE EXISTS (SELECT 1 FROM t WHERE t.owner = p.id) asks the same
        question of every outer row: is this value among the owners. Running
        the subquery once per distinct value costs a pass over the inner
        table each time, which is a pass per outer row where the values are
        all different. Read the owners once instead and look each value up.

        Returns None where the shape is anything but that, and then the
        subquery is answered a row at a time as before. What has to hold:
        one outer column, matched by an equality that is ANDed with the
        rest, and nothing in the subquery that decides which rows there are
        after the filter has run. A GROUP BY does decide that, and so do
        TOP, OFFSET and FETCH: dropping the filter would group or count
        over rows the filter would have removed.
        """
        if subquery.kind != "exists" or len(wanted) != 1:
            return None
        if (reading.group_by or reading.having is not None
                or reading.combine or reading.offset or reading.fetch
                or reading.top is not None
                or reading.top_parameter is not None):
            return None
        parts = conjuncts(reading.where)
        if not parts:
            return None
        parameter = names[wanted[0]]
        mentioning = [one for one in parts
                      if mentions_a_parameter(one, [parameter])]
        if len(mentioning) != 1:
            return None
        matching = mentioning[0]
        if not isinstance(matching, Comparison) or matching.operator != "=":
            return None
        sides = (matching.left, matching.right)
        held = [one for one in sides
                if isinstance(one, ParameterRef)
                and _parameter_name(one.name) == _parameter_name(parameter)]
        if len(held) != 1:
            return None
        reads = sides[1] if sides[0] is held[0] else sides[0]
        if mentions_a_parameter(reads, [parameter]):
            return None

        rest = all_of([one for one in parts if one is not matching])
        probe = replace(reading, items=None, where=rest, order_by=())
        found: list = []

        def matching_values(parameters):
            """Every value of the matched column, read once and kept."""
            if found:
                return found[0]
            answer = self.answer(
                Query(sql=subquery.sql, parameters=dict(parameters or {})),
                select=probe, named=named, depth=depth + 1,
            )
            columns = [column.name for column in answer.columns]
            found.append(_Candidates([
                reads.evaluate(dict(zip(columns, row)), parameters or {})
                for row in answer.rows
            ]))
            return found[0]

        def produce(row, parameters, _read=outer[0]):
            value = _read.evaluate(row, parameters or {})
            # NULL matches nothing, because NULL = anything is never true.
            if value is None:
                return 0
            return 1 if matching_values(parameters).holds(value) else 0

        return produce

    def call(self, name: str, arguments: list, parameters: dict) -> QueryResult:
        """Answer a catalog procedure call, or say the procedure is unknown."""
        if not procedures.known(name):
            raise QueryError(
                f"could not find stored procedure '{name}'",
                number=STORED_PROCEDURE_NOT_FOUND,
            )
        return procedures.run(name, self, arguments, parameters)

    def answer(self, request: Query | str, *, select=None, named=None,
               depth: int = 0) -> QueryResult:
        """Handle one batch, as a query handler for a Connection.

        Anything that is not a SELECT completes without a result set. Clients
        open a session with setup batches, and a SET answered with columns
        makes them report an invalid cursor state on the real query.
        """
        query = Query(sql=request) if isinstance(request, str) else request
        if select is not None:
            # Already parsed, because this is a named query or a subquery
            # being run on behalf of the statement that contains it. It keeps
            # the moment the statement around it started at.
            return self._read(select, query, named, depth)

        # When the statement arrived, which is what GETDATE() answers for
        # every row of it. Taken once here rather than per row, because a
        # real server evaluates it once and a filter comparing against a now
        # that moved while it ran would keep different rows for no reason.
        query.session["now"] = datetime.datetime.now()
        try:
            return self._whole_batch(query)
        except QueryError as exc:
            # A batch that stopped takes its transaction with it wherever
            # it stopped. A single read that failed never reaches _batch,
            # so it used to leave the count standing where a real server
            # reads nought. Above the moment, so that a subquery being run
            # for the statement around it is not mistaken for a batch.
            _ended_the_batch(exc, query.session)
            raise

    def _whole_batch(self, query: Query) -> QueryResult:
        """Everything a batch does once the moment it began at is fixed."""
        statement = without_comments(query.sql).lstrip()
        head = statement.upper()

        if query.procedure:
            return self.call(query.procedure, query.arguments, query.parameters)

        # Settled before a single statement runs, because a real server
        # compiles the whole batch first and runs none of it when any of it
        # will not compile: measured, CREATE TABLE #t (a int); SELECT * FROM
        # is msg 102 and leaves no #t. This ran the statements ahead of the
        # broken one and made the table, and it answered a truncated DECLARE
        # or a CREATE TABLE cut off after a comma as though it had worked.
        # The text as sent, comments and all, because a comment left open is
        # one of the ways a batch fails to compile.
        # A variable nothing declared is settled the same way and at the
        # same time, measured: SELECT 'before' AS v; SELECT @zz AS v runs
        # none of itself. This read one as null and answered, so a name
        # spelt wrong came back as an empty result rather than an error.
        refused = (malformed(query.sql)
                   or unbound(statement, known=query.parameters))
        if refused:
            first, *rest = refused
            raise QueryError(
                str(first), number=first.number, severity=15,
                state=first.state,
                following=tuple(QueryError(str(one), number=one.number,
                                           severity=15, state=one.state)
                                for one in rest),
            )

        # Only where the batch is this one call. A batch that opens with EXEC
        # and goes on to other statements is a batch, and reading the whole of
        # it as one procedure name ended it at an unknown procedure where a
        # real server runs everything after the call.
        if (head.startswith(("EXEC ", "EXECUTE "))
                and _written_out(statement) is None
                and len(_statements(statement)) == 1):
            rest = statement.split(None, 1)[1] if " " in statement else ""
            name, _, written = rest.partition(" ")
            name = name.strip().strip(",")
            if procedures.known(name):
                return self.call(name, _arguments(written, query.parameters),
                                 query.parameters)
            # Completing this silently is what produced a NullReferenceException
            # in a client: it asked for a procedure's result set and received
            # nothing, with no error to explain it.
            raise QueryError(
                f"could not find stored procedure '{name or '?'}'",
                number=STORED_PROCEDURE_NOT_FOUND,
            )

        # WITH begins a read as much as SELECT does. Anything else is a
        # setup batch, and a SET answered with columns makes a client report
        # an invalid cursor state on the real query.
        statements = _statements(statement)
        if not any(_RUNS.match(one) or _SET_XACT_ABORT.match(one)
                   for one in statements):
            return QueryResult(columns=[], rows=[])
        if (len(statements) > 1 or not _READS.match(statements[0])
                or _ASSIGNMENT.match(statements[0])
                or select_assignments(statements[0]) is not None
                or _SELECT_INTO.match(statements[0])):
            # More than one statement, or one that has to be run rather than
            # read: an IF chooses between two, an EXEC of a string is a
            # statement written as text, and a SELECT into a variable begins
            # with the word a read begins with and produces no rows.
            return self._batch(statements, query)

        try:
            select = parse_select(statements[0])
        except SqlError as exc:
            raise _refused(exc) from exc

        try:
            return self._counted(
                self._read(select, query, _named(query.session), 0),
                query.session,
            )
        except QueryError as exc:
            # A read that failed while evaluating a row, not while binding,
            # carries the shape it would have declared, so the empty result
            # set a real server sends ahead of the error goes out here too.
            # Still raised: the error is the answer, and every caller of
            # answer() expects that; the connection sends the shape it
            # carries in front of it.
            if exc.columns is None and exc.number in KEEPS_THE_COLUMN_SHAPE:
                exc.columns = _shape_of(select)
            raise

    @staticmethod
    def _counted(answer: QueryResult, session: dict | None) -> QueryResult:
        """Remember how many rows a statement produced, for @@ROWCOUNT.

        Only the statements a client runs, never a subquery or a named query
        inside one: a real server reports what the statement answered, and a
        working step inside it is not something the client ever saw.
        """
        if session is not None:
            session[ROWCOUNT] = len(answer.rows)
        return answer

    def _batch(self, statements: list[str], query: Query) -> QueryResult:
        """Answer a batch of statements, of which one is the read.

        A client asks what it is talking to before it will show a table list,
        and it asks in one batch: declare a variable, set it from a server
        property, select something worked out from it. Nothing else in the
        batch produces rows, so the read is the answer.

        Variables live for the batch, which is as long as they live in a real
        server unless the connection declared them, and are handed to the
        read as parameters because that is what they are by then.
        """
        parameters = dict(query.parameters)
        answers: list[QueryResult] = []
        twice = _made_twice(statements)
        if twice is not None:
            # None of the batch runs: measured, a real server settles a name
            # its own batch declares twice while compiling, and answers with
            # the error alone. Raised before anything runs so that nothing
            # is kept, which is what an empty answers list already means.
            raise QueryError(
                f"There is already an object named '{twice}' in the database.",
                number=ALREADY_AN_OBJECT, state=1,
            )
        try:
            self._run_all(statements, parameters, answers, query.session,
                          catching=True)
        except QueryError as exc:
            # The batch stops here. What ran before it still answered, and a
            # real server has sent those results by the time it reaches this,
            # so they are kept and the error is the last thing the client
            # reads. A compile error is the other case: a real server runs
            # none of the batch, so there is nothing to keep for one.
            # Asked here as well as in answer(), because the branch below
            # keeps what answered and returns rather than raising, so
            # there is nothing for that one to catch.
            _ended_the_batch(exc, query.session)
            if not answers or not _keeps_what_answered(exc, query.session):
                raise
            answers.append(
                QueryResult(columns=exc.columns or [], rows=[], error=exc))
        if len(answers) == 1 and answers[0].error is not None:
            # The whole batch came to one error and nothing else. Raised
            # rather than returned: it is the same bytes on the wire (the
            # connection sends any column shape the error carries ahead of
            # it), and it is what every caller of answer() already expects
            # of a statement that could not be run.
            raise answers[0].error
        if not answers:
            return QueryResult(columns=[], rows=[])
        return replace(answers[0], following=tuple(answers[1:]))

    def _run_all(self, statements: list[str], parameters: dict, answers: list,
                 session: dict | None, *, catching: bool) -> None:
        """Run each statement, keeping what the ones that failed leave behind.

        A real server ends the batch on some errors and carries on past
        others, and the ones it carries on past leave the error among the
        answers rather than in place of them: what the statements around it
        answered still reaches the client.

        Nothing is caught inside a TRY, however deeply nested. The whole
        point of writing one is that its CATCH gets the error, and a block
        that swallowed it first would leave the CATCH unreachable.
        """
        for one in statements:
            try:
                self._statement(one, parameters, answers, session,
                                catching=catching)
            except QueryError as exc:
                if session is not None:
                    session[ERROR_NUMBER] = exc.number
                goes_on = _the_batch_goes_on(exc, session)
                if not catching or not goes_on:
                    raise
                answers.append(
                    QueryResult(columns=exc.columns or [], rows=[], error=exc))
                if session is not None:
                    # Measured: a statement that failed leaves the count at
                    # nought rather than at what the last read answered.
                    session[ROWCOUNT] = 0
            else:
                if session is not None and _clears_the_error(one):
                    session[ERROR_NUMBER] = 0

    def _statement(self, written: str, parameters: dict,
                   answers: list, session: dict | None = None, *,
                   catching: bool = False) -> None:
        """Run one statement of a batch, keeping what it produced.

        Everything a client sends before it will talk to a server: give a
        variable a value, read something, choose between two statements, or
        run one written as text.
        """
        listed = declarations(written)
        if listed is not None and len(listed) > 1:
            # DECLARE @a int = 1, @b int = 2 is each of them in turn, which
            # is what a real server does, measured: a value that fails ends
            # the DECLARE with the ones after it never given theirs, and
            # @@ROWCOUNT is 1 after any that gave a value, the same as the
            # one-variable form. This read everything after the first = as
            # a single value and refused it, or where the first had no
            # value, passed over the rest and left them null.
            for one in listed:
                self._statement(f"DECLARE {one}", parameters, answers,
                                session, catching=catching)
            return

        compound = _COMPOUND_SET.match(written)
        if compound:
            # SET @a += 2 is SET @a = @a + (2), measured for every operator.
            # This passed it over as though it set an option, so @a kept
            # the value it had.
            name, operator, value = compound.groups()
            written = f"SET {name} = {name} {operator} ({value})"

        assigning = select_assignments(written)
        if assigning is not None:
            self._assigned_by_select(assigning, parameters, session)
            return

        declaration = _DECLARES.match(written)
        if declaration:
            kind = PYTHON_FOR.get(type(_declared_type(declaration.group(2))))
            if kind is not None:
                declared = dict(parameters.get(DECLARED) or {})
                declared[declaration.group(1)] = kind
                parameters[DECLARED] = declared

        assignment = _ASSIGNMENT.match(written)
        if assignment:
            name, expression = assignment.group(1), assignment.group(2)
            if self._assigned_from_a_read(name, expression, parameters, session):
                return
            try:
                parameters[name] = parse_expression(expression).evaluate(
                    {}, parameters
                )
            except PredicateError as exc:
                raise _refused(exc) from exc
            if session is not None:
                session[ROWCOUNT] = 1
            return

        guarded = _TRY.match(written)
        if guarded:
            self._tried(written, guarded.end(), parameters, answers, session,
                        catching=catching)
            return

        branch = _IF.match(written)
        if branch:
            taken = _branch_taken(
                written, branch.end(),
                lambda condition: self._condition_holds(
                    condition, parameters, session),
            )
            if session is not None:
                # An IF produced no rows of its own, whatever it went on to
                # run. Measured: after a read of five rows, IF (1 = 0) SELECT
                # 1 leaves the count at nought, and so does an IF whose
                # branch holds only a DECLARE. Set before the branch runs, so
                # that a read inside it still says what it answered.
                session[ROWCOUNT] = 0
                # And the condition was worked out without failing, so the
                # last error is cleared here rather than when the IF ends:
                # measured, an IF whose branch failed still reads that
                # failure afterwards, and one that ran nothing reads nought.
                session[ERROR_NUMBER] = 0
            if taken is not None:
                self._run_all(_block(taken), parameters, answers, session,
                              catching=catching)
            return

        # A block standing on its own, rather than one an IF chose between.
        # The block reader was only ever reached through an IF, so a
        # BEGIN ... END at the top of a batch was passed over and ran none of
        # what it held. Measured: a real server runs it, and BEGIN COMMIT END
        # leaves 3902 behind exactly as a bare COMMIT does. A transaction's
        # BEGIN is not one of these; _block leaves that alone.
        inside = _block(written)
        if inside != [written.strip()]:
            self._run_all(inside, parameters, answers, session,
                          catching=catching)
            return

        if _transaction_statement(written, parameters, session):
            return

        if session is not None and self._session_statement(written, parameters,
                                                           answers, session):
            # The count is left by whichever kind of statement it was, inside
            # the branch that did the work: making and dropping leave nought,
            # and filling leaves the rows put in. Setting it to nought for
            # all four here read as one rule and was two, right for the pair
            # it was reasoned about and quietly wrong for the other pair.
            return

        writes_back = _EXEC_OUTPUT.match(written)
        if writes_back:
            # The value goes into the variable named at the end. Which value
            # is asked for is the last argument before it.
            asked = _arguments(writes_back.group(2), parameters)
            wanted = _text_of(asked[-1]) if asked else ""
            parameters[writes_back.group(3)] = REGISTRY_VALUES.get(
                wanted.strip().upper()
            )
            return

        run = _written_out(written)
        if run is not None:
            # A statement written as text rather than sent as one, with
            # whatever values were named beside it.
            inner, given = run
            parameters.update(given)
            try:
                self._run_all(_statements(inner), parameters, answers,
                              session, catching=catching)
            except QueryError as exc:
                # The text ended, and unless the error is one of the few
                # that reach out of it, the batch that ran the text does
                # not. Inside a TRY nothing is caught here either, because
                # the CATCH is what should see it.
                if not catching or _escapes_written_out(exc):
                    raise
                answers.append(QueryResult(columns=[], rows=[], error=exc))
                if session is not None:
                    session[ERROR_NUMBER] = exc.number
                    session[ROWCOUNT] = 0
            return

        called = _EXEC_NAME.match(written)
        if called and procedures.known(called.group(1)):
            answers.append(self.call(
                called.group(1),
                _arguments(called.group(2) or "", parameters),
                parameters,
            ))
            return
        if called:
            # A procedure that is not here, named inside a batch. The same
            # refusal a batch that is only the call already gives, raised
            # here so that the rest of the batch goes on past it the way a
            # real server's does.
            raise QueryError(
                f"could not find stored procedure '{called.group(1)}'",
                number=STORED_PROCEDURE_NOT_FOUND,
            )

        asked = _raised(written, parameters)
        if asked is not None:
            message, severity, state = asked
            if severity >= LOWEST_SEVERITY_THAT_RAISES:
                # Measured: a real server carries on past one of these, at
                # every severity that raises, so it is marked as asked for
                # rather than run into. The number is the same 50000 this
                # uses for something it cannot do, and those two behave
                # differently, which is what the mark is for.
                raise QueryError(message, number=RAISED_BY_A_CLIENT,
                                 severity=severity, state=state,
                                 carries_on=True)
            # Ten and under is a message rather than an error: measured, it
            # raises nothing and leaves @@ERROR at nought. There is no way
            # to send a message here, so it goes over the way PRINT does
            # and leaves the count where PRINT leaves it.
            if session is not None:
                session[ROWCOUNT] = 0
            return

        throws = _THROW.match(written)
        if throws:
            raise _thrown(throws.group(1), parameters, session)

        setting = _SET_XACT_ABORT.match(written)
        if setting is not None and session is not None:
            # Kept on the connection, because measured it outlives the batch
            # that set it. Nothing is returned here: it falls through to the
            # branch below, where every other SET already ends up and where
            # the row count is put back to nought.
            session[XACT_ABORT] = setting.group(1).upper() == "ON"

        if not _READS.match(written):
            _refuse_a_write(written)
            # PRINT, SET and the rest of what a client sends before it will
            # talk to a server. None of them produced a row, and measured,
            # each of them sets the count to nought. A DECLARE with no value
            # in it arrives here too and is the exception: measured, it is
            # the one statement that leaves the last count standing.
            if session is not None and not declaration:
                session[ROWCOUNT] = 0
            return
        try:
            select = parse_select(written)
        except SqlError as exc:
            raise _refused(exc) from exc
        try:
            answer = self._read(
                select,
                Query(sql=written, parameters=parameters, session=session or {}),
                _named(session), 0,
            )
        except QueryError as exc:
            # The shape it would have declared, for the empty result set a
            # real server sends ahead of a row that failed to evaluate.
            # Carried on the error so the entry built for it above keeps it.
            if exc.columns is None and exc.number in KEEPS_THE_COLUMN_SHAPE:
                exc.columns = _shape_of(select)
            raise
        answers.append(self._counted(answer, session))

    def _tried(self, written: str, at: int, parameters: dict,
               answers: list, session: dict | None, *,
               catching: bool = False) -> None:
        """Run a TRY block, and its CATCH if the TRY could not finish.

        A client writes one around a question this server may not be able to
        answer, and the answer to a question that cannot be answered is the
        CATCH. What the TRY answered before it failed is kept: measured, a
        real server has already sent those rows by the time it reaches the
        failure, so they are the client's. This used to drop them and say
        that a real server dropped them too, which was never true.
        """
        body, at = _up_to(written, at, _END_TRY, _TRY)
        opened = _BEGIN_CATCH.match(written, at)
        caught, _ = (_up_to(written, opened.end(), _END_CATCH, _BEGIN_CATCH)
                     if opened else ("", at))

        try:
            for one in _statements(body):
                self._statement(one, parameters, answers, session,
                                catching=False)
            return
        except QueryError as exc:
            if session is not None:
                # What the CATCH's first statement reads. Measured: @@ERROR
                # inside a CATCH is the number that sent it there.
                session[ERROR_NUMBER] = exc.number
                # And what ERROR_NUMBER() reads for the whole of the CATCH,
                # however many statements run in it.
                session[CAUGHT] = exc
        try:
            self._run_all(_statements(caught), parameters, answers, session,
                          catching=catching)
        finally:
            # Measured: outside the CATCH they answer NULL again, so what
            # was caught goes when the CATCH does, whether it ended well or
            # raised something of its own.
            if session is not None:
                session[CAUGHT] = None

    def _condition_holds(self, condition: str, parameters: dict,
                         session: dict | None) -> bool | None:
        """Whether an IF's condition is true, reading something if it must.

        IF EXISTS (SELECT ...) asks a question of the tables rather than of
        the values to hand, and the parser is what says which kind this is:
        read as a select list, a condition holding a subquery is a read.
        """
        try:
            asked = parse_select(f"SELECT CASE WHEN {condition} THEN 1 ELSE 0 END AS v")
        except SqlError:
            asked = None
        if asked is not None and asked.subqueries:
            answer = self._read(
                asked,
                Query(sql=condition, parameters=parameters,
                      session=session or {}),
                _named(session), 0,
            )
            return bool(answer.rows and answer.rows[0][0] == 1)
        try:
            # With the connection's @@ variables as well as the batch's own,
            # because IF @@TRANCOUNT > 0 is how a client asks whether there
            # is anything to commit.
            return matches(parse_predicate(condition), {},
                           {**_connection_variables(session), **parameters})
        except PredicateError as exc:
            raise _refused(exc) from exc

    def _assigned_by_select(self, assigning, parameters: dict,
                            session: dict | None) -> None:
        """Give each variable a SELECT assigns its value, row by row.

        The SELECT is read as the same statement reading the values it would
        assign, with each value going into its variable the moment it is
        worked out, so every one of the forms a client writes is one path:
        several variables at once, a TOP in front of them, a compound
        operator, and a table or no table. Measured: SELECT @a = 1, @b = @a
        + 1 leaves @b at 2, a failure part way keeps what was assigned
        before it, a TOP 1 over rows in descending order assigns the first,
        and @@ROWCOUNT is the number of rows assigned from, nought where the
        WHERE kept none, which leaves every variable as it was.
        """
        text = assigning.as_a_read()
        try:
            select = parse_select(text)
        except SqlError as exc:
            raise _refused(exc) from exc
        names = [one.variable for one in assigning.assignments]
        answer = self._read(
            select,
            Query(sql=text, parameters=parameters, session=session or {}),
            _named(session), 0, assigning=names, into=parameters,
        )
        if answer.rows:
            for name, value in zip(names, answer.rows[-1]):
                parameters[name] = value
        self._counted(answer, session)

    def _assigned_from_a_read(self, name: str, expression: str,
                              parameters: dict, session: dict | None) -> bool:
        """Give a variable a value a read produced, or say it is not one.

        SELECT @v = something FROM a table, and SET @v = (SELECT ...), both
        have to run a query before there is a value to assign. Whether this
        is one of those is the parser's answer rather than a guess from the
        text: read as a select list, an assignment that names a table or
        holds a subquery is a read, and anything else is an expression.

        The variable ends up holding the value from the last row the read
        produced, and keeps what it had when the read produced none. That is
        what makes the two forms differ where SQL Server has them differ:
        SELECT @v = c FROM t matching no rows leaves the variable alone,
        while SET @v = (SELECT c FROM t) matching none sets it to null,
        because the select around the subquery still has a row and null is
        what is in it.
        """
        try:
            select = parse_select(f"SELECT {expression}")
        except SqlError:
            return False                  # the expression route reports why
        if not (select.table or select.subqueries):
            return False
        answer = self._read(
            select,
            Query(sql=expression, parameters=parameters, session=session or {}),
            _named(session), 0, assigning=[name],
        )
        if answer.rows:
            parameters[name] = answer.rows[-1][0]
        self._counted(answer, session)
        return True

    def _session_statement(self, written: str, parameters: dict,
                           answers: list, session: dict) -> bool:
        """A statement about a table this session made, or False for the rest.

        A temp table is the one thing a read-only bridge writes: it is the
        client's own scratch, it lives on the connection that made it, and it
        goes when that connection does. Nothing a source holds is touched.
        """
        made = _CREATE_TEMP.match(written)
        if made:
            name = made.group(1)
            if name.lower() in session:
                # Measured: msg 2714, and the same words SQL Server uses. A
                # client that makes its scratch table twice has lost track of
                # its own session, and being told nothing is how it stays
                # lost. The first table is left as it was.
                raise QueryError(
                    f"There is already an object named '{name}' in the "
                    f"database.",
                    number=ALREADY_AN_OBJECT, state=6,
                )
            session[name.lower()] = Table(
                name=name,
                columns=_declared_columns(made.group(2)),
                rows=[],
            )
            # Measured: making one leaves the count at nought.
            session[ROWCOUNT] = 0
            return True

        dropped = _DROP_TEMP.match(written)
        if dropped:
            name = dropped.group(1)
            if name.lower() not in session:
                # Measured: msg 3701. Passing it over reports that a table
                # went away, and a client that believes it will make the
                # next one and be told nothing about that either.
                raise QueryError(
                    f"Cannot drop the table '{name}', because it does not "
                    f"exist or you do not have permission.",
                    number=NO_SUCH_TABLE_TO_DROP,
                )
            del session[name.lower()]
            # Measured: dropping one leaves it at nought as well.
            session[ROWCOUNT] = 0
            return True

        made = _SELECT_INTO.match(written)
        if made:
            self._make_from(made, parameters, session)
            return True

        into = _INSERT_TEMP.match(written)
        if into:
            name, rest = into.group(1).lower(), into.group(3)
            table = session.get(name)
            if table is None:
                raise QueryError(
                    f"invalid object name '{into.group(1)}'; it was not "
                    f"created on this connection",
                    number=INVALID_OBJECT_NAME,
                )
            produced = self._rows_for(rest, parameters, session)
            added = _fitted(produced, table.columns, into.group(2))
            session[name] = replace(table, rows=table.rows + added)
            # Measured: an INSERT leaves the count at the rows it put in,
            # one for a single VALUES, two for two of them, and however
            # many a select produced. Set after the rows are worked out,
            # because reading them sets the count itself.
            session[ROWCOUNT] = len(added)
            return True
        return False

    def _make_from(self, made, parameters: dict, session: dict) -> None:
        """Build a session table out of what a select produced.

        The columns are the select list's, names and types alike, so the
        table is whatever shape the answer was. A name already taken is an
        error rather than a replacement, and a column the select list left
        unnamed is one too: there would be nothing to read it back by.
        """
        name = made.group(2)
        if name.lower() in session:
            raise QueryError(
                f"There is already an object named '{name}' in the database.",
                number=ALREADY_AN_OBJECT, state=6,
            )
        # The FROM is optional. Measured: SELECT 1 AS a INTO #t with none
        # makes a table of one row out of the constants, and leaves the
        # count at that one row. This required a FROM and sent the rest to
        # the select parser, which refused it for the FROM it was missing.
        reading = made.group(1)
        if made.group(3):
            reading = f"{reading} {made.group(3)}"
        produced = self._rows_for(reading, parameters, session)
        if any(not column.name for column in produced.columns):
            raise QueryError(NO_NAME_AT_ALL, number=A_COLUMN_WITH_NO_NAME)
        session[name.lower()] = Table(
            name=name,
            columns=list(produced.columns),
            rows=[list(row) for row in produced.rows],
        )
        # Measured: a SELECT INTO leaves the count at the rows it moved,
        # the same as the read that produced them would have.
        session[ROWCOUNT] = len(produced.rows)

    def _rows_for(self, written: str, parameters: dict,
                  session: dict) -> QueryResult:
        """The rows a statement produces, for something else to keep.

        A VALUES list is read here rather than run, because it is not a
        statement: nothing else answers it, and INSERT INTO #t VALUES (1)
        used to come back saying it had produced no rows, which is the form
        everybody writes first.
        """
        spelled = self._values_written(written, parameters)
        if spelled is not None:
            return spelled

        gathered: list = []
        self._statement(written, parameters, gathered, session)
        if not gathered:
            raise QueryError(
                f"{written[:40]!r} produced no rows to insert",
                number=UNSUPPORTED,
            )
        return gathered[0]

    def _values_written(self, written: str, parameters: dict):
        """A VALUES list as a result, or None where the text is not one."""
        try:
            rows = values_written(written.strip())
        except SqlError as exc:
            raise _refused(exc) from exc
        if rows is None:
            return None

        built: list[list[object]] = []
        for row in rows:
            try:
                built.append([one.evaluate({}, parameters or {})
                              for one in row])
            except PredicateError as exc:
                raise QueryError(str(exc),
                                 number=_number_of(exc, UNSUPPORTED)) from exc
        columns, converted = _evaluated_columns(built)
        return QueryResult(columns=columns, rows=converted)

    def _read(self, select, query, named, depth,
              assigning: list | None = None,
              into: dict | None = None) -> QueryResult:
        """Answer one parsed SELECT.

        The named queries are built first, because everything after can refer
        to them: a subquery in the WHERE as much as the FROM.

        `assigning` is the variable each item of a SELECT that assigns gives
        its value to, and the rows come back holding what each was given, row
        by row, so the last of them is what each variable ends as. The rows
        are cut to its TOP before any is assigned from, because a real server
        assigns from the rows it keeps and no others. `into` is the batch's
        own variables, which each assignment is written into as it is made,
        so that one made before a failure part way through is kept.
        """
        if depth > MAX_NESTING:
            raise QueryError(
                f"this query nests more than {MAX_NESTING} deep; a named "
                f"query that refers to itself does that",
                number=UNSUPPORTED,
            )
        parameters = _connection_variables(query.session)
        parameters["@@SERVERNAME"] = (
            self._about(query).get("server") or socket.gethostname()
        )
        parameters[CONTEXT] = self._about(query)
        parameters.update(query.parameters)

        named = dict(named or {})
        for name, definition in select.ctes:
            try:
                named[name.lower()] = self.materialise(
                    definition, named, depth + 1, name, parameters
                )
            except SourceError as exc:
                raise QueryError(str(exc), number=_number_of(exc)) from exc

        if select.combine:
            return self._combined(select, query, named, depth)

        # What the batch declared comes first; a subquery's own answer is
        # better evidence than a declaration and overwrites it.
        produced: dict[str, type] = dict(parameters.get(DECLARED) or {})
        if select.subqueries:
            answers, produced, deferred = self._subqueries(
                select, named, depth, parameters
            )
            parameters.update(answers)
            if deferred:
                select = _asking_per_row(select, deferred)
        query = Query(sql=query.sql, parameters=parameters,
                      procedure=query.procedure,
                      arguments=list(query.arguments))

        if not select.table:
            # SELECT 1, or a function of nothing. One row, no columns to read.
            # The WHERE is asked first where the row would assign, so that
            # SELECT @a = 1 WHERE 1 = 0 leaves @a as it was.
            nothing = Table(name="", columns=[], rows=[[]])
            try:
                if (assigning is not None and select.where is not None
                        and matches(select.where, {}, query.parameters)
                        is not True):
                    return QueryResult(columns=[], rows=[])
                columns, rows = _evaluate(
                    nothing, select.items, query.parameters, produced,
                    select.alias or "", assigns=assigning, into=into,
                )
            except (SourceError, PredicateError) as exc:
                raise QueryError(str(exc), number=_number_of(exc)) from exc
            if select.where is not None and assigning is None:
                try:
                    if matches(select.where, {}, query.parameters) is not True:
                        rows = []
                except PredicateError as exc:
                    raise QueryError(str(exc), number=_number_of(exc)) from exc
            return QueryResult(columns=columns, rows=rows)

        try:
            table = self.resolve(select, named, depth, query.parameters)
        except SourceError as exc:
            raise QueryError(str(exc), number=_number_of(exc)) from exc

        rows = table.rows
        if select.where is not None:
            # Filtering happens before projection, so a condition can name a
            # column the SELECT list does not.
            names = table.column_names
            try:
                rows = [
                    row for row in rows
                    if matches(select.where, dict(zip(names, row)), query.parameters)
                ]
            except PredicateError as exc:
                raise QueryError(str(exc), number=_number_of(exc)) from exc

        if select.is_grouped or select.has_aggregates or select.having is not None:
            if assigning is not None and select.is_grouped and any(
                    mentions_a_parameter(item.node, assigning)
                    for item in select.items if item.node is not None):
                # Each group would have to read what the group before it
                # assigned, and a group's values are worked out together
                # with nothing between them to assign from. Refused rather
                # than answered from the value before the statement. With no
                # GROUP BY there is one row, and the value before the
                # statement is the one it reads.
                raise QueryError(
                    "a SELECT that groups or aggregates and reads a variable "
                    "it assigns is not supported",
                    number=UNSUPPORTED,
                )
            try:
                # An aggregate the HAVING or the ORDER BY names is computed
                # for the group even when nothing asked to see it, and
                # dropped again below.
                asked = list(select.items)
                items = asked + _unlisted_aggregates(select, asked)
                if select.is_grouped:
                    columns, rows = aggregate.group(
                        table, rows, items, list(select.group_by),
                        parameters=query.parameters,
                    )
                else:
                    # No grouping means one group of everything, and one row
                    # out; ordering the input cannot change that.
                    columns, rows = aggregate.compute(
                        table, rows, items, parameters=query.parameters
                    )
            except SourceError as exc:
                raise QueryError(str(exc), number=_number_of(exc)) from exc

            # A grouped column answers to more than its heading, so a HAVING
            # and a sort can name it the way the query wrote it.
            lookup: dict[str, int] = {}
            for index, answers in enumerate(_group_names(items, columns)):
                for answer in answers:
                    lookup.setdefault(answer.lower(), index)

            if select.having is not None:
                rows = _having(select, items, columns, rows, query.parameters)
            if select.order_by:
                names = [column.name for column in columns]
                try:
                    rows = _sorted(rows, names, select.order_by,
                                   parameters=query.parameters, lookup=lookup)
                except SourceError as exc:
                    raise QueryError(
                        str(exc), number=INVALID_OBJECT_NAME
                    ) from exc
            rows = _page(select, rows, query.parameters,
                         _with_ties(select, rows,
                                    [column.name for column in columns],
                                    query.parameters, lookup=lookup))
            if len(items) > len(asked):
                columns = columns[:len(asked)]
                rows = [row[:len(asked)] for row in rows]
            return QueryResult(columns=columns, rows=rows)

        # Every window worked out here, before the sort, which is where SQL
        # Server works them out: over all the rows the WHERE kept, and in
        # their own order rather than the statement's. They come back as
        # columns, so an ORDER BY may name one the way it names any other.
        source_columns = len(table.columns)
        try:
            table = _with_windows(table, rows, select.items, query.parameters)
        except SourceError as exc:
            raise QueryError(str(exc), number=_number_of(exc)) from exc
        rows = table.rows

        if select.order_by:
            try:
                rows = _sorted(rows, table.column_names, select.order_by,
                               items=select.items, parameters=query.parameters)
            except SourceError as exc:
                raise QueryError(str(exc), number=_number_of(exc)) from exc

        if select.top_ties and select.distinct:
            raise QueryError(
                "TOP ... WITH TIES beside DISTINCT is not supported; the "
                "ties are on what the sort said and DISTINCT decides which "
                "rows there are to sort",
                number=UNSUPPORTED,
            )
        ties = _with_ties(select, rows, table.column_names, query.parameters,
                          items=select.items)
        if assigning is not None:
            if select.distinct:
                raise QueryError(
                    "a SELECT DISTINCT that assigns a variable is not "
                    "supported", number=UNSUPPORTED)
            # Cut to the TOP before a row assigns anything, measured: SELECT
            # TOP 1 @a = v ... ORDER BY v DESC assigns the largest v and no
            # other. Cutting afterwards had every row assign first.
            rows = _page(select, rows, query.parameters, ties)

        filtered = Table(name=table.name, columns=table.columns, rows=rows)
        try:
            if select.items is None or select.is_projection:
                columns, rows = filtered.select(select.columns)
                if select.items is not None:
                    # A select list may rename what it selects.
                    columns = [
                        Column(item.output_name, column.type)
                        for item, column in zip(select.items, columns)
                    ]
            else:
                columns, rows = _evaluate(
                    filtered, select.items, query.parameters, produced,
                    select.alias or "", source_columns, assigns=assigning,
                    into=into,
                )
        except SourceError as exc:
            raise QueryError(str(exc), number=_number_of(exc)) from exc
        except PredicateError as exc:
            raise QueryError(str(exc), number=_number_of(exc)) from exc

        if assigning is not None:
            return QueryResult(columns=columns, rows=rows)
        if select.distinct:
            rows = _distinct(rows)

        rows = _page(select, rows, query.parameters, ties)
        return QueryResult(columns=columns, rows=rows)


def load(config_path: str | Path) -> Catalog:
    """Build a catalog from a configuration file.

        {
          "tables": [
            {"name": "people", "csv":  "data/people.csv"},
            {"name": "sales",  "csv":  "data/sales.csv", "delimiter": ";"},
            {"name": "cities", "json": "data/cities.json"},
            {"name": "pokemon", "http": "https://pokeapi.co/api/v2/pokemon"}
          ],
          "discover": [
            {"url": "https://pokeapi.co/api/v2/"}
          ]
        }

    Relative paths resolve against the configuration file's own directory, so
    a config and its data can be moved together.

    "discover" points at the base of an API and crawls it, which is the whole
    of the configuration for a server that describes itself. "tables" names
    sources one at a time, for the cases discovery cannot reach or gets wrong.
    Both may appear; a named table wins over a discovered one of the same name,
    because a person who wrote a name meant it.
    """
    path = Path(config_path)
    try:
        # utf-8-sig: a config written by Notepad or PowerShell carries a
        # byte order mark, and json.loads refuses one.
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise SourceError(f"could not read '{path}': {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SourceError(f"'{path}' is not valid JSON: {exc}") from exc

    entries = document.get("tables") or []
    surfaces = document.get("discover") or []
    if not isinstance(entries, list):
        raise SourceError(f"'{path}': \"tables\" must be an array")
    if not isinstance(surfaces, list):
        raise SourceError(f"'{path}': \"discover\" must be an array")
    if not entries and not surfaces:
        raise SourceError(
            f"'{path}' needs a \"tables\" array, a \"discover\" array, or both"
        )

    catalog = Catalog()

    # Discovery runs first so that an explicitly named table overwrites a
    # discovered one rather than colliding with it.
    for position, surface in enumerate(surfaces, start=1):
        for discovered in _discovered_sources(surface, position, path):
            catalog.add_source(discovered)

    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise SourceError(f"'{path}' table {position} is not an object")

        for option, belongs_to in OPTIONS_OF.items():
            if option in entry and not any(kind in entry for kind in belongs_to):
                raise SourceError(
                    f'\'{path}\' table {position} names a "{option}", which '
                    f"only {_only_beside(belongs_to)}"
                )
        # Every reader answers with a list, because two of them read a file
        # that holds more than one table. A workbook has sheets and an Access
        # database has tables, and serving the first and dropping the rest
        # would lose them with nothing said.
        readers = {
            "csv": lambda p, name=None: [from_csv(
                p, name=name, delimiter=entry.get("delimiter", ","))],
            "json": lambda p, name=None: [from_json(p, name=name)],
            "xml": lambda p, name=None: [from_markup(p, "xml", name=name)],
            "html": lambda p, name=None: [from_markup(p, "html", name=name)],
            "excel": lambda p, name=None: from_excel(
                p, name=name, sheet=entry.get("sheet"),
                table=entry.get("table")),
            "access": lambda p, name=None: from_access(
                p, name=name, table=entry.get("table")),
        }
        kinds = sorted([*readers, "http"])
        given = [key for key in kinds if key in entry]
        if len(given) != 1:
            raise SourceError(
                f"'{path}' table {position} needs exactly one of "
                f"{', '.join(kinds)}, found {len(given)}"
            )

        _only_known(
            entry, TABLE_KEYS, f"'{path}' table {position}",
            inside=(HTTP_KEYS, '"http"') if "http" in entry else None,
        )

        kind = given[0]
        if kind == "http":
            catalog.add_source(_http_source(entry, position, path))
        else:
            source_path = (path.parent / entry[kind]).resolve()
            for table in readers[kind](source_path, name=entry.get("name")):
                catalog.add(table)

    return catalog


# What each part of a configuration reads. Anything else is refused rather
# than ignored: an option written one level too high, or spelled slightly
# wrong, is the one mistake a config file cannot recover from on its own,
# because the file looks right and the source behaves as though the line were
# not there.
TABLE_KEYS = frozenset({"name", "csv", "json", "xml", "html", "excel",
                        "access", "http", "delimiter", "sheet", "table"})

# An option that only makes sense beside some kinds of source, and the kinds
# it belongs to. Written beside any other kind it is refused rather than
# ignored, because a setting that does nothing looks exactly like a setting
# that did not work.
#
# "table" belongs to two of them. A workbook and a database both hold things
# called tables, and the word picking one out of either is better than a
# second word meaning the same thing in one of them.
OPTIONS_OF = {
    "delimiter": ("csv",),
    "sheet": ("excel",),
    "table": ("excel", "access"),
}
HTTP_KEYS = frozenset({
    "url", "name", "path", "records", "format", "expand", "flatten", "columns",
    "headers", "auth", "next", "paging", "max_pages", "max_rows", "timeout",
    "ttl",
})
PAGING_KEYS = frozenset({"key", "parameter", "step"})
DISCOVER_KEYS = frozenset({
    "url", "prefix", "headers", "auth", "guess", "concurrency", "max_requests",
    "max_depth", "max_pages", "max_rows", "expand", "timeout", "ttl",
})


def _only_beside(kinds: tuple[str, ...]) -> str:
    """How to say which sources an option belongs to."""
    quoted = [f'"{kind}"' for kind in kinds]
    if len(quoted) == 1:
        return f"a {quoted[0]} table has"
    return f"{' or '.join(quoted)} tables have"


def _only_known(spec: dict, known: frozenset, where: str,
                inside: tuple[frozenset, str] | None = None) -> None:
    """Refuse a key nobody reads, saying where the one meant would have gone.

    A key that is an option one level down is named as such, because writing
    an http option beside "http" rather than in it is the mistake this catches
    most often. Otherwise the nearest known key is offered.
    """
    for key in spec:
        if key in known:
            continue
        if inside and key in inside[0]:
            raise SourceError(
                f'{where}: "{key}" belongs inside {inside[1]}, not beside it'
            )
        near = difflib.get_close_matches(str(key), sorted(known), n=1)
        suggestion = f'; did you mean "{near[0]}"?' if near else ""
        raise SourceError(f'{where}: "{key}" is not an option here{suggestion}')


def _http_source(entry: dict, position: int, config: Path) -> HttpSource:
    """Build an HTTP source from one configuration entry.

        {"name": "pokemon",
         "http": {"url": "https://pokeapi.co/api/v2/pokemon?limit=50",
                  "path": "results", "ttl": 300, "timeout": 30}}

    The URL may also be given as a bare string when nothing else is needed.
    """
    spec = entry["http"]
    if isinstance(spec, str):
        spec = {"url": spec}
    if isinstance(spec, list):
        spec = {"url": spec}
    if not isinstance(spec, dict) or "url" not in spec:
        raise SourceError(
            f"{config} table {position}: http needs a url, either as a "
            f"string or as an object with a url key"
        )

    _only_known(spec, HTTP_KEYS, f"{config} table {position}: http")

    name = entry.get("name") or spec.get("name")
    if not name:
        raise SourceError(
            f"{config} table {position}: an http source needs a name, "
            f"because a URL has no obvious table name"
        )

    headers = spec.get("headers") or {}
    if not isinstance(headers, dict):
        raise SourceError(f"{config} table {position}: headers must be an object")

    records = spec.get("records", "auto")
    if records not in STRATEGIES:
        raise SourceError(
            f"{config} table {position}: '{records}' is not a records strategy; "
            f"use one of {', '.join(STRATEGIES)}"
        )

    expand = spec.get("expand", True)
    if not isinstance(expand, bool):
        raise SourceError(f"{config} table {position}: expand must be true or false")

    document = spec.get("format", "auto")
    if document not in FORMATS:
        raise SourceError(
            f"{config} table {position}: '{document}' is not a format; use "
            f"one of {', '.join(FORMATS)}"
        )

    columns = spec.get("columns")
    if columns is not None and not (
        isinstance(columns, list) and all(isinstance(c, str) for c in columns)
    ):
        raise SourceError(
            f"{config} table {position}: columns must be a list of names"
        )

    paging = _paging_spec(spec.get("paging"), f"{config} table {position}")

    url = spec["url"]
    if isinstance(url, list):
        if not url or not all(isinstance(u, str) and u for u in url):
            raise SourceError(
                f"{config} table {position}: a list of urls must hold at least "
                f"one non-empty string"
            )
    elif not isinstance(url, str):
        raise SourceError(
            f"{config} table {position}: url must be a string, or a list of "
            f"them for a load-balanced set"
        )

    return HttpSource(
        name=name,
        url=url,
        path=spec.get("path"),
        records=records,
        format=document,
        expand=expand,
        next_key=spec.get("next"),
        paging=paging,
        max_pages=int(spec.get("max_pages", DEFAULT_MAX_PAGES)),
        max_rows=int(spec.get("max_rows", DEFAULT_MAX_ROWS)),
        flatten=bool(spec.get("flatten", True)),
        columns=columns,
        timeout=float(spec.get("timeout", DEFAULT_TIMEOUT_SECONDS)),
        ttl=float(spec.get("ttl", DEFAULT_TTL_SECONDS)),
        headers={str(k): str(v) for k, v in headers.items()},
        auth=credential(spec.get("auth"),
                        what=f"{config} table {position}: auth"),
    )


def _paging_spec(spec: object, where: str) -> Paging | None:
    """Read a paging rule from configuration.

        {"paging": {"key": "skip", "parameter": "skip", "step": 30}}

    Discovery writes this out for an API that reports its position instead of
    linking to the next page, so it has to read back in: a config anyone can
    regenerate is only useful if it can also be edited and reloaded.
    """
    if spec is None:
        return None
    if not isinstance(spec, dict) or "key" not in spec:
        raise SourceError(
            f'{where}: paging must be an object with a "key", and optionally '
            f'a "parameter" and a "step"'
        )
    _only_known(spec, PAGING_KEYS, f"{where}: paging")

    key = str(spec["key"])
    try:
        step = int(spec.get("step", 1))
    except (TypeError, ValueError):
        raise SourceError(f"{where}: paging step must be a whole number") from None
    if step < 1:
        raise SourceError(f"{where}: paging step must be at least 1")
    return Paging(
        key=key,
        parameter=str(spec.get("parameter", key.rsplit(".", 1)[-1])),
        step=step,
    )


def _discovered_sources(spec: object, position: int, config: Path) -> list[HttpSource]:
    """Crawl one API surface and turn what it holds into sources.

        {"url": "https://pokeapi.co/api/v2/",
         "auth": {"bearer": "${API_TOKEN}"},
         "prefix": "poke", "max_requests": 60}

    This runs while the configuration is being read, which means starting the
    server costs one crawl. That is the right moment for it: a client asks for
    the table list immediately after connecting, and discovering the surface
    then would make the first query wait for a walk of somebody else API.
    """
    where = f"{config} discover {position}"
    if isinstance(spec, str):
        spec = {"url": spec}
    if not isinstance(spec, dict) or not isinstance(spec.get("url"), str):
        raise SourceError(
            f"{where}: needs a url, either as a string or as an object with "
            f"a url key"
        )

    _only_known(spec, DISCOVER_KEYS, where)

    headers = spec.get("headers") or {}
    if not isinstance(headers, dict):
        raise SourceError(f"{where}: headers must be an object")
    headers = {str(k): str(v) for k, v in headers.items()}

    auth = credential(spec.get("auth"), what=f"{where}: auth")
    prefix = str(spec.get("prefix", ""))
    found = discover.survey(
        spec["url"],
        auth=auth,
        headers=headers,
        max_requests=int(spec.get("max_requests", discover.DEFAULT_MAX_REQUESTS)),
        max_depth=int(spec.get("max_depth", discover.DEFAULT_MAX_DEPTH)),
        concurrency=int(spec.get("concurrency", discover.DEFAULT_CONCURRENCY)),
        timeout=float(spec.get("timeout", DEFAULT_TIMEOUT_SECONDS)),
        guess=bool(spec.get("guess", True)),
    )

    ttl = float(spec.get("ttl", DEFAULT_TTL_SECONDS))
    timeout = float(spec.get("timeout", DEFAULT_TIMEOUT_SECONDS))
    max_pages = int(spec.get("max_pages", DEFAULT_MAX_PAGES))
    max_rows = int(spec.get("max_rows", DEFAULT_MAX_ROWS))

    sources = []
    for resource in found.resources:
        source = HttpSource(
            name=f"{prefix}_{resource.name}" if prefix else resource.name,
            url=resource.url,
            path=resource.shape.path,
            records=resource.shape.records,
            next_key=resource.next_key,
            paging=resource.paging,
            expand=bool(spec.get("expand", True)),
            max_pages=max_pages,
            max_rows=max_rows,
            headers=headers,
            auth=auth,
            timeout=timeout,
            ttl=ttl,
        )
        if resource.table is not None:
            # The crawl already fetched and shaped this one. Handing it over
            # saves the whole surface being fetched twice within a second.
            source.prime(resource.table)
        sources.append(source)
    return sources


def _branch_taken(written: str, at: int, holds) -> str | None:
    """Which statement an IF chooses, or None when it chooses neither.

    The condition runs to wherever the statement after it begins, which is a
    word no condition can end with. T-SQL needs no semicolon between the two,
    so nothing else marks the boundary.

    Whether the condition is true is decided by the caller, because deciding
    it may mean reading a table: IF EXISTS (SELECT ...) is a question about
    what is served rather than about the values to hand.

    The branch then runs to its own end, blocks and all, and only an ELSE
    directly after that belongs to this IF. Taking the first one instead
    handed the inner branch of a nested IF to the outer one, along with the
    END that closed the block around it.
    """
    start = _statement_start(written, at)
    if start is None:
        raise QueryError(
            f"cannot tell where the condition ends in {written[:40]!r}",
            number=UNSUPPORTED,
        )
    condition = written[at:start].strip()
    rest = written[start:]

    finish = end_of_branch(rest, 0)
    otherwise = _ELSE.match(rest, finish)
    if otherwise is None:
        taken, alternative = rest, None
    else:
        taken = rest[:finish]
        alternative = rest[otherwise.end():]

    if holds(condition) is True:
        return taken.strip()
    return alternative.strip() if alternative else None


def _up_to(written: str, at: int, ending, opening=None) -> tuple[str, int]:
    """The text before the closing word that matches, and where it ended.

    Given the opening word as well, the ones opened in between are counted,
    so a TRY inside a TRY closes at its own END TRY rather than at the first
    one along. Without that the outer body was cut at the inner END TRY, the
    inner BEGIN CATCH was taken for the outer's, and everything after it was
    dropped, so a nested TRY answered nothing at all.

    Without an opening word it stops at the first closer, which is what
    every other caller wants and what this did before.
    """
    depth = 0
    cursor = at
    while True:
        closes = _statement_start(written, cursor, wanted=ending)
        if closes is None:
            return written[at:], len(written)
        opens = (_statement_start(written, cursor, wanted=opening)
                 if opening is not None else None)
        if opens is not None and opens < closes:
            depth += 1
            cursor = opening.match(written, opens).end()
            continue
        if depth == 0:
            return written[at:closes], ending.match(written, closes).end()
        depth -= 1
        cursor = ending.match(written, closes).end()


def _statement_start(text: str, at: int, wanted=None) -> int | None:
    """Where the next statement begins, skipping anything quoted."""
    depth = 0
    while at < len(text):
        char = text[at]
        if char in "'\"":
            at = _skip_quoted(text, at, char)
            continue
        if char == "[":
            found = text.find("]", at)
            at = len(text) if found < 0 else found + 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            word = re.compile(r"[A-Za-z_][A-Za-z0-9_]*").match(text, at)
            if word:
                if wanted is not None:
                    if wanted.match(text, at):
                        return at
                elif word.group(0).upper() in STATEMENT_WORDS:
                    return at
                at = word.end()
                continue
        at += 1
    return None


def _named(session: dict | None) -> dict:
    """The session's own tables, under the names a query calls them by."""
    return dict(session or {})


def _caught(error) -> dict:
    """What the CATCH functions answer about the error being handled.

    All NULL outside a CATCH, which is what a real server answers there.
    The line is NULL rather than a number: a real server reports the line
    within the batch, and nothing here counts them, so a number would be
    invented. The procedure is NULL because nothing here runs in one.
    """
    if error is None:
        return {
            "error_number": None, "error_message": None,
            "error_severity": None, "error_state": None,
            "error_line": None, "error_procedure": None,
        }
    return {
        "error_number": error.number,
        "error_message": str(error),
        "error_severity": error.severity,
        "error_state": error.state,
        "error_line": None,
        "error_procedure": None,
    }


def _made_twice(statements: list) -> str | None:
    """The temp table a batch declares twice, as written, or None.

    Measured on SQL Server 2025: a name one batch declares twice is settled
    while it compiles, so none of the batch runs and a read before it never
    answers, where a batch remaking a table an earlier one made runs up to
    the failure and keeps what it answered. A DROP between the two does not
    save it, and SELECT ... INTO declares a name exactly as CREATE TABLE
    does. The server tells the two apart itself: state 1 while compiling
    and state 6 while running.
    """
    seen = set()
    for one in statements:
        made = _CREATE_TEMP.match(one)
        into = None if made else _SELECT_INTO.match(one)
        if not (made or into):
            continue
        name = made.group(1) if made else into.group(2)
        if name.lower() in seen:
            return name
        seen.add(name.lower())
    return None


def _rolls_the_transaction_back(exc: QueryError,
                                session: dict | None) -> bool:
    """Whether a batch ending at this error leaves no transaction open.

    Measured on SQL Server 2025, with the transaction opened by an earlier
    batch because that is how a client writes it. A batch that ends rolls
    it back and takes every nesting level at once, with two exceptions. A
    real compile error never rolls back, with the setting on or off:
    nothing compiled, so there was nothing to undo. 208 is the odd one,
    kept with XACT_ABORT off and rolled back with it on, and it is the
    only number whose answer here the setting changes.

    A batch that carries on past its error keeps the transaction, and
    never reaches this: it is asked only where the batch ended.

    This server's own refusals have no counterpart on a real server and
    are unmeasured. They keep the transaction, which is the safer of the
    two to be wrong about: a client that commits one this threw away
    reads 3902, where one left open costs it nothing, because its next
    IF @@TRANCOUNT > 0 ROLLBACK simply works. Read through the mark
    rather than the number, because a THROW may carry 50000 too and a
    THROW rolls back.
    """
    if exc.carries_on is None:
        if exc.number in (UNSUPPORTED, SOURCE_UNAVAILABLE):
            return False
        if exc.number in SETTLED_WHILE_COMPILING:
            return False
        if exc.number == INVALID_OBJECT_NAME:
            return bool(session is not None and session.get(XACT_ABORT))
    return True


def _ended_the_batch(exc: QueryError, session: dict | None) -> None:
    """Close the connection's transactions where this error ended the batch.

    Only where it ended one. Measured: a batch of a single statement whose
    error is one a batch runs on past keeps its transaction, the same as a
    batch that had somewhere to carry on to, and the setting turning that
    error into an ender is what takes it. Asking on every error instead
    would have rolled back a lone divide by zero, which a real server
    keeps.
    """
    if session is None or _the_batch_goes_on(exc, session):
        return
    if _rolls_the_transaction_back(exc, session):
        _transactions(session).end()


def _shape_of(select) -> list | None:
    """The columns a SELECT declares, worked out without running it.

    For the empty result set a real server sends in front of a row that
    failed to evaluate: it declared the shape when it bound the query,
    before the row ran. Only where every column's type can be said from
    the select list alone, which is the same thing column_of does for a
    column with no values to read. Anything less returns None and the
    caller sends no metadata, which is what happened before and what a
    real server does for an error it hit while binding.

    A star, or a list with a plain column or a bare literal whose type
    this cannot state without the source, is one of those: it returns
    None rather than a guess.
    """
    if not select.items:
        return None
    columns = []
    for item in select.items:
        kind = _kind_of(item.node, {}) if item.node is not None else None
        declared = DECLARED_FOR.get(kind)
        if declared is None:
            return None
        columns.append(Column(item.output_name, declared))
    return columns


def _the_batch_goes_on(exc: QueryError, session: dict | None = None) -> bool:
    """Whether the rest of the batch runs, with this error among its answers.

    An error a client asked for answers this itself and XACT_ABORT does not
    reach it: measured, a RAISERROR still lets a batch run on with the
    setting on, and a THROW still ends one.
    """
    if exc.carries_on is not None:
        return exc.carries_on
    if exc.number not in THE_BATCH_GOES_ON:
        return False
    if (session is not None and session.get(XACT_ABORT)
            and exc.number not in THE_SETTING_DOES_NOT_REACH):
        return False
    return True


def _keeps_what_answered(exc: QueryError, session: dict | None = None) -> bool:
    """Whether what answered before this error still reaches the client.

    An error that ends a batch does not always throw away what the batch had
    already sent: a real server has those rows out of the door before it
    reaches the failure. A compile error is the other case, and a number in
    neither set is treated as one, which is what the client sees for
    anything this server cannot make sense of.

    XACT_ABORT moves an error from one set to the other and stops there:
    measured, a batch it ends still answers everything before the failure,
    so the setting changes where a batch stops and not what it keeps.
    """
    if exc.carries_on is not None:
        return True
    if exc.number in THE_BATCH_ENDS_AFTER:
        return True
    return bool(session is not None and session.get(XACT_ABORT)
                and exc.number in THE_BATCH_GOES_ON)


def _escapes_written_out(exc: QueryError) -> bool:
    """Whether this ends the batch that ran an EXEC of text, not only the text.

    Measured: a THROW inside one ends both, and a RAISERROR inside one ends
    neither, so the two errors a client asks for fall on opposite sides of
    this and the mark each carries is what says which.
    """
    if exc.carries_on is not None:
        return not exc.carries_on
    return exc.number in ESCAPES_WRITTEN_OUT


def _split_outside_quotes(text: str) -> list:
    """The comma-separated parts of an argument list, quoted runs respected.

    Splitting on every comma cuts a quoted string that holds one in half and
    leaves a stray quote on the tail. A RAISERROR message with a comma in it
    is an ordinary thing to write, and so is a table whose name holds one:
    a JSON key, a CSV header and an Excel sheet name all allow one, and a
    client asking that table for its columns sends the name as a single
    quoted argument.

    Either quote character, because an EXEC argument may be written with
    either and a comma inside one is as ordinary there.
    """
    parts = []
    at = start = 0
    while at < len(text):
        if text[at] in "'\"":
            at = _skip_quoted(text, at, text[at])
            continue
        if text[at] == ",":
            parts.append(text[start:at].strip())
            start = at = at + 1
            continue
        at += 1
    parts.append(text[start:].strip())
    return parts


def _message_argument(written: str, parameters: dict) -> str:
    """The text of a message argument, written out or held in a variable."""
    if written[:1] in ("N", "n") and written[1:2] == "'":
        written = written[1:]
    if written.startswith("'"):
        return written[1:_skip_quoted(written, 0, "'") - 1].replace("''", "'")
    return str(_evaluated(written, parameters))


def _thrown(rest: str, parameters: dict, session: dict | None) -> QueryError:
    """The error a THROW asks for: the one it names, or the one it re-raises.

    Every value here was measured on SQL Server 2025. Unlike RAISERROR this
    ends the batch, so the error is marked rather than left to its number:
    the number is the client's to choose and says nothing about what the
    batch does next.
    """
    rest = rest.strip().rstrip(";").strip()
    if not rest:
        caught = session.get(CAUGHT) if session is not None else None
        if caught is None:
            return QueryError(
                "To rethrow an error, a THROW statement must be used inside "
                "a CATCH block. Insert the THROW statement inside a CATCH "
                "block, or add error parameters to the THROW statement.",
                number=NOTHING_TO_RETHROW, severity=15,
            )
        # A bare THROW re-raises what its CATCH caught, whole: measured, the
        # number, severity, state and words all come back. It ends the batch
        # even where the error it re-raises would not have, which is the
        # clearest case for marking the error rather than reading its number.
        return QueryError(str(caught), number=caught.number,
                          severity=caught.severity, state=caught.state,
                          carries_on=False)

    given = _split_outside_quotes(rest)
    if len(given) != 3:
        # Measured: a real server takes three arguments or none, and refuses
        # two while it compiles, so nothing in the batch answers.
        return QueryError(f"Incorrect syntax near '{rest}'.",
                          number=SYNTAX_ERROR, severity=15)

    number = _evaluated(given[0], parameters)
    state = _evaluated(given[2], parameters)
    if not isinstance(number, int) or isinstance(number, bool):
        return QueryError(f"Incorrect syntax near '{given[0]}'.",
                          number=SYNTAX_ERROR, severity=15)
    if not (LOWEST_NUMBER_A_THROW_MAY_RAISE <= number
            <= HIGHEST_NUMBER_A_THROW_MAY_RAISE):
        return QueryError(
            f"Error number {number} in the THROW statement is outside the "
            f"valid range. Specify an error number in the valid range of "
            f"{LOWEST_NUMBER_A_THROW_MAY_RAISE} to "
            f"{HIGHEST_NUMBER_A_THROW_MAY_RAISE}.",
            number=THROWN_NUMBER_OUT_OF_RANGE,
            severity=SEVERITY_OF_A_THROW, state=10, carries_on=False,
        )
    return QueryError(_message_argument(given[1], parameters), number=number,
                      severity=SEVERITY_OF_A_THROW, state=int(state or 1),
                      carries_on=False)


def _raised(written: str, parameters: dict):
    """What a RAISERROR names: its message, severity and state, or None.

    Its own parsing rather than the shared argument reader, which splits on
    a comma inside a quoted string and leaves a doubled quote doubled. A
    message with a comma in it is an ordinary thing to write, and it would
    have arrived cut in half. The quote skipper handles both, measured.
    """
    opened = _RAISERROR.match(written)
    if opened is None:
        return None

    at = opened.end()
    while at < len(written) and written[at].isspace():
        at += 1
    # N'...' is the same string, said in the other prefix.
    if written[at:at + 1] in ("N", "n") and written[at + 1:at + 2] == "'":
        at += 1

    if written[at:at + 1] == "'":
        closed = _skip_quoted(written, at, "'")
        message = written[at + 1:closed - 1].replace("''", "'")
        at = closed
    else:
        # A variable holding the text, which ends at the comma after it.
        ends = written.find(",", at)
        if ends < 0:
            return None
        message = _evaluated(written[at:ends], parameters)
        at = ends

    rest = written[at:].strip()
    if rest.endswith(")"):
        rest = rest[:-1]
    given = [one.strip() for one in rest.lstrip(",").split(",") if one.strip()]
    severity = _evaluated(given[0], parameters) if given else 0
    state = _evaluated(given[1], parameters) if len(given) > 1 else 1
    return str(message), int(severity or 0), int(state or 1)


def _evaluated(written: str, parameters: dict):
    """A number or a variable's value, or None where it is neither."""
    try:
        return parse_expression(written).evaluate({}, parameters)
    except PredicateError:
        return None


def _clears_the_error(written: str) -> bool:
    """Whether finishing this statement puts @@ERROR back to nought.

    Every statement does, with three exceptions, each of them measured. A
    DECLARE with no value leaves it standing, the same way it leaves
    @@ROWCOUNT standing; an IF and an EXEC of text leave it to whatever they
    ran, because IF 1 = 1 BEGIN COMMIT END still reads 3902 afterwards. A
    TRY is not among them: it clears the error once its CATCH has dealt with
    it, which is what writing one is for. A DECLARE of several variables
    clears it where any of them is given a value, measured both ways round.
    """
    listed = declarations(written)
    if listed is not None and not any(
            _ASSIGNMENT.match(f"DECLARE {one}") for one in listed):
        return False
    if _IF.match(written) or _written_out(written) is not None:
        return False
    return True


def _block(written: str) -> list:
    """The statements a branch holds, whether or not it is a BEGIN block."""
    stripped = written.strip()
    if (_BEGIN.match(stripped) and not _BEGIN_TRANSACTION.match(stripped)
            and stripped.upper().endswith("END")):
        inner = stripped[_BEGIN.match(stripped).end():-3]
        return _statements(inner)
    return [stripped]


def _declared_columns(written: str) -> list:
    """The columns a CREATE TABLE declared, in the order it declared them."""
    columns = []
    for one in _split_declarations(written):
        parts = one.split(None, 1)
        if not parts:
            continue
        name = parts[0].strip("[]\"")
        written_type = parts[1] if len(parts) > 1 else "nvarchar"
        columns.append(Column(name, _declared_type(written_type)))
    if not columns:
        raise QueryError("a table needs at least one column", number=UNSUPPORTED)
    return columns


def _split_declarations(written: str) -> list:
    """One column declaration per entry, ignoring commas inside brackets."""
    found, depth, start = [], 0, 0
    for at, char in enumerate(written):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            found.append(written[start:at])
            start = at + 1
    found.append(written[start:])
    return [one.strip() for one in found if one.strip()]


def _declared_type(written: str) -> object:
    """What a column declared as this holds, as far as this serves types."""
    name = written.strip().split("(")[0].strip().upper()
    size = re.search(r"\((\d+)\)", written)
    if name in ("INT", "INTEGER", "SMALLINT", "TINYINT"):
        return Integer(4)
    if name == "BIGINT":
        return Integer(8)
    if name in ("FLOAT", "REAL", "DECIMAL", "NUMERIC", "MONEY"):
        return Float(8)
    if name == "BIT":
        return Bit()
    if name == "SYSNAME":
        return NVarChar(128)
    return NVarChar(int(size.group(1)) if size else 4000)


def _fitted(produced, columns: list, named: str | None = None) -> list:
    """The rows a statement produced, laid against the columns they go into.

    By position where the insert named none, which is how INSERT works, and
    refused when the counts differ rather than padded with nulls. Where it
    named some, the values go into those and the rest of the row is NULL,
    in the order the insert wrote them rather than the order the table has
    them: INSERT #t (b, a) SELECT name, id puts the name in b.
    """
    if named is None:
        if produced.columns and len(produced.columns) != len(columns):
            raise QueryError(
                "Column name or number of supplied values does not match "
                "table definition.",
                number=DOES_NOT_MATCH_THE_TABLE,
            )
        return [list(row) for row in produced.rows]

    wanted = [one.strip().strip("[]").lower()
              for one in named.split(",") if one.strip()]
    if len(produced.columns) != len(wanted):
        # Two messages, one for each direction, because SQL Server has two.
        fewer = len(produced.columns) < len(wanted)
        raise QueryError(
            f"The select list for the INSERT statement contains "
            f"{'fewer' if fewer else 'more'} items than the insert list. The "
            f"number of SELECT values must match the number of INSERT "
            f"columns.",
            number=TOO_FEW_TO_INSERT if fewer else TOO_MANY_TO_INSERT,
        )
    places = []
    for one in wanted:
        at = next((index for index, column in enumerate(columns)
                   if column.name.lower() == one), None)
        if at is None:
            raise QueryError(
                f"Invalid column name '{one}'.",
                number=NO_SUCH_COLUMN,
            )
        places.append(at)

    laid = []
    for row in produced.rows:
        made = [None] * len(columns)
        for at, value in zip(places, row):
            made[at] = value
        laid.append(made)
    return laid


def _reads_the_outer_row(inner) -> list:
    """The references a subquery makes to tables it does not read itself.

    A qualifier no table in the subquery answers to belongs to the query
    around it. Finding them is what makes a correlated subquery answerable:
    left alone the reference does not fail either, because a qualified name
    falls back to its bare form, and WHERE person_id = p.id would quietly
    become WHERE person_id = id and count the wrong thing.
    """
    scope = {name.lower() for name in (inner.table, inner.alias) if name}
    for join in inner.joins:
        scope |= {name.lower() for name in (join.table, join.alias) if name}

    found = []
    everywhere = columns_in(inner.where) + columns_in(inner.having) + [
        node for item in (inner.items or ()) for node in columns_in(item.node)
    ]
    for column in everywhere:
        if not column.qualified:
            continue
        qualifier = column.qualified.rsplit(".", 1)[0].lower()
        if qualifier and qualifier not in scope:
            found.append(column)
    return found


def _evaluated_columns(rows: list[list[object]]) -> tuple[list, list]:
    """Columns for values written out, typed by what they are.

    Named by position, because a VALUES list gives no names and what it is
    inserted into supplies them.
    """
    from .source import column_of

    width = len(rows[0]) if rows else 0
    columns = []
    held: list[list[object]] = [[] for _ in rows]
    for at in range(width):
        column, values = column_of(f"column{at + 1}", [row[at] for row in rows])
        columns.append(column)
        for row, value in zip(held, values):
            row.append(value)
    return columns, held


def _one_answer(answer, subquery) -> tuple:
    """What one answered subquery contributes, and the type it declared.

    IN takes the whole first column, a comparison takes one value, and
    EXISTS takes 1 or 0. A subquery standing where one value belongs and
    producing several is an error rather than a silent first row.
    """
    if subquery.kind == "exists":
        return (1 if answer.rows else 0), int
    if len(answer.columns) != 1:
        raise QueryError(
            "Only one expression can be specified in the select list when "
            "the subquery is not introduced with EXISTS.",
            number=ONE_COLUMN_ONLY,
        )
    # What it declared, so a subquery that matched nothing still types the
    # column it stands in rather than leaving it text.
    kind = PYTHON_FOR.get(type(answer.columns[0].type))
    values = [row[0] for row in answer.rows]
    if subquery.kind == "in":
        return values, kind
    if len(values) > 1:
        raise QueryError(
            f"a subquery compared against one value returned {len(values)} rows",
            number=UNSUPPORTED,
        )
    return (values[0] if values else None), kind


def _asking_per_row(select, deferred: dict):
    """The same statement with its correlated subqueries left to be asked.

    Everywhere one can stand: a select-list entry, a condition, a sort key.
    """
    items = select.items
    if items is not None:
        items = tuple(
            replace(item, node=with_deferred(item.node, deferred))
            if item.node is not None else item
            for item in items
        )
    return replace(
        select,
        where=with_deferred(select.where, deferred),
        having=with_deferred(select.having, deferred),
        items=items,
        order_by=tuple(
            replace(key, node=with_deferred(key.node, deferred))
            if key.node is not None else key
            for key in select.order_by
        ),
    )


def _order_plan(keys: tuple, lookup: dict, items) -> list:
    """Where each ORDER BY key gets its value: a column, or an expression.

    Resolution follows SQL Server, measured against 2025 rather than recalled:

      * a bare number is a position in the select list, and one past its end
        is an error rather than a sort by that constant;
      * a bare name is looked for among the select list's aliases first, so
        SELECT c AS a, a AS other ORDER BY a sorts by c, and only then among
        the columns available to sort;
      * an expression is worked out per row and cannot see the aliases, which
        is why ORDER BY s + 'x' is an error where ORDER BY s is not;
      * an item that is the same for every row is refused, because a client
        that computed a constant into an ORDER BY meant something else.

    items is None for a grouped result, whose columns are already the select
    list: an alias is a column name there, and resolving it twice would let a
    grouped column shadow the aggregate beside it.

    Returns one entry per key: an index into the row, or a node to evaluate.
    """
    aliases = {}
    for item in items or ():
        if not item.star and item.alias:
            aliases.setdefault(item.alias.lower(), item)

    plans: list = []
    for at, key in enumerate(keys, start=1):
        if key.position is not None:
            plans.append(_position_plan(key, lookup, items))
            continue

        if key.node is not None:
            # An expression the result already holds as a column is that
            # column: a grouped result answers to COUNT(*) as written.
            written = lookup.get(key.column.lower())
            if written is not None:
                plans.append(written)
                continue
            if is_constant(key.node):
                raise SourceError(
                    f"a constant expression was encountered in the ORDER BY "
                    f"list, position {at}"
                )
            plans.append(key.node)
            continue

        item = aliases.get(key.column.lower())
        if item is not None:
            plans.append(_alias_plan(item, lookup, key))
            continue

        plans.append(_column_plan(key.column, lookup, key))
    return plans


def _position_plan(key, lookup: dict, items) -> object:
    """An ORDER BY that names a position rather than a column."""
    total = len(items) if items is not None else len(set(lookup.values()))
    if key.position < 1 or key.position > total:
        raise SourceError(
            f"the ORDER BY position number {key.position} is out of range of "
            f"the number of items in the select list"
        )
    if items is None:
        return key.position - 1
    return _alias_plan(items[key.position - 1], lookup, key)


def _alias_plan(item, lookup: dict, key) -> object:
    """How to get one select-list entry's value, given its own definition."""
    if item.is_computed:
        return item.node
    position = _found(lookup, item.expression)
    if position is None:
        # An aggregate has no source column to point at; its value is the
        # output column the alias named.
        position = _found(lookup, item.alias)
    if position is None:
        raise SourceError(
            f"invalid column name '{key.column}' in the ORDER BY",
            number=NO_SUCH_COLUMN,
        )
    return position


def _found(lookup: dict, name: str | None) -> int | None:
    """Where a name lands, as written and then by its last part.

    A select list writes dtb.name where the table it reads has a column
    called name, and the qualifier is the alias it gave the table rather
    than part of the column.
    """
    if not name:
        return None
    wanted = name.lower()
    if wanted in lookup:
        return lookup[wanted]
    return lookup.get(wanted.rsplit(".", 1)[-1]) if "." in wanted else None


def _column_plan(name: str, lookup: dict, key) -> int:
    """A plain name, as written first and then by its last part.

    So a flattened team.name is found before anything is read as a table
    qualifier, and u.name still reaches the name column of a join.
    """
    wanted = name.lower()
    position = lookup.get(wanted)
    if position is None and "." in wanted:
        position = lookup.get(wanted.rsplit(".", 1)[-1])
    if position is None:
        raise SourceError(
            f"invalid column name '{key.column}' in the ORDER BY",
            number=NO_SUCH_COLUMN,
        )
    return position


def _sorted(
    rows: list[list[object]], names: list[str], keys: tuple,
    items=None, parameters=None, lookup: dict | None = None,
) -> list[list[object]]:
    """Order rows by the ORDER BY keys.

    Applied before the projection, because a sort may name a column the SELECT
    list does not, and before TOP, because TOP takes the first rows of the
    sorted result rather than sorting whatever it happened to take.

    NULLs sort first ascending and last descending, which is what SQL Server
    does. The sort is stable and runs one key at a time from the last to the
    first, so each key's direction is honoured independently.
    """
    if lookup is None:
        lookup = {name.lower(): index for index, name in enumerate(names)}
    plans = _order_plan(keys, lookup, items)
    ordered = list(rows)

    for key, plan in reversed(list(zip(keys, plans))):
        if isinstance(plan, int):
            def value(row, i=plan):
                return row[i]
        else:
            def value(row, node=plan):
                try:
                    named = {name: row[index] for name, index in lookup.items()}
                    return node.evaluate(named, parameters or {})
                except PredicateError as exc:
                    raise SourceError(f"{exc} in the ORDER BY") from exc

        # The first element of the tuple separates NULLs from values, so the
        # second is only ever compared between two values of the same column.
        # Text sorts under the declared collation, which is case-insensitive:
        # a real server orders ada, alan, barbara, Edsger, Grace, where
        # sorting by code point puts the capitals first.
        try:
            ordered.sort(
                key=lambda row: (value(row) is not None, collated(value(row))),
                reverse=key.descending,
            )
        except TypeError as exc:
            # A column holding two kinds of value, which a union of columns
            # that are not the same type produces. A real server refuses that
            # too, when it converts the one to the other and cannot; the
            # point here is that it refuses rather than failing inside and
            # taking the connection with it.
            raise SourceError(
                f"cannot order by '{key.column}': the column holds more than "
                f"one kind of value, so there is no order to put it in"
            ) from exc
    return ordered


def _values_table(rows: tuple, columns: tuple, name: str,
                  parameters: dict) -> Table:
    """A table written out with VALUES, in a FROM or on a join.

    The values carry no names, so the alias named them, and their types
    come from what they hold, which is how an APPLY over values is typed
    too. Each expression is worked out once: there is no row to be applied
    to here, which is the whole difference from an APPLY.

    Takes the rows and names rather than the statement they came from,
    because a joined one arrives on a Join and the one in the FROM on a
    Select, and neither should have to know about the other.
    """
    built: list = []
    for written in rows:
        try:
            built.append([one.evaluate({}, parameters) for one in written])
        except PredicateError as exc:
            raise SourceError(f"{exc} in {name}") from exc

    # Each column settles on one type and the rest convert to it, which is
    # what a real server's constructor does. The rows were read one at a
    # time before, so (VALUES (1),('x')) came back as a column of text and
    # two rows where a real server answers msg 245 and none.
    for at in range(len(columns)):
        try:
            settled = brought_to_one_type([row[at] for row in built])
        except PredicateError as exc:
            raise _refused(exc) from exc
        for row, value in zip(built, settled):
            row[at] = value

    return _typed(
        Table(
            name=name,
            columns=[Column(one, NVarChar(1)) for one in columns],
            rows=built,
        ),
        0,
    )


def _applied(table: Table, applies: tuple) -> Table:
    """Each row of a table beside the rows an APPLY works out for it.

    CROSS APPLY over values written into the query: the values may name the
    columns of the row they are applied to, so they are worked out again for
    every row rather than once.
    """
    if not applies:
        return table
    for apply in applies:
        columns = list(table.columns) + [
            Column(f"{apply.alias}.{name}", NVarChar(1)) for name in apply.columns
        ]
        names = table.column_names
        built: list = []
        for row in table.rows:
            named = dict(zip(names, row))
            for written in apply.rows:
                try:
                    built.append(list(row) + [
                        one.evaluate(named, {}) for one in written
                    ])
                except PredicateError as exc:
                    raise SourceError(f"{exc} in {apply.alias}") from exc
        table = _typed(Table(name=table.name, columns=columns, rows=built),
                       len(table.columns))
    return table


def _typed(table: Table, from_column: int) -> Table:
    """The same table with the columns after this one typed from their values."""
    from .source import column_of

    columns = list(table.columns[:from_column])
    held = [list(row) for row in table.rows]
    for at in range(from_column, len(table.columns)):
        column, values = column_of(
            table.columns[at].name, [row[at] for row in held]
        )
        columns.append(column)
        for row, value in zip(held, values):
            row[at] = value
    return Table(name=table.name, columns=columns, rows=held)


def _renamed(table: Table, qualifier: str) -> Table:
    """A copy whose columns are all qualified by the table's name or alias."""
    return Table(
        name=qualifier,
        columns=[
            Column(f"{qualifier}.{column.name}", column.type)
            for column in table.columns
        ],
        rows=table.rows,
    )


def _unqualified(table: Table) -> Table:
    """Drop the qualifier from every column name that only one table has.

    A joined table keeps u.name and o.name apart, but a name only one side
    carries reads better as itself, and that is what a client writing
    SELECT status after joining expects to work.
    """
    bare: dict[str, int] = {}
    for column in table.columns:
        _, _, name = column.name.partition(".")
        bare[name.lower()] = bare.get(name.lower(), 0) + 1

    columns = []
    for column in table.columns:
        _, _, name = column.name.partition(".")
        columns.append(
            Column(name, column.type) if bare[name.lower()] == 1 else column
        )
    return Table(name=table.name, columns=columns, rows=table.rows)


def _equalities(condition, left: Table, right: Table):
    """The (left index, right index) pairs an ON condition joins on.

    Only top-level ANDs of column-to-column equality count. Anything else is
    left to be checked row by row, which is correct but slower, so the pairs
    found here are what make the join a hash rather than a loop.
    """
    from .predicate import And, Column as ColumnRef, Comparison

    pairs = []
    pending = [condition]
    while pending:
        node = pending.pop()
        if isinstance(node, And):
            pending.extend((node.left, node.right))
            continue
        if not isinstance(node, Comparison) or node.operator != "=":
            continue
        if not (isinstance(node.left, ColumnRef) and isinstance(node.right, ColumnRef)):
            continue
        for a, b in ((node.left, node.right), (node.right, node.left)):
            at_left = _index_of(left, a)
            at_right = _index_of(right, b)
            if at_left is None or at_right is None:
                continue
            if _buckets_alike(left.columns[at_left], right.columns[at_right]):
                pairs.append((at_left, at_right))
            break
    return pairs


def _buckets_alike(one: Column, other: Column) -> bool:
    """Whether two columns can be bucketed against each other.

    The bucket is a filter: a row whose key is not in it is never compared at
    all, so a key that misses a pair SQL calls equal loses that row with
    nothing said. collated() makes a faithful key for the collation, which is
    case and trailing spaces, and for nothing else. Across kinds SQL converts
    one side to the other, a number beating text and a moment beating both,
    and none of that is in the key.

    Measured: a table of ints joined to a table of the same digits as text
    answered nothing here, and both rows on SQL Server; joined to a text
    column holding something that is not a number at all, a real server
    refuses with message 245 rather than dropping the row. Neither is
    something a bucket can produce, so a join across two kinds does not get
    one and every pair is compared instead, which is slower and right.

    Integers and floats are the exception, and hash together on purpose: 1
    and 1.0 are one key in Python as they are one value in SQL.
    """
    kinds = {type(one.type), type(other.type)}
    if kinds <= {Integer, Float}:
        return True
    return len(kinds) == 1


def _index_of(table: Table, reference) -> int | None:
    """Where a reference lands in a table, or None if it is not this one."""
    for wanted in (reference.qualified, reference.name):
        if not wanted:
            continue
        for at, column in enumerate(table.columns):
            if column.name.lower() == wanted.lower():
                return at
            _, _, bare = column.name.partition(".")
            if bare.lower() == wanted.lower() and reference.qualified is None:
                return at
    return None


def _join(left: Table, right: Table, join, parameters: dict | None = None) -> Table:
    """Match the two tables, saying so where the condition cannot be read.

    A condition naming a column that is not there raised PredicateError from
    inside the row loop and travelled out as itself, reaching the client as
    an internal error. The same mistake in a WHERE or a select list has
    always come back as a refusal with a number on it; found by taking every
    query in the differential and cutting it short, which is what a truncated
    ON condition looks like.
    """
    try:
        return _matched(left, right, join, parameters)
    except PredicateError as exc:
        raise QueryError(str(exc),
                         number=_number_of(exc, NO_SUCH_COLUMN)) from exc


def _matched(left: Table, right: Table, join, parameters: dict | None) -> Table:
    """Match the two tables under the join's condition.

    The parameters go in because an ON condition may name one: a client
    compares a column against a value it bound, or against a subquery that
    was answered before the join ran. Evaluating the condition without them
    made every such comparison NULL and every row fall out.
    """
    parameters = parameters or {}
    from .predicate import collated

    columns = list(left.columns) + list(right.columns)
    names = [column.name for column in columns]
    empty = [None] * len(right.columns)
    nothing = [None] * len(left.columns)
    # Which side keeps the rows that matched nothing. The columns stay in the
    # order they were written whichever side that is, so a RIGHT join is not
    # a LEFT one with the tables swapped: the swap would move the columns.
    keep_unmatched = join.kind in ("LEFT", "FULL")
    keep_others = join.kind in ("RIGHT", "FULL")

    if join.kind == "CROSS" or join.on is None:
        _check_size(len(left.rows) * len(right.rows), join)
        rows = [a + b for a in left.rows for b in right.rows]
        return Table(name=left.name, columns=columns, rows=rows)

    pairs = _equalities(join.on, left, right)
    rows: list[list[object]] = []

    if pairs:
        buckets: dict[tuple, list[int]] = {}
        for at, row in enumerate(right.rows):
            key = tuple(collated(row[one]) for _, one in pairs)
            if None in key:
                continue        # NULL never matches, not even itself
            buckets.setdefault(key, []).append(at)

        paired: set = set()
        for row in left.rows:
            key = tuple(collated(row[at]) for at, _ in pairs)
            found = buckets.get(key, ()) if None not in key else ()
            matched = False
            for at in found:
                combined = row + right.rows[at]
                if matches(join.on, dict(zip(names, combined)), parameters):
                    rows.append(combined)
                    matched = True
                    paired.add(at)
            if keep_unmatched and not matched:
                rows.append(row + empty)
            _check_size(len(rows), join)
        if keep_others:
            rows.extend(nothing + other for at, other in enumerate(right.rows)
                        if at not in paired)
            _check_size(len(rows), join)
        return Table(name=left.name, columns=columns, rows=rows)

    # No equality to hash on, so every pair is tried.
    _check_size(len(left.rows) * len(right.rows), join)
    paired = set()
    for row in left.rows:
        matched = False
        for at, other in enumerate(right.rows):
            combined = row + other
            if matches(join.on, dict(zip(names, combined)), parameters):
                rows.append(combined)
                matched = True
                paired.add(at)
        if keep_unmatched and not matched:
            rows.append(row + empty)
    if keep_others:
        rows.extend(nothing + other for at, other in enumerate(right.rows)
                    if at not in paired)
    return Table(name=left.name, columns=columns, rows=rows)


def _check_size(size: int, join) -> None:
    if size > MAX_JOIN_ROWS:
        what = getattr(join, "table", None) or getattr(join, "alias", "it")
        raise SourceError(
            f"the join of '{what}' would produce more than "
            f"{MAX_JOIN_ROWS} rows; narrow it with a WHERE or a tighter ON"
        )


def _with_windows(table: Table, rows: list, items, parameters) -> Table:
    """The table with one more column for every window the select list names.

    Named as the select list heads it, so the ORDER BY can find it there. A
    star does not reach them: _evaluate is told how many columns the source
    had, and stops at that.
    """
    windows = [item for item in (items or ()) if item.is_window]
    if not windows:
        return Table(name=table.name, columns=table.columns, rows=rows)

    columns = list(table.columns)
    widened = [list(row) for row in rows]
    over = Table(name=table.name, columns=table.columns, rows=rows)
    for at, item in enumerate(windows):
        declared, answers = window.over(
            item.output_name or f"window{at}", over, rows, item.window,
            parameters,
        )
        columns.append(declared)
        for row, answer in zip(widened, answers):
            row.append(answer)
    return Table(name=table.name, columns=columns, rows=widened)


def _evaluate(
    table: Table, items, parameters, produced: dict | None = None,
    alias: str = "", source_columns: int | None = None,
    assigns: list | None = None, into: dict | None = None,
) -> tuple[list[Column], list[list[object]]]:
    """Work out a select list that is more than a projection.

    Stars expand to the table's own columns, plain names are read from the
    row, and anything computed is evaluated against it. Types come from the
    values produced, which is the same rule the sources are typed by: a
    column is whatever every value in it can be.

    `assigns` names the variable each item gives its value to, for a SELECT
    that assigns rather than reads, and a list that does has no star. Each
    value goes into its variable the moment it is worked out, so the items
    after it and the rows after it read what it left: measured, SELECT @s =
    @s + n + ',' FROM a table of a, b and c ends as 'a,b,c,', and SELECT @a
    = 1, @b = @a + 1 leaves @b at 2. This read every row with the value
    from before the statement and kept the last, which gave 'c,'. `into`,
    where given, is written the same way.
    """
    from .source import column_of

    names = table.column_names
    # A star is the source's own columns and not the ones a window added,
    # which sit on the end.
    starred = names if source_columns is None else names[:source_columns]
    # What each column holds, so an expression over an empty table can still
    # be typed: score * 2 is a float whether or not a row survived the WHERE.
    holds = holdings(table.columns)
    windows = iter([
        at for at, name in enumerate(names)
        if source_columns is not None and at >= source_columns
    ])
    headings: list[str] = []
    plans: list[object] = []
    for item in items:
        if item.star:
            wanted = _starred(starred, item.expression, table.name, alias)
            # t.* names the columns of t, and a real server heads them with
            # the names they have there rather than with the qualifier.
            headings.extend(
                names[at].split(".", 1)[-1] if item.expression else names[at]
                for at in wanted
            )
            plans.extend(wanted)
            continue
        if item.is_window:
            # Already worked out, before the sort, and sitting on the end of
            # the row as a column of its own; see _with_windows.
            headings.append(item.output_name)
            plans.append(next(windows))
            continue
        if item.is_computed:
            headings.append(item.output_name)
            plans.append(item.node)
            continue
        at = table.index_of(item.expression or "")
        if at is None:
            raise SourceError(
                f"invalid column name '{item.expression}' in table '{table.name}'"
            )
        headings.append(item.output_name)
        plans.append(at)

    built: list[list[object]] = []
    if assigns is not None:
        # Its own loop, so that a read that assigns nothing pays nothing.
        for row in table.rows:
            named = dict(zip(names, row))
            values = []
            for plan, variable in zip(plans, assigns):
                value = (row[plan] if isinstance(plan, int)
                         else plan.evaluate(named, parameters))
                values.append(value)
                parameters[variable] = value
                if into is not None:
                    into[variable] = value
            built.append(values)
    else:
        for row in table.rows:
            named = None
            values = []
            for plan in plans:
                if isinstance(plan, int):
                    values.append(row[plan])
                    continue
                if named is None:
                    named = dict(zip(names, row))
                values.append(plan.evaluate(named, parameters))
            built.append(values)

    columns = []
    for at, heading in enumerate(headings):
        if isinstance(plans[at], int):
            columns.append(Column(heading, table.columns[plans[at]].type))
        else:
            column, values = column_of(
                heading, [row[at] for row in built],
                kind=_kind_of(plans[at], produced or {}, holds),
            )
            columns.append(column)
            for row, value in zip(built, values):
                row[at] = value
    return columns, built


def _group_names(items, columns) -> list[list[str]]:
    """Every name one column of a grouped result answers to.

    Three per entry: what the query wrote for an aggregate, the alias if it
    gave one, and the column name for a grouped column. A client may write
    COUNT(*) or the alias it gave that count and mean the same thing, in a
    HAVING or in an ORDER BY.
    """
    names: list[list[str]] = []
    for item, column in zip(items, columns):
        answers = [column.name]
        if item.is_aggregate:
            answers.append(f"{item.function}({item.expression or '*'})")
        elif item.expression:
            answers.append(item.expression)
        names.append([answer for answer in answers if answer])
    return names


def _aggregate_key(item) -> str:
    """What an aggregate entry is named by, which is what it says.

    The same spelling an Aggregate node inside an expression builds, so the
    two find each other: COUNT(DISTINCT team) is not COUNT(team).
    """
    written = item.expression or "*"
    return f"{item.function}(DISTINCT {written})" if item.distinct else \
        f"{item.function}({written})"


def _number_of(exc: Exception, otherwise: int = INVALID_OBJECT_NAME) -> int:
    """The number an error already knows, or the one for a name gone wrong.

    A client shows it. SSMS prints "Msg 8134" beside the words, and a divide
    by zero reported as msg 208, invalid object name, sends whoever reads it
    looking for a table that was never the problem. Most of what reaches
    here really is a name gone wrong, which is why that is the fallback.
    """
    return getattr(exc, "number", None) or otherwise


def _refused(exc: Exception) -> QueryError:
    """A statement that could not be read, as the error a client is sent.

    With the number, level and state a real server gives the same complaint
    where it is one a real server has, and this project's own 50000 at level
    16 where it is not. The level used to be dropped on the way, so msg 107,
    which a real server sends at 15, went out at 16. A PredicateError comes
    this way too, and carries a number but no level or state.
    """
    return QueryError(str(exc), number=_number_of(exc, UNSUPPORTED),
                      severity=getattr(exc, "severity", 16),
                      state=getattr(exc, "state", 1))


def _unlisted_aggregates(select, items: list) -> list:
    """The aggregates a HAVING or an ORDER BY names and the select list does not.

    SQL Server computes them for the group anyway: ORDER BY MAX(a) sorts by a
    value nobody asked to see, and HAVING MAX(a) > 3 keeps groups by one.
    They are appended to the select list, used, and dropped before the result
    goes out.
    """
    known = {
        one_spelling(_aggregate_key(item))
        for item in items if item.is_aggregate
    }
    named = aggregates_in(select.having)
    for key in select.order_by:
        named += aggregates_in(key.node)
    for item in select.items or ():
        # An entry that computes over aggregates rather than being one:
        # SUM(a) / COUNT(*) names two that nothing else asked to see.
        named += aggregates_in(item.node)

    extra = []
    for node in named:
        if one_spelling(node.key) in known:
            continue
        known.add(one_spelling(node.key))
        extra.append(_asked_for(node))
    return extra


def _asked_for(node) -> SelectItem:
    """The select-list entry that computes one aggregate an expression named.

    A plain column is named and read by position. Anything else has to be
    worked out per row before it can be reduced, which is the same shape the
    select list already uses for MAX(score * 2) written on its own.
    """
    if node.argument == "*":
        return SelectItem(function=node.function, distinct=node.distinct)
    try:
        inner = parse_expression(node.argument)
    except PredicateError as exc:
        raise QueryError(
            f"cannot read {node.argument!r} inside {node.function}(): {exc}",
            number=UNSUPPORTED,
        ) from exc
    if isinstance(inner, PredicateColumn):
        return SelectItem(function=node.function, expression=node.argument,
                          distinct=node.distinct)
    return SelectItem(function=node.function, expression=node.argument,
                      argument=inner, distinct=node.distinct)


# What an expression produces, once a subquery has been answered, as the
# Python type the column builder speaks.
def _kind_of(node, produced: dict, columns: dict | None = None) -> type | None:
    """What an expression produces, or None where it cannot be said.

    A subquery is a parameter by the time this runs, and result_kind cannot
    say what a parameter holds. This one can: it was answered a moment ago
    and its column said what it was.

    NoneType says every part of the expression was a written NULL, which has
    no type of its own until something gives it one. It is answered as
    nothing known rather than as a type, because that is what a union needs
    from it: measured, SELECT NULL UNION ALL SELECT name is an nvarchar
    column there, so the NULL branch has to let the other one decide.
    """
    kind = result_kind(node, columns)
    if kind not in (None, type(None)):
        return kind
    name = getattr(node, "name", None)
    return produced.get(name) if isinstance(name, str) else None


def _starred(names: list, qualifier: str | None, table: str = "",
             alias: str = "") -> list:
    """Which columns a star stands for: all of them, or one table's.

    t.* is every column t brought and nothing else, which is how a query
    reads one side of a join or the table an APPLY worked out. A join
    qualifies its columns and nothing else does, so the table this reads
    from answers to its own name with the columns that carry no qualifier.
    """
    if not qualifier:
        return list(range(len(names)))
    prefix = f"{qualifier.lower()}."
    found = [at for at, name in enumerate(names) if name.lower().startswith(prefix)]
    if found:
        return found
    # The name a query calls the table by, which is the alias where it gave
    # one and the table's own name where it did not. p.* is every column of
    # p either way.
    if qualifier.lower() in {(table or "").lower(), (alias or "").lower()}:
        return [at for at, name in enumerate(names) if "." not in name]
    raise SourceError(f"'{qualifier}.*' names nothing this query reads")


def _having(select, items, columns, rows, parameters):
    """Keep the groups the HAVING accepts.

    The condition is evaluated against the group's own row, under every name
    that column answers to, so HAVING COUNT(*) > 2 and HAVING n > 2 are the
    same condition when the query wrote COUNT(*) AS n.
    """
    keys = _group_names(items, columns)

    kept = []
    for row in rows:
        seen: dict[str, object] = {}
        for names, value in zip(keys, row):
            for name in names:
                seen.setdefault(name, value)
        try:
            if matches(select.having, seen, parameters):
                kept.append(row)
        except PredicateError as exc:
            raise QueryError(str(exc), number=_number_of(exc)) from exc
    return kept


# What sp_executesql is handed after the statement and its declarations: a
# name and the value to give it.
_AN_ARGUMENT = re.compile(
    r"\s*(@[A-Za-z0-9_@#$]+)\s*=\s*(N?'(?:[^']|'')*'|[^,]+)", re.IGNORECASE
)


def _written_out(statement: str):
    """A statement a batch wrote out as text, with the values it named.

    EXEC('...') and EXEC sp_executesql N'...' are the two ways of saying it
    and mean the same thing. Returns None for anything else, and a pair of
    the text and its values otherwise; the values are empty for EXEC(), which
    takes none.
    """
    run = _EXEC_LITERAL.match(statement)
    if run:
        return run.group(1).replace("''", "'"), {}
    named = _EXEC_SP.match(statement)
    if not named:
        return None
    opened = statement.index("'", named.end() - 1)
    closed = _skip_quoted(statement, opened, "'")
    written = statement[opened + 1:closed - 1].replace("''", "'")
    # What follows is the declarations and then the arguments. Only the
    # named ones are values; the declarations are a string and say nothing
    # this needs, because a value carries its own type here.
    return written, _arguments_named(statement[closed:])


def _arguments_named(rest: str) -> dict:
    """The @name = value pairs among what follows a statement."""
    found: dict = {}
    for match in _AN_ARGUMENT.finditer(rest):
        written = match.group(2).strip()
        try:
            found[match.group(1)] = parse_expression(written).evaluate({}, {})
        except PredicateError:
            found[match.group(1)] = written
    return found


def _refuse_a_write(written: str) -> None:
    """Say so, where a statement would change something this cannot change.

    Everything else that is neither a read nor a write is passed over on
    purpose: a client sends SET and USE by the dozen before it will talk to a
    server, and answering those with an error stops it before it starts. A
    write is different. Passing over a DELETE reports that it worked, and a
    person who believes that has been told something untrue about their data.
    A GRANT is the same untruth about who can read it.
    """
    permission = _PERMISSION.match(written)
    if permission:
        named = permission.group(2)
        raise QueryError(
            f"{permission.group(1).upper()} is not supported: this server has "
            f"no permissions to change, and "
            + (f"'{named}' is read-only for everyone who can reach it"
               if named else "every source it serves is read-only"),
            number=UNSUPPORTED,
        )

    write = _WRITES.match(written)
    if not write or write.group(2).lstrip("[").startswith("#"):
        # No target, or the session's own scratch table, which is written by
        # _session_statement and has already had its chance at this.
        return
    raise QueryError(
        f"{write.group(1).upper()} is not supported: this server reads its "
        f"sources and never writes to them, so '{write.group(2)}' is "
        f"unchanged",
        number=UNSUPPORTED,
    )


def _one_type_per_column(columns: list, answers: list) -> tuple[list, list]:
    """Every branch's values brought to the one type each column settled on.

    Returns the headings the client is told and each branch's rows to match.
    The conversion happens here rather than after the branches are combined,
    which is where it has to be: UNION drops repeated rows, and 1 and '1' are
    one row only once they are the same value. Measured that way round.

    Branches that already agree on a column are left untouched, which is the
    usual case and the one that has to stay exactly as it was.
    """
    kinds = [
        precedence.resolve([answer.columns[at].type for answer in answers])
        for at in range(len(columns))
    ]
    branches = [[list(row) for row in answer.rows] for answer in answers]
    if all(kind is None for kind in kinds):
        return columns, branches

    for at, wanted in enumerate(kinds):
        if wanted is None:
            continue
        for rows, answer in zip(branches, answers):
            came_from = answer.columns[at].type
            if came_from == wanted:
                continue
            for row in rows:
                row[at] = precedence.convert(row[at], wanted, came_from)

    headed = []
    for at, (column, wanted) in enumerate(zip(columns, kinds)):
        if wanted is None:
            headed.append(column)
            continue
        held = [row[at] for rows in branches for row in rows]
        headed.append(Column(column.name, precedence.sized(wanted, held)))
    return headed, branches


def _parts(select) -> list:
    """The SELECTs a combined statement is made of, each with its operator.

    The first carries None, because nothing precedes it. A chain of three is
    parsed as a part holding a part, so this walks it flat.
    """
    parts = [(None, select)]
    while parts[-1][1].combine:
        kind, following = parts[-1][1].combine[0]
        parts.append((kind, following))
    return parts


def _signature(row: list) -> tuple:
    """What makes two rows the same row, under the declared collation."""
    return tuple(collated(value) for value in row)


def _distinct(rows: list[list[object]]) -> list[list[object]]:
    """Drop repeated rows, keeping the order they first appeared in.

    Compared under the declared collation, which is case-insensitive, so two
    rows differing only in case are one row.
    """
    from .predicate import collated

    seen: set[tuple] = set()
    kept = []
    for row in rows:
        signature = tuple(collated(value) for value in row)
        if signature in seen:
            continue
        seen.add(signature)
        kept.append(row)
    return kept


def _with_ties(select, rows: list, names: list, parameters, items=None,
               lookup: dict | None = None) -> int | None:
    """How many rows TOP ... WITH TIES reaches to, or None where it is not one.

    Worked out over the sorted rows and before anything is projected, because
    the sort may name a column the select list does not and the ties are on
    what the sort said. Every row equal to the last one TOP would have taken,
    on every key it was sorted by, comes with it.

    Read the same way the sort read them, so a key that is an expression ties
    on what it works out to: ORDER BY id - id is the same for every row, and
    TOP 1 WITH TIES over it is every row.
    """
    if not select.top_ties:
        return None
    limit = select.row_limit(parameters)
    if limit is None or limit <= 0 or limit >= len(rows):
        return limit
    if lookup is None:
        lookup = {name.lower(): at for at, name in enumerate(names)}
    plans = _order_plan(select.order_by, lookup, items)

    def said(row):
        named = None
        values = []
        for plan in plans:
            if isinstance(plan, int):
                values.append(collated(row[plan]))
                continue
            if named is None:
                named = dict(zip(names, row))
            values.append(collated(plan.evaluate(named, parameters or {})))
        return tuple(values)

    last = said(rows[limit - 1])
    reach = limit
    while reach < len(rows) and said(rows[reach]) == last:
        reach += 1
    return reach


def _page(select, rows: list[list[object]], parameters,
          ties: int | None = None) -> list[list[object]]:
    """Apply TOP and OFFSET/FETCH, after the sort rather than before.

    TOP 3 ... ORDER BY score DESC means the three highest scores, not three
    arbitrary rows put in order. And after DISTINCT, so a share of the rows
    is a share of the ones a client will see.

    ties is how many rows TOP ... WITH TIES reaches to, worked out by
    _with_ties where the sort keys were still to hand.
    """
    try:
        limit = select.row_limit(parameters)
    except SqlError as exc:
        raise _refused(exc) from exc
    if limit is not None and select.top_share:
        # A share of the rows rather than a count of them, rounded up: one
        # percent of six rows is one row and not none. Measured.
        limit = -(-limit * len(rows) // 100)
    if ties is not None:
        limit = ties
    if limit is not None:
        rows = rows[:limit]
    if select.offset:
        rows = rows[select.offset:]
    if select.fetch is not None:
        rows = rows[:select.fetch]
    return rows


def _sorted_by_name(tables: list[Table]) -> list[Table]:
    return sorted(tables, key=lambda t: t.name.lower())


def _text_of(value: object) -> str:
    """A value as the name it stands for, which is what a registry read asks."""
    return "" if value is None else str(value)


def _arguments(written: str, bound: dict) -> list[object]:
    """The arguments of an EXEC written as text.

    A client that sends EXEC sp_columns 'people' rather than an RPC still has
    to be understood, and its arguments arrive as a comma-separated list of
    quoted strings, numbers and NULLs.

    A marker with nothing bound to it is no filter rather than a filter on the
    literal text: EXEC sp_columns @Table with no @Table supplied is asking for
    every table, and reading it as a table named "@Table" answers with none.
    """
    values: list[object] = []
    # Split with the quotes respected. Splitting on every comma cut a name
    # that held one in half, and the client was answered with nothing at
    # all rather than an error: EXEC sp_columns 'one, two' asked for a
    # table called "one" and found none. The parts arrive stripped.
    for piece in _split_outside_quotes(written):
        if not piece:
            continue
        if "=" in piece and piece.lstrip().startswith("@"):
            piece = piece.split("=", 1)[1].strip()
        if piece.startswith("@"):
            values.append(_bound(piece, bound))
        elif piece.upper() == "NULL":
            values.append(None)
        elif piece[:1] in "'\"" or piece[:2].upper() == "N'":
            # A doubled quote is one quote, which this used to leave
            # doubled, so a table named it's was never found either.
            quoted = piece.lstrip("Nn")
            quote = quoted[:1]
            values.append(quoted.strip("'\"").replace(quote * 2, quote))
        else:
            try:
                values.append(int(piece))
            except ValueError:
                values.append(piece)
    return values


def _bound(marker: str, bound: dict) -> object:
    """What a client supplied for a parameter marker, if anything."""
    wanted = marker.lstrip("@").lower()
    for name, value in bound.items():
        if name.lstrip("@").lower() == wanted:
            return value
    return None


def _safe_load(source) -> Table:
    """Ask one source for its shape, turning a failure into an empty table."""
    try:
        return source.schema()
    except SourceError:
        return Table(name=source.name, columns=[], rows=[])
