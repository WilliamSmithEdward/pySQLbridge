import codecs
import json
import threading

import pytest

from pysqlbridge.http_source import (
    DEFAULT_TIMEOUT_SECONDS,
    HttpSource,
    Paging,
    StaticSource,
    extract,
    stride_between,
)
from pysqlbridge.source import SourceError, from_records

PAYLOAD = {
    "count": 3,
    "next": None,
    "results": [
        {"name": "bulbasaur", "url": "https://example.test/1/"},
        {"name": "ivysaur", "url": "https://example.test/2/"},
    ],
}


class Recorder:
    """A fetcher that answers from memory and counts its calls.

    The tests must not depend on a network, and a real endpoint would make them
    slow, flaky and rude to whoever runs it.
    """

    def __init__(self, payload=PAYLOAD, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[tuple[str, dict, float]] = []

    def __call__(self, url: str, headers: dict, timeout: float) -> bytes:
        self.calls.append((url, headers, timeout))
        if self.error:
            raise self.error
        return json.dumps(self.payload).encode("utf-8")


class SlowRecorder:
    """Wraps a fetcher and makes it slow, so a race has time to happen."""

    def __init__(self, inner, delay: float = 0.05) -> None:
        self.inner = inner
        self.delay = delay

    def __call__(self, url: str, headers: dict, timeout: float) -> bytes:
        import time

        time.sleep(self.delay)
        return self.inner(url, headers, timeout)


class Paged:
    """A fetcher serving a collection ten rows at a time, counting overlap."""

    def __init__(self, rows: int, per_page: int) -> None:
        self.rows, self.per_page = rows, per_page
        self.offsets: list[int] = []
        self.most_at_once = 0
        self._live = 0
        self._lock = threading.Lock()

    def __call__(self, url: str, headers: dict, timeout: float) -> bytes:
        import time
        from urllib.parse import parse_qs, urlsplit

        with self._lock:
            self._live += 1
            self.most_at_once = max(self.most_at_once, self._live)
        try:
            time.sleep(0.02)
            offset = int(parse_qs(urlsplit(url).query).get("offset", ["0"])[0])
            self.offsets.append(offset)
            window = list(range(offset, min(offset + self.per_page, self.rows)))
            following = offset + self.per_page
            return json.dumps({
                "count": self.rows,
                "next": (f"https://h.test/p?offset={following}&limit={self.per_page}"
                         if following < self.rows else None),
                "results": [{"id": i} for i in window],
            }).encode()
        finally:
            with self._lock:
                self._live -= 1


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def source(**kwargs) -> HttpSource:
    kwargs.setdefault("name", "pokemon")
    kwargs.setdefault("url", "https://example.test/api")
    kwargs.setdefault("path", "results")
    kwargs.setdefault("fetcher", Recorder())
    kwargs.setdefault("clock", Clock())
    return HttpSource(**kwargs)


class TestExtract:
    def test_no_path_returns_the_whole_payload(self):
        assert extract(PAYLOAD, None, "u") is PAYLOAD

    def test_walks_one_level(self):
        assert extract(PAYLOAD, "results", "u") == PAYLOAD["results"]

    def test_walks_a_dotted_path(self):
        nested = {"data": {"items": [1, 2]}}
        assert extract(nested, "data.items", "u") == [1, 2]

    def test_a_missing_key_lists_what_is_there(self):
        with pytest.raises(SourceError, match="count, next, results"):
            extract(PAYLOAD, "nope", "u")

    def test_walking_into_a_non_object_says_which_part(self):
        with pytest.raises(SourceError, match=r"'count\.deeper' is not reachable"):
            extract(PAYLOAD, "count.deeper", "u")

    def test_that_message_names_the_type_it_hit(self):
        with pytest.raises(SourceError, match="'count' is an int, not an object"):
            extract(PAYLOAD, "count.deeper", "u")


class TestLoading:
    def test_builds_a_table_from_the_extracted_array(self):
        table = source().load()
        assert table.name == "pokemon"
        assert table.column_names == ["name", "url"]
        assert table.rows[0][0] == "bulbasaur"

    def test_an_envelope_without_a_path_finds_its_own_rows(self):
        # The envelope's one interesting key is "results", and detection finds
        # it, so a URL on its own is enough. Reading the envelope itself would
        # have given a single row of paging metadata.
        table = source(path=None).load()
        assert table.column_names == ["name", "url"]
        assert len(table.rows) == 2

    def test_a_named_strategy_that_does_not_fit_names_the_others(self):
        # Naming a strategy turns detection off, so a wrong one is an error
        # rather than something to work around.
        with pytest.raises(SourceError, match="where a list was expected"):
            source(path=None, records="array").load()

    def test_that_message_names_the_other_strategies(self):
        with pytest.raises(SourceError, match="array, single, values, entries, columns"):
            source(path=None, records="array").load()

    def test_invalid_json_says_so(self):
        class Broken(Recorder):
            def __call__(self, url, headers, timeout):
                return b"{not json"

        with pytest.raises(SourceError, match="did not return valid JSON"):
            source(fetcher=Broken()).load()

    def test_a_fetch_failure_surfaces_as_a_source_error(self):
        failing = Recorder(error=SourceError("could not reach https://example.test"))
        with pytest.raises(SourceError, match="could not reach"):
            source(fetcher=failing).load()


class TestCsv:
    """A CSV off a URL is the same file as a CSV off a disk.

    Asked for rather than sniffed: a CSV announces itself nowhere in its bytes,
    so reading an unparseable response as one would turn every broken JSON API
    into a table of text.
    """

    def served(self, body: bytes, **kwargs):
        class Csv(Recorder):
            def __call__(self, url, headers, timeout):
                return body

        kwargs.setdefault("format", "csv")
        kwargs.setdefault("path", None)
        return source(fetcher=Csv(), **kwargs)

    def test_a_csv_response_becomes_a_table(self):
        table = self.served(b"name,age\nada,36\ngrace,45\n").load()
        assert table.column_names == ["name", "age"]
        assert table.rows == [["ada", 36], ["grace", 45]]

    def test_the_types_are_inferred_like_any_other_source(self):
        table = self.served(b"n,x\n1,1.5\n2,2.5\n").load()
        assert [c.type.__class__.__name__ for c in table.columns] == ["Integer", "Float"]

    def test_a_quoted_field_may_hold_the_delimiter(self):
        # Which is most of the Titanic passenger list: "Braund, Mr. Owen".
        table = self.served(b'name,note\n"Braund, Mr. Owen",first\n').load()
        assert table.rows == [["Braund, Mr. Owen", "first"]]

    def test_a_ragged_line_is_refused_with_its_number(self):
        with pytest.raises(SourceError, match="line 3 has 3 fields"):
            self.served(b"a,b\n1,2\n1,2,3\n").load()

    def test_a_mark_from_a_spreadsheet_export_is_not_part_of_the_name(self):
        table = self.served(codecs.BOM_UTF8 + b"name,age\nada,36\n").load()
        assert table.column_names == ["name", "age"]

    def test_an_empty_field_is_null(self):
        table = self.served(b"a,b\n1,\n2,x\n").load()
        assert table.rows[0][1] is None

    def test_json_that_will_not_parse_names_csv_as_a_possibility(self):
        class Text(Recorder):
            def __call__(self, url, headers, timeout):
                return b"name,age\nada,36\n"

        with pytest.raises(SourceError, match='set "format": "csv"'):
            source(fetcher=Text(), path=None).load()


class TestByteOrderMark:
    """A response written by Microsoft tooling carries one.

    The file readers have stripped it since the executable smoke test found a
    BOM in a generated config. A response over the network is the same bytes
    with the same problem: the Federal Reserve press feed serves one, and
    without stripping it the feed is sniffed as JSON and then fails as JSON.
    """

    def marked(self, body: bytes, **kwargs):
        class Marked(Recorder):
            def __call__(self, url, headers, timeout):
                return codecs.BOM_UTF8 + body

        return source(fetcher=Marked(), **kwargs)

    def test_a_marked_json_response_still_loads(self):
        table = self.marked(json.dumps(PAYLOAD).encode("utf-8")).load()
        assert table.column_names == ["name", "url"]

    def test_a_marked_feed_still_loads(self):
        feed = (
            b'<?xml version="1.0"?><rss><channel>'
            b"<item><title>First</title></item>"
            b"<item><title>Second</title></item>"
            b"</channel></rss>"
        )
        table = self.marked(feed, path=None).load()
        assert [row[0] for row in table.rows] == ["First", "Second"]


class TestTimeout:
    def test_every_request_carries_one(self):
        # A source that hangs holds the connection thread that asked for it.
        recorder = Recorder()
        source(fetcher=recorder, timeout=7.5).load()
        assert recorder.calls[0][2] == 7.5

    def test_the_default_is_finite(self):
        assert 0 < DEFAULT_TIMEOUT_SECONDS < float("inf")


class TestCaching:
    def test_a_second_load_does_not_refetch(self):
        recorder, clock = Recorder(), Clock()
        s = source(fetcher=recorder, clock=clock, ttl=100)
        s.load()
        s.load()
        assert len(recorder.calls) == 1

    def test_it_refetches_once_the_cache_expires(self):
        recorder, clock = Recorder(), Clock()
        s = source(fetcher=recorder, clock=clock, ttl=100)
        s.load()
        clock.advance(101)
        s.load()
        assert len(recorder.calls) == 2

    def test_it_does_not_refetch_just_before_expiry(self):
        recorder, clock = Recorder(), Clock()
        s = source(fetcher=recorder, clock=clock, ttl=100)
        s.load()
        clock.advance(99)
        s.load()
        assert len(recorder.calls) == 1

    def test_invalidate_forces_the_next_load_to_fetch(self):
        recorder = Recorder()
        s = source(fetcher=recorder, ttl=10_000)
        s.load()
        s.invalidate()
        s.load()
        assert len(recorder.calls) == 2

    def test_a_scan_does_not_become_a_burst_of_requests(self):
        recorder = Recorder()
        s = source(fetcher=recorder, ttl=100)
        for _ in range(20):
            s.load()
        assert len(recorder.calls) == 1


class TestHeaders:
    def test_supplied_headers_reach_the_fetcher(self):
        recorder = Recorder()
        source(fetcher=recorder, headers={"Authorization": "Bearer x"}).load()
        assert recorder.calls[0][1] == {"Authorization": "Bearer x"}


class TestStaticSource:
    def test_hands_back_the_table_it_holds(self):
        table = from_records([{"a": 1}], name="t")
        wrapped = StaticSource(table)
        assert wrapped.name == "t"
        assert wrapped.load() is table

    def test_loading_repeatedly_is_free(self):
        table = from_records([{"a": 1}], name="t")
        wrapped = StaticSource(table)
        assert wrapped.load() is wrapped.load()


class TestConcurrency:
    """One connection per client means several threads reach a source at once."""

    def test_threads_arriving_together_cause_one_fetch(self):
        # Without the lock they all miss the cache and all fetch, turning a
        # busy moment into a burst at somebody else's API.
        import threading

        started = threading.Barrier(8)
        recorder = Recorder()
        slow = SlowRecorder(recorder, delay=0.05)
        s = source(fetcher=slow, ttl=100)

        def hammer():
            started.wait()
            s.load()

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(recorder.calls) == 1

    def test_every_thread_gets_the_same_table(self):
        import threading

        recorder = Recorder()
        s = source(fetcher=recorder, ttl=100)
        seen = []
        lock = threading.Lock()

        def collect():
            table = s.load()
            with lock:
                seen.append(table)

        threads = [threading.Thread(target=collect) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(seen) == 6
        assert all(table is seen[0] for table in seen)

    def test_invalidate_is_safe_while_others_read(self):
        import threading

        s = source(fetcher=Recorder(), ttl=100)
        s.load()
        errors = []

        def churn(fn):
            try:
                for _ in range(50):
                    fn()
            except Exception as exc:      # noqa: BLE001 - the test is the assert
                errors.append(exc)

        threads = [
            threading.Thread(target=churn, args=(s.load,)),
            threading.Thread(target=churn, args=(s.invalidate,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []


class TestMirrors:
    """Several URLs serving the same data, raced."""

    class Replicas:
        def __init__(self, plan):
            self.plan = plan
            self.hits = []

        def __call__(self, url, headers, timeout):
            import time

            self.hits.append(url)
            delay, outcome = self.plan[url]
            time.sleep(delay)
            if outcome == "ok":
                return json.dumps(PAYLOAD).encode()
            raise SourceError(f"{url} is {outcome}")

    def test_the_quickest_healthy_replica_answers(self):
        import time

        plan = {"http://slow": (0.30, "ok"), "http://quick": (0.02, "ok")}
        s = source(url=list(plan), fetcher=self.Replicas(plan), path="results")
        start = time.perf_counter()
        s.load()
        assert time.perf_counter() - start < 0.20

    def test_a_fast_failure_does_not_beat_a_slow_success(self):
        # First successful, not first finished, which is the whole point.
        plan = {"http://broken": (0.01, "down"), "http://good": (0.10, "ok")}
        s = source(url=list(plan), fetcher=self.Replicas(plan), path="results")
        assert s.load().rows

    def test_every_replica_is_tried(self):
        plan = {"http://a": (0.02, "ok"), "http://b": (0.02, "ok")}
        replicas = self.Replicas(plan)
        source(url=list(plan), fetcher=replicas, path="results").load()
        assert sorted(replicas.hits) == ["http://a", "http://b"]

    def test_all_down_reports_every_reason(self):
        plan = {"http://a": (0.01, "down"), "http://b": (0.01, "on fire")}
        s = source(url=list(plan), fetcher=self.Replicas(plan), path="results")
        with pytest.raises(SourceError) as caught:
            s.load()
        assert "http://a" in str(caught.value) and "on fire" in str(caught.value)

    def test_one_url_still_works(self):
        assert source().load().rows


class TestPagination:
    """Following a next link, bounded."""

    class Pages:
        """Serves numbered pages, each pointing at the next."""

        def __init__(self, count, key="next", nest=False):
            self.count = count
            self.key = key
            self.nest = nest
            self.calls = 0

        def __call__(self, url, headers, timeout):
            self.calls += 1
            page = self.calls
            body = {"results": [{"page": page, "row": i} for i in range(2)]}
            if page < self.count:
                nxt = f"http://page/{page + 1}"
                body["info"] = {self.key: nxt} if self.nest else None
                if not self.nest:
                    body[self.key] = nxt
            return json.dumps(body).encode()

    def test_without_a_next_key_only_one_page_is_read(self):
        pages = self.Pages(5)
        table = source(fetcher=pages, path="results").load()
        assert pages.calls == 1 and len(table.rows) == 2

    def test_following_a_top_level_next_key(self):
        pages = self.Pages(3)
        table = source(fetcher=pages, path="results", next_key="next").load()
        assert pages.calls == 3 and len(table.rows) == 6

    def test_following_a_nested_next_key(self):
        # Rick and Morty puts it under info.
        pages = self.Pages(3, nest=True)
        table = source(fetcher=pages, path="results", next_key="info.next").load()
        assert pages.calls == 3 and len(table.rows) == 6

    def test_max_pages_bounds_the_walk(self):
        pages = self.Pages(50)
        source(fetcher=pages, path="results", next_key="next", max_pages=4).load()
        assert pages.calls == 4

    def test_a_runaway_source_is_refused_rather_than_truncated(self):
        # Truncating silently would look like the API only had this many rows.
        pages = self.Pages(50)
        s = source(fetcher=pages, path="results", next_key="next",
                   max_pages=50, max_rows=5)
        with pytest.raises(SourceError, match="past the 5 it is allowed"):
            s.load()


class TestStaleWhileRevalidate:
    """An expiry should not make a query wait on the network."""

    def test_the_first_load_has_to_wait(self):
        recorder, clock = Recorder(), Clock()
        s = source(fetcher=recorder, clock=clock, ttl=100)
        assert s.load().rows
        assert len(recorder.calls) == 1

    def test_an_expiry_serves_the_previous_answer_at_once(self):
        recorder, clock = Recorder(), Clock()
        s = source(fetcher=recorder, clock=clock, ttl=100)
        first = s.load()
        clock.advance(101)
        assert s.load() is first          # the same table, not a new fetch

    def test_the_expiry_starts_exactly_one_refresh(self):
        import time

        recorder, clock = Recorder(), Clock()
        s = source(fetcher=recorder, clock=clock, ttl=100)
        s.load()
        clock.advance(101)
        for _ in range(5):
            s.load()
        time.sleep(0.2)
        assert len(recorder.calls) == 2   # the first, and one refresh

    def test_a_second_expiry_refreshes_again(self):
        # The refresh flag has to be cleared when a refresh succeeds, not only
        # when one fails. Left set, every later expiry finds a refresh already
        # in progress, and the source serves its first answer forever.
        import time

        recorder, clock = Recorder(), Clock()
        s = source(fetcher=recorder, clock=clock, ttl=100)
        s.load()
        for _ in range(3):
            clock.advance(101)
            s.load()
            time.sleep(0.2)
        assert len(recorder.calls) == 4   # the first, and one per expiry

    def test_a_failed_background_refresh_keeps_serving_the_stale_table(self):
        import time

        class FailsAfterFirst:
            def __init__(self):
                self.calls = 0

            def __call__(self, url, headers, timeout):
                self.calls += 1
                if self.calls > 1:
                    raise SourceError("the API went away")
                return json.dumps(PAYLOAD).encode()

        clock = Clock()
        s = source(fetcher=FailsAfterFirst(), clock=clock, ttl=100)
        first = s.load()
        clock.advance(101)
        assert s.load() is first
        time.sleep(0.2)
        assert s.load().rows              # still serving, not raising


class TestCompression:
    def test_a_gzipped_response_is_read(self):
        import gzip as gziplib

        class Gzipped:
            def __call__(self, url, headers, timeout):
                # The real fetcher decompresses; this checks the helper it uses.
                return gziplib.compress(json.dumps(PAYLOAD).encode())

        from pysqlbridge.http_source import _decompress

        raw = Gzipped()("u", {}, 1)
        assert json.loads(_decompress(raw, "gzip")) == PAYLOAD

    def test_an_uncompressed_response_passes_through(self):
        from pysqlbridge.http_source import _decompress

        body = json.dumps(PAYLOAD).encode()
        assert _decompress(body, "") == body

    def test_a_broken_compressed_body_says_so(self):
        from pysqlbridge.http_source import _decompress

        with pytest.raises(SourceError, match="could not decompress"):
            _decompress(b"not actually gzip", "gzip")


class TestReadingPagesInParallel:
    """Two consecutive next links say how to write the rest down.

    Once the pattern is known the remaining pages do not have to be asked for
    one at a time, which is the difference between one round trip per page and
    one per batch.
    """

    def test_two_consecutive_links_reveal_the_moving_parameter(self):
        assert stride_between(
            "https://h.test/p?offset=20&limit=20",
            "https://h.test/p?offset=40&limit=20",
        ) == ("offset", 20)

    def test_a_page_number_counts_as_a_stride(self):
        assert stride_between("https://h.test/p?page=2",
                              "https://h.test/p?page=3") == ("page", 1)

    def test_a_bare_first_url_settles_nothing(self):
        # The PokeAPI answers /pokemon with a link to ?offset=20&limit=20,
        # where an offset and a page size look equally new.
        assert stride_between("https://h.test/p",
                              "https://h.test/p?offset=20&limit=20") is None

    def test_two_parameters_moving_together_settle_nothing(self):
        assert stride_between("https://h.test/p?a=1&b=1",
                              "https://h.test/p?a=2&b=2") is None

    def test_a_different_path_is_not_the_next_page(self):
        assert stride_between("https://h.test/p?page=1",
                              "https://h.test/q?page=2") is None

    def test_every_page_is_read_and_in_order(self):
        pages = Paged(rows=95, per_page=10)
        table = HttpSource(name="t", url="https://h.test/p", path="results",
                           records="array", next_key="next", fetcher=pages,
                           page_workers=4).load()
        assert len(table.rows) == 95
        assert [row[0] for row in table.rows] == list(range(95))

    def test_the_batches_actually_overlap(self):
        pages = Paged(rows=95, per_page=10)
        HttpSource(name="t", url="https://h.test/p", path="results",
                   records="array", next_key="next", fetcher=pages,
                   page_workers=4).load()
        assert pages.most_at_once > 1, "the pages were fetched one at a time"

    def test_it_stops_where_the_api_says_it_does(self):
        # Never asks for a page beyond the collection when the total is known.
        pages = Paged(rows=95, per_page=10)
        HttpSource(name="t", url="https://h.test/p", path="results",
                   records="array", next_key="next", fetcher=pages,
                   page_workers=4).load()
        assert max(pages.offsets) < 95

    def test_a_page_bound_still_holds(self):
        pages = Paged(rows=500, per_page=10)
        table = HttpSource(name="t", url="https://h.test/p", path="results",
                           records="array", next_key="next", fetcher=pages,
                           page_workers=4, max_pages=6).load()
        assert len(table.rows) == 60

    def test_an_api_that_ignores_the_parameter_is_not_read_twice(self):
        # It answers every request with page one. Following that would serve
        # max_pages copies of the same rows.
        class Stuck:
            def __call__(self, url, headers, timeout):
                return json.dumps({
                    "count": 500,
                    "next": "https://h.test/p?offset=10",
                    "results": [{"id": i} for i in range(10)],
                }).encode()

        table = HttpSource(name="t", url="https://h.test/p", path="results",
                           records="array", next_key="next",
                           fetcher=Stuck()).load()
        assert len(table.rows) == 10
