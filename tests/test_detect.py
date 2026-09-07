"""How a reading is chosen, and the one rule that decides a difficult case.

The difficult case is a map of same-typed scalars. Frankfurter answers with
29 numbers under USD, GBP and SEK; sunrise-sunset answers with 10 strings
under sunrise, solar_noon and day_length. Both are a map of scalars, so
neither the value types nor the number of keys separates them.
"""

import pytest

from pysqlbridge.source import SourceError, from_records
from pysqlbridge.detect import (
    MIN_KEYS_TO_AGREE,
    _shape_of,
    detect,
    is_rejection,
    keys_identify_rows,
)


class TestKeyShape:
    def test_keys_from_one_generator_share_a_shape(self):
        assert _shape_of("USD") == _shape_of("GBP") == _shape_of("SEK")

    def test_field_names_do_not(self):
        assert _shape_of("sunrise") != _shape_of("solar_noon")

    def test_a_separator_is_part_of_the_shape(self):
        assert _shape_of("day_length") != _shape_of("daylength")

    def test_length_is_part_of_it(self):
        assert _shape_of("ab") != _shape_of("abc")


class TestKeysIdentifyRows:
    def test_currency_codes_are_row_identifiers(self):
        assert keys_identify_rows(["USD", "GBP", "SEK", "NOK", "CHF"])

    def test_numeric_identifiers_are_too(self):
        # Read off the NASA feed, which keys objects by their id.
        assert keys_identify_rows(["157251234", "157251235", "157251236"])

    def test_field_names_are_not(self):
        assert not keys_identify_rows(
            ["sunrise", "sunset", "solar_noon", "day_length"]
        )

    @pytest.mark.parametrize("keys", [
        ["swim", "burrow", "walk"],
        ["width", "height", "depth"],
        ["large", "medium", "thumbnail"],
        ["tvrage", "thetvdb", "imdb"],
        ["query", "scanning", "total"],
        ["id", "iso2code", "value"],
        ["title", "first", "last"],
    ])
    def test_records_found_in_real_responses_are_not_rows(self, keys):
        # Every one of these came out of a corpus of 116 public API responses.
        assert not keys_identify_rows(keys)

    @pytest.mark.parametrize("keys", [
        ["lat", "lng"],
        ["svg", "png"],
        ["name", "desc"],
        ["sha", "url"],
        ["walk", "swim"],
        ["latest", "legacy"],
        ["average", "reviews"],
        ["estimated_diameter_min", "estimated_diameter_max"],
    ])
    def test_two_keys_agreeing_is_not_evidence(self, keys):
        # All eight are records whose two keys happen to share a shape. In
        # that corpus, 11 of the 44 distinct two-key objects agreed by chance
        # and none of them were rows, which is why agreement needs more keys.
        assert not keys_identify_rows(keys)

    def test_the_minimum_is_where_the_evidence_starts(self):
        codes = ["AAA", "BBB", "CCC"]
        assert len(codes) == MIN_KEYS_TO_AGREE
        assert keys_identify_rows(codes)
        assert not keys_identify_rows(codes[:-1])


class TestRejection:
    def test_a_refusal_carrying_errors(self):
        # REST Countries answers a bad field list with HTTP 200 and this.
        assert is_rejection({"status": 400, "message": "bad", "errors": ["x"]})

    def test_a_status_of_its_own(self):
        assert is_rejection({"error": "nope"})

    def test_success_stated_as_false(self):
        assert is_rejection({"success": False, "errors": []})

    def test_an_empty_error_key_is_not_a_refusal(self):
        assert not is_rejection({"errors": [], "data": [1, 2]})

    def test_ordinary_data_is_not(self):
        assert not is_rejection({"data": [{"id": 1}]})

    def test_a_refusal_is_never_read_as_a_table(self):
        assert detect({"status": 400, "errors": [{"a": 1}, {"a": 2}]}) is None


class TestReadings:
    def test_rows_under_a_named_key(self):
        shape = detect({"count": 2, "results": [{"a": 1}, {"a": 2}]})
        assert shape.path == "results" and shape.records == "array"

    def test_a_map_keyed_by_a_code(self):
        shape = detect({"base": "EUR", "rates": {"USD": 1.0, "GBP": 0.9, "SEK": 11.0}})
        assert shape.path == "rates" and shape.records == "entries"

    def test_a_record_of_same_typed_fields(self):
        shape = detect({"sunrise": "6am", "sunset": "6pm", "day_length": "12h"})
        assert shape.records == "single"

    def test_parallel_arrays(self):
        shape = detect({"hourly": {"time": ["a", "b"], "degrees": [1, 2]}})
        assert shape.path == "hourly" and shape.records == "columns"

    def test_nothing_readable_is_refused(self):
        assert detect({"a": {"b": {"c": {"d": {"e": 1}}}}}) is None


class TestNothingCameBack:
    """A search that matched nothing is still a table."""

    def test_a_bare_empty_list_is_read_as_rows(self):
        # GitHub answers /releases with [] for a project that publishes tags.
        # Refusing it makes a source work only on the days it has results.
        shape = detect([])
        assert shape.path is None and shape.records == "array"

    def test_building_it_asks_for_the_columns(self):
        # Which is the useful half: an empty response says nothing about its
        # columns, and this error says exactly that.
        with pytest.raises(SourceError, match="name them with"):
            from_records([], name="t", origin="the API")

    def test_an_empty_list_inside_a_record_is_not_read_as_rows(self):
        # A repository with no topics is the same document as a search with no
        # results, so reading one as rows would serve nothing for the other.
        # The record itself is still the answer here.
        shape = detect({"name": "cpython", "topics": []})
        assert shape.records == "single"

    def test_a_reading_with_evidence_still_wins(self):
        shape = detect({"results": [{"a": 1}, {"a": 2}]})
        assert shape.path == "results"
