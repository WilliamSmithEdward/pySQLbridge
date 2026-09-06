import json

import pytest

from pysqlbridge.http_source import (
    DEFAULT_TIMEOUT_SECONDS,
    HttpSource,
    StaticSource,
    extract,
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

    def test_an_envelope_without_a_path_is_not_a_table(self):
        # Selecting from the envelope would give one row of metadata.
        with pytest.raises(SourceError, match="not a JSON array"):
            source(path=None).load()

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
