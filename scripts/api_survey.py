"""Fetch a wide set of public APIs and grade the whole pipeline over them.

Detection, shaping and SQL are only worth what they do on responses nobody
chose for them, so the list below is deliberately hostile: envelopes, bare
arrays, maps keyed by a code, parallel arrays, arrays of bare integers, a
response whose first element is metadata and second is the rows, OData
payloads whose keys contain dots, JSON:API, GeoJSON, feeds, and markup.

Every response is graded in five stages, and the last one is what counts:

  1. decoded          the bytes parsed as JSON, XML or HTML
  2. read             detection chose a reading of where the rows are
  3. built            that reading produced a table with columns and rows
  4. queried          SQL over that table answered
  5. checked          the answers agreed with the data they came from

Stage five is the part that catches real defects: a COUNT that disagrees with
the table, an ORDER BY that disagrees with a sort, a flattened row that lost a
leaf, a child table that lost elements of the array it came from.

Responses are cached so this can be run repeatedly without asking anyone's
server again. Network-bound, so it is not part of the test suite:

    python scripts/api_survey.py                # fetch what is missing, grade
    python scripts/api_survey.py --offline      # grade what is already cached
    python scripts/api_survey.py --cache DIR    # somewhere other than TEMP
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import hashlib
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from pysqlbridge.catalog import Catalog                        # noqa: E402
from pysqlbridge.detect import describe, detect, is_rejection  # noqa: E402
from pysqlbridge.http_source import extract, fetch, locate     # noqa: E402
from pysqlbridge.markup import (                               # noqa: E402
    parse_html,
    parse_xml,
    sniff,
    without_bom,
)
from pysqlbridge.predicate import collated                     # noqa: E402
from pysqlbridge.source import (                               # noqa: E402
    MAX_COLUMNS,
    SourceError,
    child_tables,
    csv_records,
    flatten_record,
    from_records,
    identifying_column,
)

TIMEOUT = 25.0

ENDPOINTS = [
    # --- envelopes: the rows sit under a key -----------------------------
    "https://pokeapi.co/api/v2/pokemon?limit=20",
    "https://pokeapi.co/api/v2/berry",
    "https://pokeapi.co/api/v2/type",
    "https://pokeapi.co/api/v2/ability?limit=5",
    "https://pokeapi.co/api/v2/move?limit=5",
    "https://pokeapi.co/api/v2/item?limit=5",
    "https://pokeapi.co/api/v2/machine?limit=5",
    "https://pokeapi.co/api/v2/location?limit=5",
    "https://rickandmortyapi.com/api/character",
    "https://rickandmortyapi.com/api/location",
    "https://rickandmortyapi.com/api/episode",
    "https://api.artic.edu/api/v1/artworks?limit=5",
    "https://api.artic.edu/api/v1/artists?limit=5",
    "https://api.artic.edu/api/v1/exhibitions?limit=5",
    "https://openlibrary.org/search.json?q=dijkstra&limit=5",
    "https://openlibrary.org/search/authors.json?q=knuth",
    "https://openlibrary.org/subjects/love.json?limit=5",
    "https://opentdb.com/api.php?amount=5&type=multiple",
    "https://randomuser.me/api/?results=5",
    "https://dummyjson.com/products?limit=5",
    "https://dummyjson.com/users?limit=5",
    "https://dummyjson.com/carts?limit=3",
    "https://dummyjson.com/recipes?limit=3",
    "https://dummyjson.com/posts?limit=5",
    "https://dummyjson.com/comments?limit=5",
    "https://dummyjson.com/todos?limit=5",
    "https://dummyjson.com/quotes?limit=5",
    "https://www.themealdb.com/api/json/v1/1/search.php?s=arrabiata",
    "https://www.themealdb.com/api/json/v1/1/categories.php",
    "https://www.themealdb.com/api/json/v1/1/filter.php?a=Canadian",
    "https://www.thecocktaildb.com/api/json/v1/1/search.php?s=margarita",
    "https://www.thecocktaildb.com/api/json/v1/1/list.php?c=list",
    "https://api.fbi.gov/wanted/v1/list",
    "https://deckofcardsapi.com/api/deck/new/draw/?count=5",
    "https://catfact.ninja/facts?limit=5",
    "https://catfact.ninja/breeds?limit=5",
    "https://api.gbif.org/v1/species/search?q=puma&limit=5",
    "https://api.gbif.org/v1/occurrence/search?limit=5",
    "https://api.gbif.org/v1/dataset?limit=5",
    "https://api.gbif.org/v1/organization?limit=5",
    "https://ll.thespacedevs.com/2.2.0/launch/upcoming/?limit=5",
    "https://ll.thespacedevs.com/2.2.0/agencies/?limit=5",
    "https://ll.thespacedevs.com/2.2.0/astronaut/?limit=5",
    "https://ll.thespacedevs.com/2.2.0/pad/?limit=5",
    "https://api.open5e.com/v1/monsters/?limit=5",
    "https://api.open5e.com/v1/spells/?limit=5",
    "https://api.open5e.com/v1/classes/?limit=3",
    "https://api.open5e.com/v1/races/?limit=5",
    "https://api.open5e.com/v1/magicitems/?limit=5",
    "https://api.open5e.com/v1/conditions/?limit=5",
    "https://hn.algolia.com/api/v1/search?query=python&hitsPerPage=5",
    "https://hn.algolia.com/api/v1/search_by_date?tags=story&hitsPerPage=5",
    "https://api.tvmaze.com/search/shows?q=girls",
    "https://api.tvmaze.com/search/people?q=lauren",
    "https://api.punkapi.online/v3/beers?page=1&per_page=5",
    "https://api.jikan.moe/v4/anime?limit=5",
    "https://api.jikan.moe/v4/manga?limit=5",
    "https://api.jikan.moe/v4/genres/anime",
    "https://api.disneyapi.dev/character?pageSize=5",
    "https://www.anapioficeandfire.com/api/books",
    "https://www.anapioficeandfire.com/api/houses?pageSize=5",
    "https://api.magicthegathering.io/v1/sets",
    "https://api.magicthegathering.io/v1/cards?pageSize=5",
    "https://api.magicthegathering.io/v1/types",
    "https://api.chess.com/pub/titled/GM",
    "https://api.chess.com/pub/player/hikaru",
    "https://api.chess.com/pub/player/hikaru/stats",
    "https://api.chess.com/pub/leaderboards",
    "https://api.spaceflightnewsapi.net/v4/articles/?limit=5",
    "https://api.spaceflightnewsapi.net/v4/blogs/?limit=5",
    "https://api.spaceflightnewsapi.net/v4/reports/?limit=5",
    "https://api.wheretheiss.at/v1/satellites",
    "https://api.nobelprize.org/2.1/nobelPrizes?limit=5",
    "https://api.nobelprize.org/2.1/laureates?limit=5",
    "https://api.postcodes.io/random/postcodes",
    "https://api.postcodes.io/postcodes/SW1A1AA",
    "https://api.crossref.org/works?rows=5",
    "https://api.crossref.org/journals?rows=5",
    "https://api.openalex.org/works?per-page=5",
    "https://api.openalex.org/authors?per-page=5",
    "https://api.openalex.org/institutions?per-page=5",
    "https://api.plos.org/search?q=title:dna&rows=5",
    "https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=malaria&format=json&pageSize=5",
    "https://api.fda.gov/drug/event.json?limit=5",
    "https://api.fda.gov/food/enforcement.json?limit=5",
    "https://api.fda.gov/device/recall.json?limit=5",
    "https://clinicaltrials.gov/api/v2/studies?pageSize=5",
    "https://registry.npmjs.org/-/v1/search?text=sql&size=5",
    "https://crates.io/api/v1/crates?page=1&per_page=5",
    "https://api.rubygems.org/api/v1/search.json?query=rails",
    "https://api.stackexchange.com/2.3/questions?site=stackoverflow&pagesize=5",
    "https://api.stackexchange.com/2.3/tags?site=stackoverflow&pagesize=5",
    "https://api.stackexchange.com/2.3/users?site=stackoverflow&pagesize=5",
    "https://itunes.apple.com/search?term=jack+johnson&limit=5",
    "https://api.deezer.com/search?q=eminem&limit=5",
    "https://musicbrainz.org/ws/2/artist?query=beatles&fmt=json&limit=5",
    "https://openaccess-api.clevelandart.org/api/artworks?limit=5",
    "https://api.vam.ac.uk/v2/objects/search?q=cat&page_size=5",
    "https://collectionapi.metmuseum.org/public/collection/v1/search?q=sunflower",
    "https://images-api.nasa.gov/search?q=moon&page=1",
    "https://api.le-systeme-solaire.net/rest/bodies/?filter[]=isPlanet,eq,true",
    "https://www.amiiboapi.com/api/amiibo/?character=mario",
    "https://api.instantwebtools.net/v1/passenger?page=0&size=5",
    "https://fakerapi.it/api/v1/persons?_quantity=5",
    "https://datausa.io/api/data?drilldowns=Nation&measures=Population",
    "https://api.coincap.io/v2/assets?limit=5",
    "https://api.coinbase.com/v2/exchange-rates?currency=USD",
    "https://api.first.org/data/v1/countries?limit=5",
    "https://content.guardianapis.com/search?api-key=test&page-size=5",
    "https://api.open-notify.org/astros.json",
    "https://api.tfl.gov.uk/Line/Mode/tube/Status",
    "https://api.opentopodata.org/v1/etopo1?locations=57.688709,11.976404",
    "https://api.irail.be/stations/?format=json",
    "https://gbfs.citibikenyc.com/gbfs/en/station_information.json",
    "https://gbfs.citibikenyc.com/gbfs/en/station_status.json",
    "https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch=sql&format=json",
    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term=dna&retmode=json",
    "https://api.escuelajs.co/api/v1/products?limit=5&offset=0",
    "https://api.escuelajs.co/api/v1/categories?limit=5",

    # --- GeoJSON: rows under features, properties one level down ---------
    "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson",
    "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_month.geojson",
    "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson",
    "https://api.weather.gov/alerts/active?limit=5",
    "https://api.weather.gov/stations?limit=5",

    # --- OData and JSON:API: dotted and @-prefixed keys -------------------
    "https://services.odata.org/V4/TripPinServiceRW/People?$top=5",
    "https://services.odata.org/V4/TripPinServiceRW/Airports?$top=5",
    "https://services.odata.org/V4/Northwind/Northwind.svc/Products?$top=5&$format=json",
    "https://services.odata.org/V4/Northwind/Northwind.svc/Customers?$top=5&$format=json",
    "https://services.odata.org/V4/Northwind/Northwind.svc/Orders?$top=5&$format=json",
    "https://kitsu.io/api/edge/anime?page[limit]=5",
    "https://kitsu.io/api/edge/manga?page[limit]=5",

    # --- bare top-level arrays -------------------------------------------
    "https://jsonplaceholder.typicode.com/users",
    "https://jsonplaceholder.typicode.com/posts?_limit=5",
    "https://jsonplaceholder.typicode.com/comments?_limit=5",
    "https://jsonplaceholder.typicode.com/todos?_limit=5",
    "https://jsonplaceholder.typicode.com/albums?_limit=5",
    "https://jsonplaceholder.typicode.com/photos?_limit=5",
    "https://swapi.info/api/people",
    "https://swapi.info/api/planets",
    "https://swapi.info/api/starships",
    "https://swapi.info/api/species",
    "https://swapi.info/api/films",
    "https://api.tvmaze.com/shows?page=0",
    "https://api.tvmaze.com/schedule?country=US",
    "https://date.nager.at/api/v3/PublicHolidays/2026/NO",
    "https://date.nager.at/api/v3/PublicHolidays/2026/US",
    "https://date.nager.at/api/v3/AvailableCountries",
    "https://api.datamuse.com/words?rel_rhy=forgetful",
    "https://api.datamuse.com/sug?s=hipp",
    "https://www.fruityvice.com/api/fruit/all",
    "https://api.openbrewerydb.org/v1/breweries?per_page=5",
    "https://api.openbrewerydb.org/v1/breweries/random?size=3",
    "http://universities.hipolabs.com/search?country=Norway",
    "https://api.github.com/orgs/python/repos?per_page=5",
    "https://api.github.com/repos/python/cpython/contributors?per_page=5",
    "https://api.github.com/repos/python/cpython/tags?per_page=5",
    "https://api.github.com/repos/python/cpython/releases?per_page=3",
    "https://api.github.com/repos/python/cpython/issues?per_page=5",
    "https://api.github.com/licenses",
    "https://api.github.com/gitignore/templates",
    "https://gitlab.com/api/v4/projects?per_page=5",
    "https://restcountries.com/v3.1/region/europe?fields=name,cca3,population",
    "https://restcountries.com/v3.1/currency/nok",
    "https://api.chucknorris.io/jokes/categories",
    "https://official-joke-api.appspot.com/jokes/ten",
    "https://api.sampleapis.com/coffee/hot",
    "https://api.sampleapis.com/wines/reds",
    "https://api.sampleapis.com/beers/ale",
    "https://api.sampleapis.com/switch/games",
    "https://api.sampleapis.com/futurama/characters",
    "https://api.sampleapis.com/movies/animation",
    "https://dog.ceo/api/breeds/image/random/5",
    "https://api.dictionaryapi.dev/api/v2/entries/en/hello",
    "https://api.urbandictionary.com/v0/define?term=api",
    "https://zenquotes.io/api/quotes",
    "https://ghibliapi.vercel.app/films",
    "https://fakestoreapi.com/products?limit=5",
    "https://fakestoreapi.com/carts?limit=5",
    "https://random-data-api.com/api/v2/users?size=5",
    "https://v6.db.transport.rest/locations?query=berlin&results=5",
    "https://api.nbp.pl/api/exchangerates/tables/A?format=json",
    "https://lobste.rs/hottest.json",
    "https://dev.to/api/articles?per_page=5",
    "https://api.spacexdata.com/v4/rockets",
    "https://api.spacexdata.com/v4/capsules",
    "https://api.spacexdata.com/v4/crew",
    "https://api.spacexdata.com/v4/launchpads",
    "https://api.spacexdata.com/v4/dragons",
    "https://api.spacexdata.com/v4/ships",
    "https://api.spacexdata.com/v4/history",
    "https://packagist.org/packages/list.json?vendor=symfony",

    # --- arrays of bare scalars, no keys at all ---------------------------
    "https://hacker-news.firebaseio.com/v0/topstories.json",
    "https://hacker-news.firebaseio.com/v0/newstories.json",
    "https://collectionapi.metmuseum.org/public/collection/v1/objects?departmentIds=1",
    "https://api.first.org/data/v1/countries",

    # --- maps keyed by an identifier --------------------------------------
    "https://api.frankfurter.app/latest",
    "https://api.frankfurter.app/currencies",
    "https://api.frankfurter.app/2020-01-01",
    "https://open.er-api.com/v6/latest/USD",
    "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd",
    "https://blockchain.info/ticker",
    "https://api.exchangerate-api.com/v4/latest/USD",
    "https://api.github.com/repos/python/cpython/languages",
    "https://dog.ceo/api/breeds/list/all",
    "https://api.publicapis.org/categories",

    # --- parallel arrays, one per column ----------------------------------
    "https://api.open-meteo.com/v1/forecast?latitude=59.9&longitude=10.7&hourly=temperature_2m&forecast_days=1",
    "https://api.open-meteo.com/v1/forecast?latitude=51.5&longitude=-0.1&daily=temperature_2m_max&forecast_days=3",
    "https://api.open-meteo.com/v1/forecast?latitude=35.7&longitude=139.7&hourly=temperature_2m,relative_humidity_2m,wind_speed_10m&forecast_days=2",
    "https://air-quality-api.open-meteo.com/v1/air-quality?latitude=59.9&longitude=10.7&hourly=pm10&forecast_days=1",
    "https://marine-api.open-meteo.com/v1/marine?latitude=54.3&longitude=10.1&hourly=wave_height&forecast_days=1",
    "https://archive-api.open-meteo.com/v1/archive?latitude=59.9&longitude=10.7&start_date=2024-01-01&end_date=2024-01-03&daily=temperature_2m_max",
    "https://api.github.com/repos/python/cpython/stats/participation",

    # --- metadata first, rows second --------------------------------------
    "https://api.worldbank.org/v2/country/NO/indicator/SP.POP.TOTL?format=json&per_page=5",
    "https://api.worldbank.org/v2/country?format=json&per_page=5",
    "https://api.worldbank.org/v2/sources?format=json",

    # --- one object that is one row ---------------------------------------
    "https://api.github.com/repos/python/cpython",
    "https://api.github.com/repos/torvalds/linux",
    "https://api.agify.io?name=william",
    "https://api.genderize.io?name=luc",
    "https://api.nationalize.io?name=nathaniel",
    "https://api.sunrise-sunset.org/json?lat=59.9&lng=10.7",
    "https://api.kanye.rest",
    "https://api.adviceslip.com/advice",
    "https://uselessfacts.jsph.pl/api/v2/facts/random",
    "https://api.chucknorris.io/jokes/random",
    "https://official-joke-api.appspot.com/random_joke",
    "https://api.ipify.org?format=json",
    "https://ipapi.co/json/",
    "https://api.zippopotam.us/us/33162",
    "https://catfact.ninja/fact",
    "https://dog.ceo/api/breeds/image/random",
    "https://api.thecatapi.com/v1/images/search",
    "https://api.artic.edu/api/v1/artworks/129884",
    "https://xkcd.com/info.0.json",
    "https://hacker-news.firebaseio.com/v0/item/8863.json",
    "https://hacker-news.firebaseio.com/v0/user/jl.json",
    "https://pypi.org/pypi/requests/json",
    "https://en.wikipedia.org/api/rest_v1/page/summary/Structured_Query_Language",
    "https://api.bigdatacloud.net/data/reverse-geocode-client?latitude=59.9&longitude=10.7&localityLanguage=en",
    "https://api.lyrics.ovh/v1/coldplay/yellow",
    "https://api.duckduckgo.com/?q=sql&format=json",
    "https://collectionapi.metmuseum.org/public/collection/v1/objects/45734",
    "https://api.open-notify.org/iss-now.json",
    "https://worldtimeapi.org/api/timezone/Europe/Oslo",
    "https://api.spacexdata.com/v4/company",
    "https://api.spacexdata.com/v4/roadster",
    "https://wttr.in/Oslo?format=j1",

    # --- nested, deep, or not tabular at all -------------------------------
    "https://pokeapi.co/api/v2/pokemon/pikachu",
    "https://pokeapi.co/api/v2/berry/1",
    "https://pokeapi.co/api/v2/pokemon-species/1",
    "https://rickandmortyapi.com/api/character/1",
    "https://api.github.com",
    "https://pokeapi.co/api/v2/",
    "https://rickandmortyapi.com/api/",
    "https://swapi.info/api/",
    "https://api.spacexdata.com/v4",
    "https://httpbin.org/uuid",
    "https://httpbin.org/json",
    "https://httpbin.org/headers",
    "https://api.nuget.org/v3/index.json",
    "https://worldtimeapi.org/api/timezone",

    # --- feeds and markup ---------------------------------------------------
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://hnrss.org/frontpage",
    "https://www.w3schools.com/xml/cd_catalog.xml",
    "https://www.w3schools.com/xml/plant_catalog.xml",
    "https://www.w3schools.com/xml/simple.xml",
    "https://github.com/python/cpython/releases.atom",
    "https://xkcd.com/rss.xml",
    "https://export.arxiv.org/api/query?search_query=all:database&max_results=5",
    "https://api.nasa.gov/planetary/apod?api_key=DEMO_KEY",
    "https://api.nasa.gov/neo/rest/v1/feed?api_key=DEMO_KEY",
    "https://www.federalreserve.gov/feeds/press_all.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
    "https://en.wikipedia.org/wiki/List_of_programming_languages",
    "https://www.w3schools.com/html/html_tables.asp",
]

# Served as CSV, which announces itself nowhere in its bytes and so is asked
# for by configuration rather than sniffed. Open data portals serve far more
# CSV than JSON, and the same file read off a disk has always worked.
CSV_ENDPOINTS = [
    "https://people.sc.fsu.edu/~jburkardt/data/csv/airtravel.csv",
    "https://people.sc.fsu.edu/~jburkardt/data/csv/biostats.csv",
    "https://people.sc.fsu.edu/~jburkardt/data/csv/cities.csv",
    "https://raw.githubusercontent.com/plotly/datasets/master/2014_apple_stock.csv",
    "https://raw.githubusercontent.com/datasciencedojo/datasets/master/titanic.csv",
    "https://data.cityofnewyork.us/api/views/25th-nujf/rows.csv?accessType=DOWNLOAD",
    "https://raw.githubusercontent.com/datasets/country-list/main/data/data.csv",
]


def every() -> list[str]:
    """Every endpoint, whatever format it serves."""
    return ENDPOINTS + CSV_ENDPOINTS


def cached(cache: pathlib.Path, url: str) -> pathlib.Path:
    return cache / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".bin")


def collect(cache: pathlib.Path, url: str) -> tuple[str, str | None]:
    """Fetch a URL into the cache, or report why not. Already cached is free."""
    where = cached(cache, url)
    if where.exists():
        return url, None
    try:
        raw = fetch(url, {}, TIMEOUT)
    except Exception as exc:                                # noqa: BLE001
        return url, f"{type(exc).__name__}: {exc}"[:70]
    where.write_bytes(raw)
    return url, None


def decode(raw: bytes, url: str) -> object:
    """The same decision HttpSource makes, on the same bytes."""
    raw = without_bom(raw)
    if url in CSV_ENDPOINTS:
        return csv_records(raw, url)
    kind = sniff(raw)
    if kind == "xml":
        return parse_xml(raw, url)
    if kind == "html":
        return parse_html(raw, url)
    return json.loads(raw)


def leaves(node: object, depth: int = 0) -> int:
    """How many scalars the flattener should produce for this record."""
    if isinstance(node, dict) and depth < 6 and node:
        return sum(leaves(value, depth + 1) for value in node.values())
    return 1


def sortable(values: list) -> bool:
    """Whether ORDER BY and a Python sort are comparing the same thing."""
    kinds = {type(value) for value in values}
    if len(values) < 2 or len(kinds) != 1:
        return False
    return kinds.pop() in (str, int, float)


class Survey:
    """What the grader learned, kept apart from how it prints."""

    def __init__(self) -> None:
        self.stages: collections.Counter = collections.Counter()
        self.readings: collections.Counter = collections.Counter()
        self.problems: list[tuple[str, str, str]] = []
        self.refused: list[str] = []
        self.widest = 0
        self.deepest = 0
        self.dotted: list[str] = []
        self.children = 0
        self.parents_with_children = 0

    def failed(self, url: str, stage: str, why: str) -> None:
        self.problems.append((url.split("//")[-1][:52], stage, why[:64]))


def grade(url: str, raw: bytes, survey: Survey) -> None:
    """Push one response as far through the pipeline as it will go."""
    try:
        document = decode(raw, url)
    except Exception as exc:                                # noqa: BLE001
        survey.failed(url, "decode", f"{type(exc).__name__}: {exc}")
        return
    survey.stages["decoded"] += 1

    if is_rejection(document):
        survey.readings["refused: the API said no"] += 1
        return

    shape = detect(document)
    if shape is None:
        survey.readings["refused: no reading"] += 1
        survey.failed(url, "read", describe(document))
        return
    survey.stages["read"] += 1
    survey.readings[shape.records] += 1

    try:
        records = list(locate(extract(document, shape.path, url), shape.records, url))
    except SourceError as exc:
        survey.failed(url, "build", str(exc))
        return
    if not records:
        # A response with no rows has no columns either, so serving it needs
        # them named. Refusing is the answer, not a defect to chase.
        survey.readings["refused: nothing came back"] += 1
        survey.refused.append(url.split("//")[-1][:52])
        return
    try:
        table = from_records(records, name="t", origin=url)
    except SourceError as exc:
        survey.failed(url, "build", str(exc))
        return
    if not table.columns:
        survey.failed(url, "build", "no columns")
        return
    survey.stages["built"] += 1

    # Flattening is lossless: every leaf of every record reached a column.
    shaped = [record for record in records if isinstance(record, dict)]
    for record in shaped[:20]:
        flat = flatten_record(record)
        survey.widest = max(survey.widest, len(flat))
        survey.deepest = max(survey.deepest,
                             max((name.count(".") for name in flat), default=0))
        survey.dotted += [k for k in record if isinstance(k, str) and "." in k]
        lost = leaves(record) - len(flat)
        if lost:
            survey.failed(url, "check", f"flattening lost {lost} leaves")
            return
    if len(table.columns) > MAX_COLUMNS:
        survey.failed(url, "check", f"{len(table.columns)} columns, past the cap")
        return

    catalog = Catalog()
    catalog.add(table)
    first = table.column_names[0]
    try:
        counted = catalog.answer("SELECT COUNT(*) AS n FROM t").rows[0][0]
        ordered = catalog.answer(f"SELECT [{first}] FROM t ORDER BY [{first}]").rows
        catalog.answer(f"SELECT [{first}], COUNT(*) AS n FROM t GROUP BY [{first}]")
        catalog.answer(f"SELECT TOP 3 * FROM t WHERE [{first}] IS NOT NULL")
    except Exception as exc:                                # noqa: BLE001
        survey.failed(url, "query", f"{type(exc).__name__}: {exc}")
        return
    survey.stages["queried"] += 1

    # The answers agree with the data they came from.
    if counted != len(table.rows):
        survey.failed(url, "check", f"COUNT said {counted}, table has {len(table.rows)}")
        return
    values = [row[table.index_of(first)] for row in table.rows]
    if sortable(values):
        expected = sorted(values,
                          key=lambda v: collated(v) if isinstance(v, str) else v)
        if [row[0] for row in ordered] != expected:
            survey.failed(url, "check", f"ORDER BY disagreed with a sort on '{first}'")
            return

    # Child tables keep every element of the arrays they came from.
    built = child_tables("t", shaped, identifying_column(shaped))
    if built:
        survey.parents_with_children += 1
        survey.children += len(built)
    for name, child in built.items():
        column = name[len("t_"):]
        # A list inside a list has no shape to give, so it contributes no row.
        elements = sum(
            1 for record in shaped
            if isinstance(record.get(column), list)
            for element in record[column]
            if not isinstance(element, list)
        )
        if elements and len(child.rows) != elements:
            survey.failed(url, "check",
                          f"{name} has {len(child.rows)} rows for {elements} elements")
            return
    survey.stages["checked"] += 1


def main() -> int:
    parser = argparse.ArgumentParser(description="grade the pipeline over public APIs")
    parser.add_argument("--cache", default=os.environ.get("PYSQLBRIDGE_CORPUS"),
                        help="where responses are kept between runs")
    parser.add_argument("--offline", action="store_true",
                        help="grade what is cached, ask nobody's server")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    cache = pathlib.Path(
        args.cache or (pathlib.Path(tempfile.gettempdir()) / "pysqlbridge-corpus")
    )
    cache.mkdir(parents=True, exist_ok=True)

    unreachable: list[tuple[str, str]] = []
    if not args.offline:
        missing = [url for url in every() if not cached(cache, url).exists()]
        if missing:
            print(f"fetching {len(missing)} of {len(every())} into {cache}")
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
                for url, error in pool.map(lambda u: collect(cache, u), missing):
                    if error:
                        unreachable.append((url.split("//")[-1][:52], error))

    survey = Survey()
    have = [url for url in every() if cached(cache, url).exists()]
    for url in have:
        grade(url, cached(cache, url).read_bytes(), survey)

    print(f"\n{len(have)} of {len(every())} responses in hand"
          + (f", {len(unreachable)} unreachable" if unreachable else ""))
    for stage in ("decoded", "read", "built", "queried", "checked"):
        print(f"  {stage:9} {survey.stages[stage]:4}")

    print("\nreadings chosen:")
    for reading, count in survey.readings.most_common():
        print(f"  {reading:26} {count}")

    print(f"\nnesting: {survey.parents_with_children} responses made "
          f"{survey.children} child tables; widest row {survey.widest} columns, "
          f"deepest path {survey.deepest} dots")
    if survey.dotted:
        print(f"keys containing a dot: {len(survey.dotted)}, "
              f"{len(set(survey.dotted))} distinct, e.g. "
              f"{sorted(set(survey.dotted))[:3]}")

    if unreachable:
        print(f"\n{len(unreachable)} unreachable:")
        for short, why in unreachable:
            print(f"  {short:54} {why}")

    if survey.refused:
        print(f"\nnothing came back from {len(survey.refused)}: "
              f"{', '.join(survey.refused)}")

    if survey.problems:
        print(f"\n{len(survey.problems)} to look at:")
        for short, stage, why in survey.problems:
            print(f"  {short:54} {stage:6} {why}")
    return 1 if survey.problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
