# Fuzz corpus

Seed inputs for the fuzz targets in [`tests/fuzz_targets.py`](../fuzz_targets.py),
one directory per target. Each file is raw bytes; the extension only says what
it holds. [`tests/test_fuzz_corpus.py`](../test_fuzz_corpus.py) replays every
seed through its target on every CI run, and the Fuzz workflow starts
Atheris from them.

A target either succeeds or raises the one error its parser documents for bad
input. Anything else escaping is a finding.

| Directory | What it reads | May raise |
|---|---|---|
| `packet/` | `tds.reassemble()`: packet headers and message reassembly | `TdsProtocolError` |
| `prelogin/` | `Prelogin.parse()` | `TdsProtocolError` |
| `login7/` | `Login7.parse()` and `deobfuscate_password()` | `TdsProtocolError` |
| `batch/` | `parse_sql_batch()`, as TDS 7.4 and 7.1 | `TdsProtocolError` |
| `rpc/` | `parse_rpc()`, as TDS 7.4 and 7.1 | `TdsProtocolError` |
| `sql/` | `statements()`, `malformed()`, `loose_exits()` and `parse_select()` | `SqlError` |
| `json/` | `json.loads`, then `detect()`, `extract()`, `locate()` and `from_records()` | `SourceError` |
| `xml/` | `parse_xml()`, then the same as `json/` | `SourceError` |
| `html/` | `parse_html()`, then the same as `json/` | `SourceError` |
| `csv/` | `csv_records()`, then `from_records()` | `SourceError` |

The seeds in `packet/`, `prelogin/`, `login7/`, `batch/` and `rpc/` come from
the captures in [`tests/captured.py`](../captured.py). A file named
`regression_*` is a finding, minimised, kept so it stays fixed.

## Running a target

Atheris ships Linux wheels for Python 3.12 to 3.14:

```
python -m pip install --require-hashes -r .github/requirements/fuzz.txt
python -m pip install --no-deps --no-build-isolation -e .
python fuzz/fuzz_parsers.py rpc -max_total_time=60 tests/fuzz_corpus/rpc
```

The fuzzer adds what it learns to the first corpus directory, so point it at
a copy if the seeds here should stay as they are. A finding is written to a
`crash-*` file: minimise it, add it here as `regression_<what>`, fix the
parser, and add a test beside the parser's own tests.
