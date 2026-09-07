import codecs

import pytest

from pysqlbridge.detect import detect
from pysqlbridge.markup import parse_html, parse_xml, sniff, without_bom
from pysqlbridge.source import SourceError

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Example</title>
  <item><title>First</title><link>https://e.test/1</link></item>
  <item><title>Second</title><link>https://e.test/2</link></item>
  <item><title>Third</title><link>https://e.test/3</link></item>
</channel></rss>"""

PAGE = b"""<!DOCTYPE html><html><body>
<table><tr><td>a layout table</td></tr></table>
<table><caption>Sales by region</caption>
  <thead><tr><th>Region</th><th>Q1</th></tr></thead>
  <tbody><tr><td>North</td><td>120</td></tr><tr><td>South</td><td>90</td></tr></tbody>
</table>
<script type="application/ld+json">{"@type":"Organization","name":"Example"}</script>
</body></html>"""


class TestByteOrderMark:
    """Microsoft tooling writes one, and it is invisible to whoever reads it."""

    def test_a_mark_does_not_hide_xml(self):
        # The Federal Reserve press feed serves one. Without this the feed is
        # sniffed as JSON and then fails to parse as JSON.
        assert sniff(codecs.BOM_UTF8 + RSS) == "xml"

    def test_a_mark_does_not_hide_json(self):
        assert sniff(codecs.BOM_UTF8 + b'{"a": 1}') == "json"

    def test_a_mark_does_not_hide_html(self):
        assert sniff(codecs.BOM_UTF8 + PAGE) == "html"

    def test_stripping_one_leaves_everything_else_alone(self):
        assert without_bom(b'{"a": 1}') == b'{"a": 1}'
        assert without_bom(codecs.BOM_UTF8 + b"{}") == b"{}"

    def test_a_marked_feed_still_becomes_rows(self):
        document = parse_xml(codecs.BOM_UTF8 + RSS)
        assert len(document["rss"]["channel"]["item"]) == 3


class TestXml:
    def test_an_element_with_only_text_becomes_that_text(self):
        assert parse_xml(b"<a><b>hello</b></a>") == {"a": {"b": "hello"}}

    def test_an_empty_element_is_null(self):
        assert parse_xml(b"<a><b/></a>") == {"a": {"b": None}}

    def test_attributes_become_prefixed_keys(self):
        # <link href=".."> and <link><href>..</href> would otherwise collide
        # into one column meaning two things.
        assert parse_xml(b'<a><b id="1"/></a>') == {"a": {"b": {"@id": "1"}}}

    def test_a_repeated_tag_becomes_a_list(self):
        assert parse_xml(b"<a><b>1</b><b>2</b></a>") == {"a": {"b": ["1", "2"]}}

    def test_a_single_occurrence_is_not_a_list(self):
        assert parse_xml(b"<a><b>1</b></a>") == {"a": {"b": "1"}}

    def test_namespaces_are_stripped(self):
        document = parse_xml(
            b'<feed xmlns="http://www.w3.org/2005/Atom"><title>T</title></feed>'
        )
        assert document == {"feed": {"title": "T"}}

    def test_text_alongside_children_is_kept_under_its_own_key(self):
        assert parse_xml(b"<a>text<b>1</b></a>") == {"a": {"#text": "text", "b": "1"}}

    def test_a_doctype_is_refused(self):
        # A DTD can define entities that expand a small document into
        # gigabytes inside the parser.
        with pytest.raises(SourceError, match="DOCTYPE"):
            parse_xml(b'<!DOCTYPE r [<!ENTITY a "aaa">]><r>&a;</r>')

    def test_broken_xml_says_so(self):
        with pytest.raises(SourceError, match="not valid XML"):
            parse_xml(b"<a><b></a>")

    def test_a_feed_detects_straight_through_to_its_items(self):
        shape = detect(parse_xml(RSS))
        assert shape.path == "rss.channel.item"
        assert shape.records == "array"


class TestHtml:
    def test_a_table_becomes_records(self):
        found = parse_html(PAGE)
        assert found["sales_by_region"] == [
            {"region": "North", "q1": "120"},
            {"region": "South", "q1": "90"},
        ]

    def test_a_caption_names_the_table(self):
        assert "sales_by_region" in parse_html(PAGE)

    def test_a_table_without_a_caption_is_named_by_position(self):
        page = b"<html><table><tr><th>a</th></tr><tr><td>1</td></tr></table></html>"
        assert list(parse_html(page)) == ["table_1"]

    def test_a_one_row_table_is_dropped(self):
        # Pages are full of these used for layout. Serving them as empty
        # tables buries the real ones.
        assert "table_1" not in parse_html(PAGE)

    def test_embedded_json_is_kept(self):
        assert parse_html(PAGE)["json_ld"] == {"@type": "Organization",
                                               "name": "Example"}

    def test_a_page_with_neither_says_so(self):
        with pytest.raises(SourceError, match="no table and no published JSON"):
            parse_html(b"<html><body><p>just prose</p></body></html>")

    def test_colspan_repeats_a_value_across_what_it_covers(self):
        page = (b"<table><tr><th>a</th><th>b</th><th>c</th></tr>"
                b'<tr><td colspan="2">wide</td><td>3</td></tr></table>')
        assert parse_html(page)["table_1"] == [{"a": "wide", "b": "wide", "c": "3"}]

    def test_a_style_block_inside_a_cell_is_ignored(self):
        # Wikipedia puts one in the first cell of its larger tables. Left in,
        # the stylesheet becomes the column name.
        page = (b"<table><tr><th><style>.x{color:red}</style>Name</th></tr>"
                b"<tr><td>ada</td></tr></table>")
        assert parse_html(page)["table_1"] == [{"name": "ada"}]

    def test_the_widest_header_row_wins(self):
        # A two-level header puts the columns that matter above a sub-header
        # sitting under a colspan.
        page = (b"<table>"
                b'<tr><th>rank</th><th>name</th><th colspan="2">height</th></tr>'
                b"<tr><th>m</th><th>ft</th></tr>"
                b"<tr><td>1</td><td>Burj</td><td>828</td><td>2717</td></tr>"
                b"</table>")
        assert list(parse_html(page)["table_1"][0]) == [
            "rank", "name", "height", "height_4"
        ]

    def test_a_spanning_title_is_not_the_header(self):
        # A colspan repeats its value, so a one-cell title becomes the widest
        # row in the table while still saying only one thing.
        page = (b'<table><tr><th colspan="3">A Big Title</th></tr>'
                b"<tr><th>z</th><th>sym</th><th>element</th></tr>"
                b"<tr><td>1</td><td>H</td><td>Hydrogen</td></tr></table>")
        assert list(parse_html(page)["table_1"][0]) == ["z", "sym", "element"]

    def test_a_nested_table_does_not_become_its_own_table(self):
        page = (b"<table><tr><th>outer</th></tr>"
                b"<tr><td><table><tr><td>inner</td></tr></table></td></tr></table>")
        assert list(parse_html(page)) == ["table_1"]

    def test_an_unnamed_column_still_gets_a_name(self):
        page = b"<table><tr><th></th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table>"
        assert list(parse_html(page)["table_1"][0]) == ["column_1", "b"]

    def test_a_short_row_pads_with_nulls_rather_than_shifting(self):
        page = (b"<table><tr><th>a</th><th>b</th></tr>"
                b"<tr><td>1</td></tr></table>")
        assert parse_html(page)["table_1"] == [{"a": "1", "b": None}]


class TestSniffing:
    def test_an_object_is_json(self):
        assert sniff(b'  {"a": 1}') == "json"

    def test_an_array_is_json(self):
        assert sniff(b"[1, 2]") == "json"

    def test_a_declaration_is_xml(self):
        assert sniff(b'<?xml version="1.0"?><a/>') == "xml"

    def test_a_doctype_html_is_html(self):
        assert sniff(b"<!DOCTYPE html><html>") == "html"

    def test_an_html_tag_is_html(self):
        assert sniff(b"<html lang='en'>") == "html"

    def test_a_bare_element_is_xml(self):
        assert sniff(b"<rows><row/></rows>") == "xml"
