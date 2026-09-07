import json

import pytest

from pysqlbridge.credentials import credential
from pysqlbridge.http_source import HttpSource
from pysqlbridge.discover import (
    Crawler,
    _is_index,
    _links_from,
    _name_for,
    _normalise,
    _paths_from_description,
    _within,
    _worth_fetching,
    survey,
)

from .fake_api import API_KEY, FakeApi


@pytest.fixture(scope="module")
def api():
    """One server for the module. Starting it per test is the slow part."""
    with FakeApi() as running:
        yield running


@pytest.fixture(scope="module")
def surveyed(api):
    return survey(api.url(), auth=credential({"header": "X-API-Key",
                                              "value": API_KEY}))


class TestScope:
    def test_the_same_path_prefix_is_inside(self):
        assert _within("https://h.test/api/v1/users", "https://h.test/api/v1")

    def test_the_base_itself_is_inside(self):
        assert _within("https://h.test/api/v1", "https://h.test/api/v1")

    def test_a_sibling_prefix_is_outside(self):
        # /api/v10 starts with /api/v1 as a string and is a different API.
        assert not _within("https://h.test/api/v10/users", "https://h.test/api/v1")

    def test_another_host_is_outside(self):
        assert not _within("https://other.test/api/v1/x", "https://h.test/api/v1")

    def test_a_path_above_the_base_is_outside(self):
        assert not _within("https://h.test/blog", "https://h.test/api/v1")


class TestWhatIsWorthFetching:
    def test_a_uri_template_is_not_fetched(self):
        # The GitHub root advertises these. Fetching one asks for a repository
        # belonging to a user called {owner}.
        assert not _worth_fetching("https://api.github.com/repos/{owner}/{repo}")

    def test_an_image_is_not_fetched(self):
        assert not _worth_fetching("https://h.test/logo.png")

    def test_a_relative_path_is_not_fetched(self):
        assert not _worth_fetching("/api/v1/users")

    def test_an_ordinary_endpoint_is(self):
        assert _worth_fetching("https://h.test/api/v1/users")


class TestNormalising:
    def test_a_trailing_slash_is_the_same_resource(self):
        assert _normalise("https://h.test/a/") == _normalise("https://h.test/a")

    def test_a_fragment_is_the_same_resource(self):
        assert _normalise("https://h.test/a#top") == _normalise("https://h.test/a")

    def test_a_different_query_is_a_different_resource(self):
        assert _normalise("https://h.test/s?q=a") != _normalise("https://h.test/s?q=b")


class TestNaming:
    def test_the_base_is_stripped(self):
        assert _name_for("https://h.test/api/v1/users", "https://h.test/api/v1",
                         set()) == "users"

    def test_a_nested_path_joins_its_segments(self):
        assert _name_for("https://h.test/api/v1/private/audit",
                         "https://h.test/api/v1", set()) == "private_audit"

    def test_a_taken_name_is_numbered(self):
        assert _name_for("https://h.test/api/v1/users", "https://h.test/api/v1",
                         {"users"}) == "users_2"

    def test_punctuation_becomes_underscores(self):
        assert _name_for("https://h.test/api/v1/order-items",
                         "https://h.test/api/v1", set()) == "order_items"


class TestRecognisingAnIndex:
    def test_a_map_of_urls_is_an_index(self):
        document = {"users": "https://h.test/api/users",
                    "orders": "https://h.test/api/orders"}
        assert _is_index(document, "https://h.test/api")

    def test_an_index_of_templates_is_still_an_index(self):
        # Reading the GitHub root as one row of 30 string columns is the least
        # useful thing available to do with an API index.
        document = {"repos": "https://h.test/api/repos/{owner}/{repo}",
                    "users": "https://h.test/api/users/{user}"}
        assert _is_index(document, "https://h.test/api")

    def test_a_record_of_strings_is_not_an_index(self):
        document = {"region": "eu-north-1", "window": "sun 02:00"}
        assert not _is_index(document, "https://h.test/api")

    def test_a_map_of_offsite_urls_is_not_an_index(self):
        document = {"docs": "https://elsewhere.test/a",
                    "blog": "https://elsewhere.test/b"}
        assert not _is_index(document, "https://h.test/api")


class TestHarvestingLinks:
    def test_links_in_the_envelope_are_followed(self):
        out = []
        _links_from({"users": "https://h.test/api/users"}, "https://h.test/api", out)
        assert out == ["https://h.test/api/users"]

    def test_links_inside_rows_are_not_followed(self):
        # Twenty rows holding twenty links to twenty individual records turn
        # one table into twenty one-row tables named after it.
        out = []
        _links_from(
            {"results": [{"url": "https://h.test/api/character/1"},
                         {"url": "https://h.test/api/character/2"}]},
            "https://h.test/api", out,
        )
        assert out == []

    def test_pagination_links_are_not_followed(self):
        out = []
        _links_from({"next": "https://h.test/api/users?page=2"},
                    "https://h.test/api", out)
        assert out == []

    def test_hal_hrefs_are_followed(self):
        out = []
        _links_from({"_links": {"self": {"href": "https://h.test/api"},
                                "orders": {"href": "https://h.test/api/orders"}}},
                    "https://h.test/api", out)
        assert out == ["https://h.test/api/orders"]


class TestReadingADescription:
    def test_get_paths_become_urls(self):
        document = {"servers": [{"url": "https://h.test"}],
                    "paths": {"/api/v1/users": {"get": {}}}}
        assert _paths_from_description(document, "https://h.test/api/v1") == [
            "https://h.test/api/v1/users"
        ]

    def test_a_templated_path_is_skipped(self):
        document = {"servers": [{"url": "https://h.test"}],
                    "paths": {"/api/v1/users/{id}": {"get": {}}}}
        assert _paths_from_description(document, "https://h.test/api/v1") == []

    def test_a_path_without_a_get_is_skipped(self):
        document = {"servers": [{"url": "https://h.test"}],
                    "paths": {"/api/v1/users": {"post": {}}}}
        assert _paths_from_description(document, "https://h.test/api/v1") == []

    def test_the_origin_stands_in_for_a_missing_server(self):
        document = {"paths": {"/api/v1/users": {"get": {}}}}
        assert _paths_from_description(document, "https://h.test/api/v1") == [
            "https://h.test/api/v1/users"
        ]


class TestCrawlingASurface:
    def test_every_shape_on_the_surface_is_found(self, surveyed):
        assert {r.name for r in surveyed.resources} == {
            "users", "orders", "metrics", "rates", "regions", "settings",
            "topology", "private_audit",
        }

    def test_each_shape_is_read_the_right_way(self, surveyed):
        read = {r.name: (r.shape.path, r.shape.records) for r in surveyed.resources}
        assert read["users"] == ("results", "array")
        assert read["orders"] == (None, "array")
        assert read["metrics"] == ("hourly", "columns")
        assert read["rates"] == ("rates", "entries")
        assert read["regions"] == (None, "values")
        assert read["settings"] == (None, "single")

    def test_the_rows_survive_the_reading(self, surveyed):
        rows = {r.name: r.rows for r in surveyed.resources}
        assert rows["orders"] == 40
        assert rows["metrics"] == 24
        assert rows["rates"] == 6

    def test_the_index_does_not_become_a_table(self, surveyed):
        # It is a map of eight URLs. Detection would call it one row of eight
        # string columns, which is true and useless.
        assert all(not r.url.rstrip("/").endswith("/api/v1")
                   for r in surveyed.resources)

    def test_individual_records_do_not_become_tables(self, surveyed):
        assert not [r for r in surveyed.resources if r.name.startswith("users_")]

    def test_the_description_document_was_used(self, surveyed):
        assert surveyed.description.endswith("/openapi.json")

    def test_the_crawl_finds_what_the_description_omits(self, surveyed):
        # The fake document lists neither, and both are reachable from the
        # index. A description is authoritative about what it names and silent
        # about the rest.
        by_route = {r.name: r.found_by for r in surveyed.resources}
        assert by_route["metrics"] == "the index"
        assert by_route["settings"] == "the index"


class TestCredentials:
    def test_a_protected_route_is_skipped_with_a_reason(self, api):
        found = survey(api.url())
        assert "private_audit" not in {r.name for r in found.resources}
        refused = [why for url, why in found.skipped.items() if "private" in url]
        assert refused and "401" in refused[0]

    def test_a_credential_opens_it(self, surveyed):
        audit = [r for r in surveyed.resources if r.name == "private_audit"]
        assert audit and audit[0].rows == 2

    def test_a_query_parameter_credential_works_too(self, api):
        found = survey(api.url(),
                       auth=credential({"query": "api_key", "value": API_KEY}))
        assert "private_audit" in {r.name for r in found.resources}


class TestBounds:
    def test_the_request_budget_is_honoured(self, api):
        found = survey(api.url(), max_requests=5)
        assert found.requests <= 5

    def test_the_budget_holds_under_concurrency(self, api):
        # Eight threads that each check the budget and then fetch overshoot it
        # by up to eight. Claiming and counting happen together for this.
        found = survey(api.url(), max_requests=7, concurrency=8)
        assert found.requests <= 7

    def test_nothing_offsite_is_fetched(self, api):
        # The index links to example.test, which is not this API.
        survey(api.url())
        assert all(not h.startswith("http") for h in api.hits)

    def test_probing_for_a_description_does_not_fill_the_skipped_list(self, api):
        found = survey(api.url())
        assert not [u for u in found.skipped if "swagger" in u or "api-docs" in u]


class TestGuessing:
    def test_conventional_names_are_tried_when_nothing_else_worked(self, api):
        # /nowhere has no index and no description, so the only move left is
        # to ask for the names an API like that probably uses.
        found = survey(f"{api.origin}/nowhere", guess=True)
        assert found.resources == []
        assert any("/nowhere/users" in h for h in api.hits)

    def test_guessing_can_be_turned_off(self, api):
        before = len(api.hits)
        survey(f"{api.origin}/quiet", guess=False)
        asked = api.hits[before:]
        assert not [h for h in asked if h.startswith("/quiet/users")]

    def test_a_surface_that_answers_is_never_guessed_at(self, api):
        found = survey(api.url())
        assert all(r.found_by != "a guessed name" for r in found.resources)


class TestConfiguration:
    def test_the_config_it_writes_names_every_table(self, surveyed):
        written = surveyed.as_config()
        assert len(written["tables"]) == len(surveyed.resources)
        assert all("name" in t and "url" in t["http"] for t in written["tables"])

    def test_the_config_is_json(self, surveyed):
        assert json.loads(json.dumps(surveyed.as_config()))

    def test_a_default_reading_is_left_out(self, surveyed):
        orders = [t for t in surveyed.as_config()["tables"]
                  if t["name"] == "orders"][0]["http"]
        assert "records" not in orders and "path" not in orders

    def test_a_reading_that_needs_saying_is_written_down(self, surveyed):
        rates = [t for t in surveyed.as_config()["tables"]
                 if t["name"] == "rates"][0]["http"]
        assert rates["path"] == "rates" and rates["records"] == "entries"


class TestFailures:
    def test_an_unreachable_base_is_reported_rather_than_raised(self):
        found = Crawler("http://127.0.0.1:1/api", timeout=2, guess=False).run()
        assert found.resources == []
        assert found.skipped

    def test_a_response_that_is_not_json_is_skipped(self):
        def html(url, headers, timeout):
            return b"<html><body>hello</body></html>"

        found = Crawler("https://h.test/api", fetcher=html, guess=False).run()
        assert found.resources == []
        assert "not JSON" in next(iter(found.skipped.values()))


class TestPagination:
    def test_a_paginated_collection_is_read_whole(self, api):
        # The fixture serves 200 users ten at a time. A table that stopped at
        # the first page would report 10 and give no sign of the other 190.
        found = survey(api.url())
        users = [r for r in found.resources if r.name == "users"][0]
        assert users.next_key == "next"
        source = HttpSource(name="users", url=users.url, path=users.shape.path,
                            records=users.shape.records, next_key=users.next_key)
        assert len(source.load().rows) == 200

    def test_the_declared_total_is_noticed(self, api):
        found = survey(api.url())
        users = [r for r in found.resources if r.name == "users"][0]
        assert users.total == 200

    def test_a_collection_with_a_next_link_is_not_called_truncated(self, api):
        found = survey(api.url())
        assert not found.truncated

    def test_the_written_config_carries_the_next_key(self, api):
        written = survey(api.url()).as_config()
        users = [t for t in written["tables"] if t["name"] == "users"][0]
        assert users["http"]["next"] == "next"
