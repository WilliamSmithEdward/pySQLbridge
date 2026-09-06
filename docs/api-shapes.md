# Serving arbitrary APIs as tables

What real APIs return, and what a bridge has to do about it. Surveyed
2026-09-06 against nine public endpoints, because designing this from
imagination produces a config format that fits the APIs you thought of.

## What is actually out there

| API | Shape | Handled today |
| --- | --- | --- |
| PokeAPI | envelope, rows under `results`, records flat | yes |
| JSONPlaceholder `/users` | top-level array, records nest 3 deep (`address`, `company`) | no |
| USGS earthquakes | envelope under `features`, each record has `properties` and `geometry` | no |
| Open Library `/search` | envelope under `docs`, records hold arrays (`author_name`, `language`) | no |
| Frankfurter `/latest` | `rates` is a map of scalars: `{"USD": 1.08, "GBP": 0.85}` | no |
| Open-Meteo forecast | `hourly` holds parallel arrays: `time[]`, `temperature_2m[]` | no |
| GitHub `/repos/{o}/{r}` | a single object, 86 keys, several nested | no |
| World Bank | array of two elements: metadata object, then the rows array | no |
| REST Countries (rejecting the request) | `{"errors": [{"message": ...}]}` | serves the errors as a table |

One of nine fits. The current design is the special case.

That last row is the one to be careful about. The request was rejected and the
API answered HTTP 200 with an error envelope, and a shape-sniffer looking for
"the array in this document" found `errors` and would have served the failure
as data.

## Two stages, not a pile of options

Everything above is the same two problems: find the records in a document, then
turn one record into a flat row. Adding a config flag per API quirk gives a
format nobody can hold in their head. Two stages with a few strategies each
covers all nine.

### Stage one: locate the records

| Strategy | Document | Produces |
| --- | --- | --- |
| `array` | `path` points at a list of objects | those objects |
| `single` | `path` points at one object | one row |
| `values` | `path` points at a map of objects | the values |
| `entries` | `path` points at a map of scalars | a row per pair, `key` and `value` |
| `columns` | `path` points at a map of equal-length arrays | the arrays zipped into rows |

`path` also needs to index a list, so World Bank's `[metadata, rows]` is
reachable as `1`. A numeric segment means an index rather than a key.

`array` stays the default because it is the common case, and naming a strategy
is better than sniffing: sniffing is exactly what would have served REST
Countries' error envelope as a table.

### Stage two: flatten the record

A record that nests is refused today, which rules out most of the survey.
Flattening joins the keys:

```
{"name": {"common": "Norway"}, "address": {"geo": {"lat": "-37"}}}
  ->  name.common, address.geo.lat
```

Arrays inside a record are the harder half, and the honest answer depends on
what they hold. `author_name: ["E. Dijkstra"]` wants to be text.
`features: [{...}, {...}]` is a nested table, not a column. The rule worth
having:

- an array of scalars becomes its JSON text, so nothing is lost
- an array of objects is not flattened, and its path is offered as a separate
  table instead

Both need a depth limit and a column cap. GitHub's repo object flattens to
around a hundred columns, which is a table nobody wants and a result set nobody
reads. A `columns` projection in the config is the answer, and the cap makes
forgetting it an error rather than a surprise.

## How a table advertises its fields

INFORMATION_SCHEMA has to answer before anyone selects anything, and the
columns of an API table are not known until something is fetched. Three ways
out, in the order they should be tried:

1. **Declared in the config.** Exact, free, and it drifts from reality the
   first time the API changes.
2. **Probed once at startup**, with whatever `limit` the endpoint takes, and
   cached. One cheap request buys a real answer.
3. **Fetched on first use**, with the catalog reporting the table with no
   columns until then. What happens now.

Probing is the right default, with declaration as an override for an endpoint
that cannot be sampled cheaply, and lazy as the fallback when a probe fails.
A source that cannot be reached still has to appear in the table list, because
one dead API should not hide every table that works.

There is a sharp edge underneath this. COLMETADATA declares a column's type
once per result set, and inference from a sample can be wrong for rows that
arrive later: an integer column in the first 25 rows, text in row 400. The
options are to re-infer per fetch, which changes a column's type under a client
that has already read it, or to widen to text on conflict, which is stable and
lossy. Stability wins; a client that has been told a column is `int` should not
receive `nvarchar` on its next query.

## Joining two APIs

Once each API is a table, a join belongs to the query layer, not the source
layer. `FROM a JOIN b ON a.x = b.y` over two in-memory tables is a hash join
and is not the hard part.

The hard part is what it costs. Both sides load in full, because there is no
predicate pushdown: joining a 50,000-row API to a 10-row lookup fetches 50,000
rows to discard most of them. Three things make that tolerable, in increasing
order of work:

- the TTL cache already there, so a repeated join does not refetch
- a row cap per source, so a runaway endpoint fails loudly rather than filling
  memory
- **parameterised sources**, where a URL carries a placeholder and is fetched
  per key: `https://pokeapi.co/api/v2/pokemon/{name}`. A join against one of
  those becomes N small requests rather than one enormous one, which is how
  the endpoint was designed to be used.

That third one is the interesting one, and it is a genuine change in shape: a
source stops being a table and becomes a function from key to rows. It needs a
hard bound on N and a clear error when a query would exceed it, because the
failure mode is hammering somebody else's API from a spreadsheet.

## Order of work

1. Flattening with a depth limit and a column cap. Unblocks five of the nine
   surveyed APIs and is the only item with no design questions left.
2. The record-locating strategies. Covers the remaining four.
3. Pagination, bounded by a page limit. PokeAPI, GitHub and Open Library all
   page, and a table that silently returns only the first page is worse than
   one that refuses.
4. Column probing at startup, so table pickers see real columns.
5. Joins, in-memory, with a row cap.
6. Parameterised sources, if joins prove the need.
