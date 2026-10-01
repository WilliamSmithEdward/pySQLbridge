# Examples

Each file is a configuration for `pysqlbridge --config <file>`. Paths inside one
resolve against the file, so `data/` here is `examples/data/`.

| File | What it serves |
| --- | --- |
| `tables.json` | `people` from a CSV, `cities` from a JSON file, and `pokemon` from a live HTTP API |
| `discover.json` | whatever discovery finds under three APIs' base URLs, prefixed `poke`, `rm` and `shop`, beside four named tables read as JSON, RSS, XML and HTML |
| `apis.json` | nine public APIs, one for each shape an API answers in |

The tables in `apis.json`, and what each one shows:

| Table | Shape |
| --- | --- |
| `pokemon` | follows the `next` link, bounded to four pages |
| `users` | a top-level array whose records nest three deep; flattening gives `address.geo.lat` |
| `earthquakes` | GeoJSON: the rows are under `features`, and each one nests into `properties` and `geometry` |
| `exchange_rates` | a map of scalars is rows turned sideways: `entries` gives one row per currency |
| `forecast` | parallel arrays, one per column, zipped into rows by `columns` |
| `cpython` | one object is one row (`single`); its 86 keys flatten to more columns than anyone wants, so `columns` keeps six |
| `norway_population` | the response is `[metadata, rows]`, and the numeric path segment `1` indexes the list |
| `characters` | the next link lives under `info`; each character's `episode` array becomes the table `characters_episode` |
| `joke_categories` | a bare list of strings has no key to name a column with, so `scalars` serves it as one column |

A configuration key the bridge does not know is refused rather than ignored,
which is why these notes live here and not in the JSON.
