"""Building an Access database to read back.

Built rather than committed, for the same reason a workbook is: an .accdb in
the tree is 300KB of opaque binary that nobody can review, and what matters
about the fixture is the SQL that made it, which is right here.

The same library that reads one writes one, in pure Python, so this needs no
Access, no database engine and no Windows. These tests run wherever the rest
of the suite does.
"""

# Every type Access offers that this has an answer for, and a couple it does
# not, so both paths through the type mapping are exercised.
SCHEMA = """
    CREATE TABLE Members (
        id COUNTER PRIMARY KEY,
        name TEXT(50),
        score DOUBLE,
        joined DATETIME,
        active YESNO,
        fee CURRENCY,
        notes MEMO,
        tally LONG,
        small SHORT
    )
"""

ROWS = [
    "INSERT INTO Members (name, score, joined, active, fee, notes, tally, "
    "small) VALUES ('ada', 99.5, #2024-01-15#, True, 12.34, 'first', "
    "1000000, 7)",
    "INSERT INTO Members (name, score, joined, active, fee, notes, tally, "
    "small) VALUES ('grace', 87.25, #2023-06-01 13:30:00#, False, 0.5, NULL, "
    "2, 8)",
    "INSERT INTO Members (name, joined, active, fee, notes, tally, small) "
    "VALUES ('edsger', #1965-03-02#, True, 0, 'x', 3, 9)",
]

MORE = [
    "CREATE TABLE Rooms (code TEXT(10), seats LONG)",
    "INSERT INTO Rooms (code, seats) VALUES ('A1', 30)",
    "INSERT INTO Rooms (code, seats) VALUES ('B2', 12)",
    "CREATE TABLE Nothing (a TEXT(5))",
]

# Saved queries, which Access calls views. Its own DDL has no CREATE VIEW, so
# they go in the way DAO's CreateQueryDef puts them there.
QUERIES = {
    "BigRooms": "SELECT code, seats FROM Rooms WHERE seats > 20",
    "NoRooms": "SELECT code, seats FROM Rooms WHERE seats > 900",
}


def build(path, statements=None, queries=None):
    """Write a database at path, and answer where it was written."""
    from pyopenvba.access import AccessDatabase

    database = AccessDatabase.create_new(path)
    for statement in (statements if statements is not None
                      else [SCHEMA, *ROWS, *MORE]):
        database.execute(statement)
    for name, sql in (QUERIES if queries is None else queries).items():
        database.create_query(name, sql)
    database.save()
    return path
