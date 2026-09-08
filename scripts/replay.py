"""Replay every query a real client sent, and report what still refuses.

The differential comparison says whether an answer is right. This says
whether there is an answer at all, over the queries a client actually sends
rather than the ones anyone thought to write down. SSMS sends about seventy
distinct ones before it will draw a tree, Power Query sends its own, and the
ones that refuse are the work list.

    python -m pysqlbridge --config examples/tables.json --log ssms.log
    ... connect a client, expand the tree, run something ...
    python scripts/replay.py ssms.log --config examples/tables.json

Nothing is sent anywhere: each query is answered against a catalog built
here, which is enough to say whether it would be refused and why. Grouped by
the reason, because one missing view is usually several refusals and the
count says which gap is worth closing first.

A log truncated mid-query shows up as a refusal too, most visibly as a
bracket that was opened and not closed. Those are the log's fault rather
than the server's, which is why the reason is printed rather than a count.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from pysqlbridge.catalog import load
from pysqlbridge.tds.result import Query

# What the server writes before a query it is about to answer. The query
# follows on one line, with any newline in it written as backslash and n.
MARK = "query in full: "


def unescaped(line: str) -> str:
    """One logged line back into the query it was written from.

    The escapes are read left to right rather than replaced one kind at a
    time, so a backslash that was in the query stays one rather than turning
    the character after it into a line break.
    """
    out: list[str] = []
    at = 0
    while at < len(line):
        char = line[at]
        if char != "\\" or at + 1 >= len(line):
            out.append(char)
            at += 1
            continue
        following = line[at + 1]
        out.append({"n": "\n", "r": "\r", "\\": "\\"}.get(following, char + following))
        at += 2
    return "".join(out)


def queries_in(log: pathlib.Path) -> list[str]:
    """Every query the log recorded in full, in the order it recorded them.

    Read with newline="" so that the only thing that ends a record is the
    newline the log wrote. A log written before the server escaped carriage
    returns holds a bare one at the end of every line of every query a
    client sent with CRLF; read as line breaks those cut each query off at
    its first one, and seventeen of eighteen refusals reported here were
    that rather than anything the server could not answer.
    """
    with log.open(encoding="utf-8", errors="replace", newline="") as handle:
        text = handle.read()
    found = []
    at = 0
    while True:
        at = text.find(MARK, at)
        if at < 0:
            return found
        at += len(MARK)
        end = text.find("\n", at)
        end = len(text) if end < 0 else end
        line = text[at:end]
        if line.endswith("\r"):
            line = line[:-1]            # the other half of a CRLF terminator
        found.append(unescaped(line).rstrip().rstrip(";"))


def main() -> int:
    parse = argparse.ArgumentParser(description=__doc__)
    parse.add_argument("log", type=pathlib.Path,
                       help="a log written by --log while a client was connected")
    parse.add_argument("--config", type=pathlib.Path, required=True,
                       help="the sources to answer against")
    parse.add_argument("--show", action="store_true",
                       help="print the queries themselves, not just the reasons")
    args = parse.parse_args()

    sent = queries_in(args.log)
    if not sent:
        print(f"no queries in {args.log}; was the server run with --log?")
        return 1
    distinct = list(dict.fromkeys(sent))

    catalog = load(args.config)
    answered = 0
    refused: dict[str, list[str]] = {}
    for sql in distinct:
        try:
            catalog.answer(Query(sql=sql, session={}))
            answered += 1
        except Exception as exc:  # noqa: BLE001 - every refusal counts, whatever it is
            refused.setdefault(str(exc), []).append(sql)

    print(f"{len(sent)} sent, {len(distinct)} distinct")
    print(f"{answered} answered, {len(distinct) - answered} refused")
    print()
    for reason, whose in sorted(refused.items(), key=lambda pair: -len(pair[1])):
        print(f"{len(whose):>3}  {reason}")
        if args.show:
            for sql in whose:
                print(f"       {sql[:300]}")
    return 1 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
