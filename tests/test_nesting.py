"""A nested array becomes a table of its own, keyed back to its parent.

A row cannot hold a list, and over 116 public API responses a third of the
tables built from them had one in every row. Serving those as JSON text made
a column nobody can query; dropping them lost the data. The relational answer
is a second table, which is also the one a join can use.
"""


from pysqlbridge.catalog import Catalog
from pysqlbridge.http_source import HttpSource
from pysqlbridge.source import (
    MAX_CHILD_TABLES,
    MAX_NESTING,
    child_tables,
    identifying_column,
)

CHARACTERS = [
    {"id": 1, "name": "rick", "episode": ["e1", "e2"]},
    {"id": 2, "name": "morty", "episode": ["e1"]},
]

CARTS = [
    {"id": 7, "products": [
        {"id": 99, "sku": "a", "reviews": [{"stars": 5}, {"stars": 4}]},
        {"id": 98, "sku": "b", "reviews": []},
    ]},
]


def built(parent, rows):
    return child_tables(parent, rows, identifying_column(rows))


class TestFindingTheKey:
    def test_a_column_that_never_repeats_is_the_key(self):
        assert identifying_column([{"id": 1}, {"id": 2}]) == "id"

    def test_a_column_that_repeats_is_not(self):
        assert identifying_column([{"id": 1}, {"id": 1}]) is None

    def test_a_column_with_a_gap_is_not(self):
        assert identifying_column([{"id": 1}, {"id": None}]) is None

    def test_the_name_does_not_decide_it(self):
        # A column called id that repeats is not a key, and one called slug
        # that does not repeat is.
        rows = [{"id": 1, "slug": "a"}, {"id": 1, "slug": "b"}]
        assert identifying_column(rows) == "slug"

    def test_the_first_such_column_wins(self):
        rows = [{"a": 1, "b": 10}, {"a": 2, "b": 20}]
        assert identifying_column(rows) == "a"


class TestScalarArrays:
    def test_one_row_per_element(self):
        table = built("characters", CHARACTERS)["characters_episode"]
        assert len(table.rows) == 3

    def test_the_element_and_its_position_are_columns(self):
        table = built("characters", CHARACTERS)["characters_episode"]
        assert table.column_names == ["characters_id", "episode_index", "value"]

    def test_the_parent_key_is_beside_every_element(self):
        table = built("characters", CHARACTERS)["characters_episode"]
        assert table.rows == [[1, 0, "e1"], [1, 1, "e2"], [2, 0, "e1"]]

    def test_the_position_column_is_named_after_its_array(self):
        # Two levels each need a position, and calling both of them position
        # would leave the deeper one unable to say where it sat.
        tables = built("carts", CARTS)
        assert "products_index" in tables["carts_products"].column_names
        assert "reviews_index" in tables["carts_products_reviews"].column_names


class TestObjectArrays:
    def test_the_element_fields_become_columns(self):
        table = built("carts", CARTS)["carts_products"]
        assert "sku" in table.column_names

    def test_the_carried_key_is_named_for_its_parent(self):
        # An element usually has an id of its own. Writing both as id would
        # lose the parent and make the obvious join join the wrong thing.
        table = built("carts", CARTS)["carts_products"]
        assert "carts_id" in table.column_names and "id" in table.column_names
        assert table.rows[0][0] == 7 and table.rows[0][2] == 99

    def test_an_empty_array_contributes_nothing(self):
        table = built("carts", CARTS)["carts_products_reviews"]
        assert len(table.rows) == 2      # the second product had none


class TestNestingInsideNesting:
    def test_an_array_inside_a_child_becomes_another_table(self):
        assert "carts_products_reviews" in built("carts", CARTS)

    def test_a_grandchild_carries_every_level_above_it(self):
        table = built("carts", CARTS)["carts_products_reviews"]
        assert table.column_names == [
            "carts_id", "products_index", "reviews_index", "stars"
        ]
        assert table.rows == [[7, 0, 0, 5], [7, 0, 1, 4]]

    def test_a_table_does_not_also_carry_what_it_expanded(self):
        # The reviews are a table now, so leaving them here as JSON would be
        # the same data twice and the wide copy is not queryable.
        assert "reviews" not in built("carts", CARTS)["carts_products"].column_names

    def test_nesting_is_bounded(self):
        rows = [{"id": 1, "a": [{"b": [{"c": [{"d": [{"e": [{"f": 1}]}]}]}]}]}]
        names = built("t", rows)
        assert all(name.count("_") <= MAX_NESTING for name in names)

    def test_the_number_of_tables_is_bounded(self):
        rows = [{"id": 1, **{f"a{n}": [{"v": n}] for n in range(200)}}]
        assert len(built("t", rows)) <= MAX_CHILD_TABLES


class TestThroughTheCatalog:
    def fetcher(self, payload):
        import json as _json

        def fetch(url, headers, timeout):
            return _json.dumps(payload).encode()

        return fetch

    def test_a_child_appears_in_the_table_list(self):
        source = HttpSource(name="characters", url="https://h.test",
                            fetcher=self.fetcher(CHARACTERS))
        catalog = Catalog()
        catalog.add_source(source)
        catalog.load_all()
        assert "characters_episode" in catalog.names

    def test_a_child_can_be_joined_back_to_its_parent(self):
        source = HttpSource(name="characters", url="https://h.test",
                            fetcher=self.fetcher(CHARACTERS))
        catalog = Catalog()
        catalog.add_source(source)
        catalog.load_all()
        result = catalog.answer(
            "SELECT c.name, COUNT(*) AS n FROM characters c "
            "JOIN characters_episode e ON e.characters_id = c.id "
            "GROUP BY c.name ORDER BY n DESC, c.name"
        )
        assert result.rows == [["rick", 2], ["morty", 1]]

    def test_a_name_a_person_gave_is_not_overwritten(self):
        from pysqlbridge.source import from_records

        source = HttpSource(name="characters", url="https://h.test",
                            fetcher=self.fetcher(CHARACTERS))
        catalog = Catalog()
        catalog.add(from_records([{"mine": 1}], name="characters_episode"))
        catalog.add_source(source)
        catalog.load_all()
        assert catalog.get("characters_episode").column_names == ["mine"]

    def test_expansion_can_be_turned_off(self):
        source = HttpSource(name="characters", url="https://h.test",
                            expand=False, fetcher=self.fetcher(CHARACTERS))
        catalog = Catalog()
        catalog.add_source(source)
        catalog.load_all()
        assert catalog.names == ["characters"]
        # And the array stays where it was, as text.
        assert "episode" in catalog.get("characters").column_names
