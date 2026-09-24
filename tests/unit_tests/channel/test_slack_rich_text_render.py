"""Unit tests for the Markdown-to-``rich_text`` renderer.

The renderer is pure -- Markdown in, element dictionaries out -- so nothing here
touches the connector, a Slack client or the network.

Nothing here has been posted to a real workspace either. Every element and field
name asserted below comes from Slack's Block Kit reference, so these tests fix
what the renderer *builds* and not what Slack accepts; the two are checked
against each other by reading the reference, not by running the suite.
"""

from __future__ import annotations

import pytest

from jiuwenswarm.common import slack_rich_text_render as renderer


def _render(markdown):
    return renderer.render_rich_text(markdown)


def _only(markdown):
    """The single element *markdown* renders to, asserted to be single."""
    elements = _render(markdown)
    assert len(elements) == 1, elements
    return elements[0]


def _texts(element):
    """The words a container shows, concatenated, whatever elements carry them."""
    parts = []
    for child in element["elements"]:
        if child["type"] == "text":
            parts.append(child["text"])
        elif child["type"] == "link":
            parts.append(child.get("text") or child["url"])
        elif child["type"] == "emoji":
            parts.append(f":{child['name']}:")
        else:
            parts.append("")
    return "".join(parts)


def _styled(element, style):
    """Every run inside *element* carrying exactly *style*."""
    return [
        child["text"]
        for child in element["elements"]
        if child["type"] == "text" and child.get("style", {}) == style
    ]


# ---------------------------------------------------------------- empty input


def test_empty_input_renders_no_elements() -> None:
    assert _render("") == []


def test_whitespace_only_input_renders_no_elements() -> None:
    assert _render("   \n\t\n  \n") == []


def test_no_block_is_built_for_input_that_holds_nothing() -> None:
    assert renderer.rich_text_block("") is None
    assert renderer.rich_text_block("  \n ") is None


def test_a_block_wraps_the_elements_it_was_built_from() -> None:
    block = renderer.rich_text_block("hello")
    assert block["type"] == "rich_text"
    assert block["elements"] == _render("hello")
    assert "block_id" not in block


def test_a_block_id_is_carried_when_the_caller_names_one() -> None:
    block = renderer.rich_text_block("hello", block_id="cell-1")
    assert block["block_id"] == "cell-1"


# -------------------------------------------------------------- inline styles


def test_plain_prose_becomes_one_section_with_no_style() -> None:
    section = _only("just some words")
    assert section == {
        "type": "rich_text_section",
        "elements": [{"type": "text", "text": "just some words"}],
    }


@pytest.mark.parametrize(
    ("markdown", "flag"),
    [
        ("**strong**", "bold"),
        ("__strong__", "bold"),
        ("*slanted*", "italic"),
        ("_slanted_", "italic"),
        ("~~gone~~", "strike"),
        ("~gone~", "strike"),
    ],
)
def test_each_emphasis_marker_becomes_a_style_flag(markdown, flag) -> None:
    section = _only(markdown)
    assert section["elements"] == [
        {"type": "text", "text": markdown.strip("*_~"), "style": {flag: True}}
    ]


def test_a_code_span_becomes_the_code_style_flag() -> None:
    section = _only("run `pytest -q` first")
    assert _styled(section, {"code": True}) == ["pytest -q"]


def test_markup_inside_a_code_span_stays_literal() -> None:
    section = _only("`**not bold**`")
    assert _styled(section, {"code": True}) == ["**not bold**"]


def test_nested_emphasis_combines_flags_rather_than_nesting_elements() -> None:
    section = _only("**bold _and italic_**")
    assert section["elements"] == [
        {"type": "text", "text": "bold ", "style": {"bold": True}},
        {
            "type": "text",
            "text": "and italic",
            "style": {"bold": True, "italic": True},
        },
    ]


def test_three_styles_land_on_one_element() -> None:
    section = _only("**_~~all three~~_**")
    assert section["elements"] == [
        {
            "type": "text",
            "text": "all three",
            "style": {"bold": True, "italic": True, "strike": True},
        }
    ]


def test_an_unstyled_element_carries_no_style_object() -> None:
    section = _only("plain")
    assert "style" not in section["elements"][0]


def test_an_underscore_inside_a_word_is_not_emphasis() -> None:
    section = _only("snake_case_name and MAX_SLACK_TEXT_LENGTH")
    assert section["elements"] == [
        {"type": "text", "text": "snake_case_name and MAX_SLACK_TEXT_LENGTH"}
    ]


def test_a_lone_asterisk_between_spaces_is_not_emphasis() -> None:
    section = _only("2 * 3 = 6")
    assert _texts(section) == "2 * 3 = 6"


def test_a_backslash_escapes_the_marker_after_it() -> None:
    section = _only(r"escaped \*not italic\* here")
    assert section["elements"] == [
        {"type": "text", "text": "escaped *not italic* here"}
    ]


def test_adjacent_runs_that_agree_are_merged() -> None:
    # "a", "b" and "c" are three separate runs to the scanner, which steps over
    # the markers between them; one element is what a reader is owed.
    section = _only(r"a\*b\*c")
    assert section["elements"] == [{"type": "text", "text": "a*b*c"}]


# --------------------------------------------------------------------- links


def test_a_markdown_link_becomes_a_link_element() -> None:
    section = _only("see [the docs](https://docs.slack.dev) now")
    assert section["elements"][1] == {
        "type": "link",
        "url": "https://docs.slack.dev",
        "text": "the docs",
    }


def test_a_bare_autolink_carries_no_redundant_text() -> None:
    section = _only("<https://example.com>")
    assert section["elements"] == [{"type": "link", "url": "https://example.com"}]


def test_a_label_equal_to_its_url_is_not_repeated_back() -> None:
    section = _only("[https://example.com](https://example.com)")
    assert section["elements"] == [{"type": "link", "url": "https://example.com"}]


def test_a_url_holding_balanced_parentheses_survives_whole() -> None:
    url = "https://en.wikipedia.org/wiki/Slack_(software)"
    section = _only(f"[Slack]({url})")
    assert section["elements"][0]["url"] == url


def test_a_link_without_a_scheme_stays_characters() -> None:
    section = _only("[here](/docs/index.html)")
    assert section["elements"] == [
        {"type": "text", "text": "[here](/docs/index.html)"}
    ]


def test_a_wholly_bold_label_is_hoisted_onto_the_link() -> None:
    section = _only("[**PR #1**](https://example.com/1)")
    assert section["elements"] == [
        {
            "type": "link",
            "url": "https://example.com/1",
            "text": "PR #1",
            "style": {"bold": True},
        }
    ]


def test_a_partly_styled_label_is_reduced_to_its_words() -> None:
    section = _only("[a **bold** word](https://example.com)")
    assert section["elements"] == [
        {"type": "link", "url": "https://example.com", "text": "a bold word"}
    ]


def test_a_code_span_in_a_label_loses_its_styling() -> None:
    # The link element's style object has no ``code`` flag, so there is nowhere
    # to put it.
    section = _only("[`render_blocks`](https://example.com)")
    assert section["elements"] == [
        {"type": "link", "url": "https://example.com", "text": "render_blocks"}
    ]


def test_emphasis_around_a_link_reaches_the_link_element() -> None:
    section = _only("**see [the docs](https://docs.slack.dev)**")
    assert section["elements"][1] == {
        "type": "link",
        "url": "https://docs.slack.dev",
        "text": "the docs",
        "style": {"bold": True},
    }


def test_two_links_on_one_line_both_survive() -> None:
    section = _only("[one](https://a.example) and [two](https://b.example)")
    urls = [
        child["url"] for child in section["elements"] if child["type"] == "link"
    ]
    assert urls == ["https://a.example", "https://b.example"]


def test_an_image_becomes_a_link_to_itself() -> None:
    section = _only("![a diagram](https://example.com/d.png)")
    assert section["elements"] == [
        {
            "type": "link",
            "url": "https://example.com/d.png",
            "text": "a diagram",
        }
    ]


# --------------------------------------------------------------------- lists


def test_a_bullet_list_becomes_one_list_element() -> None:
    element = _only("- one\n- two\n- three")
    assert element["type"] == "rich_text_list"
    assert element["style"] == renderer.LIST_STYLE_BULLET
    assert [_texts(item) for item in element["elements"]] == ["one", "two", "three"]


@pytest.mark.parametrize("marker", ["-", "+", "*"])
def test_every_bullet_marker_opens_a_list(marker) -> None:
    element = _only(f"{marker} only item")
    assert element["type"] == "rich_text_list"
    assert element["style"] == renderer.LIST_STYLE_BULLET


def test_an_ordered_list_is_styled_ordered() -> None:
    element = _only("1. first\n2. second")
    assert element["style"] == renderer.LIST_STYLE_ORDERED
    assert [_texts(item) for item in element["elements"]] == ["first", "second"]


def test_a_list_starting_at_one_carries_no_offset() -> None:
    assert "offset" not in _only("1. first\n2. second")


def test_a_list_starting_higher_carries_the_offset_slack_documents() -> None:
    # "if the offset = 4, the first number in the ordered list would be 5".
    element = _only("3. third\n4. fourth")
    assert element["offset"] == 2


def test_a_top_level_list_carries_no_indent() -> None:
    assert "indent" not in _only("- one")


def test_nesting_becomes_sibling_lists_carrying_an_indent() -> None:
    # Slack's own nested example is three sibling ``rich_text_list`` elements,
    # the middle one indented -- not a list inside a list.
    elements = _render("- one\n- two\n  - deep\n- three")
    assert [element["type"] for element in elements] == ["rich_text_list"] * 3
    assert "indent" not in elements[0]
    assert elements[1]["indent"] == 1
    assert "indent" not in elements[2]
    assert [_texts(item) for item in elements[1]["elements"]] == ["deep"]


def test_indent_depth_comes_from_order_not_from_width() -> None:
    two = _render("- one\n  - deep")
    four = _render("- one\n    - deep")
    assert two[1]["indent"] == four[1]["indent"] == 1


def test_a_tab_indents_as_far_as_spaces_do() -> None:
    elements = _render("- one\n\t- deep")
    assert elements[1]["indent"] == 1


def test_changing_style_mid_run_starts_a_new_list() -> None:
    elements = _render("- bullet\n1. numbered")
    assert [element["style"] for element in elements] == ["bullet", "ordered"]


def test_a_blank_line_between_items_does_not_end_the_list() -> None:
    element = _only("- one\n\n- two")
    assert [_texts(item) for item in element["elements"]] == ["one", "two"]


def test_inline_style_inside_a_list_item_survives() -> None:
    element = _only("- a **bold** word")
    item = element["elements"][0]
    assert _styled(item, {"bold": True}) == ["bold"]


def test_a_link_inside_a_list_item_survives() -> None:
    element = _only("- see [the docs](https://docs.slack.dev)")
    item = element["elements"][0]
    assert item["elements"][1] == {
        "type": "link",
        "url": "https://docs.slack.dev",
        "text": "the docs",
    }


def test_prose_after_a_list_is_its_own_section() -> None:
    elements = _render("- one\n\nafterwards")
    assert [element["type"] for element in elements] == [
        "rich_text_list",
        "rich_text_section",
    ]
    assert _texts(elements[1]) == "afterwards"


# -------------------------------------------------------------------- quotes


def test_a_quoted_line_becomes_a_quote_element() -> None:
    element = _only("> quoted words")
    assert element["type"] == "rich_text_quote"
    assert element["elements"] == [{"type": "text", "text": "quoted words"}]


def test_consecutive_quote_lines_become_one_quote() -> None:
    element = _only("> first line\n> second line")
    assert _texts(element) == "first line\nsecond line"


def test_a_link_inside_a_quote_survives() -> None:
    element = _only("> see [the docs](https://docs.slack.dev)")
    assert element["elements"][1] == {
        "type": "link",
        "url": "https://docs.slack.dev",
        "text": "the docs",
    }


def test_inline_style_inside_a_quote_survives() -> None:
    element = _only("> a **bold** word")
    assert _styled(element, {"bold": True}) == ["bold"]


def test_prose_after_a_quote_is_its_own_section() -> None:
    elements = _render("> quoted\n\nafterwards")
    assert [element["type"] for element in elements] == [
        "rich_text_quote",
        "rich_text_section",
    ]


# ------------------------------------------------------------- preformatted


def test_a_fence_becomes_a_preformatted_element() -> None:
    element = _only("```\nplain code\n```")
    assert element == {
        "type": "rich_text_preformatted",
        "elements": [{"type": "text", "text": "plain code"}],
    }


def test_a_named_fence_carries_its_language() -> None:
    element = _only("```python\nprint('hi')\n```")
    assert element["language"] == "python"
    assert element["elements"] == [{"type": "text", "text": "print('hi')"}]


def test_markup_inside_a_fence_stays_literal() -> None:
    element = _only("```\n**not bold** and [not a link](https://a.example)\n```")
    assert element["elements"] == [
        {
            "type": "text",
            "text": "**not bold** and [not a link](https://a.example)",
        }
    ]


def test_a_multi_line_fence_keeps_its_newlines() -> None:
    element = _only("```\none\ntwo\n```")
    assert element["elements"][0]["text"] == "one\ntwo"


def test_an_unterminated_fence_still_closes_at_the_end_of_the_input() -> None:
    element = _only("```\nunterminated")
    assert element["type"] == "rich_text_preformatted"
    assert element["elements"][0]["text"] == "unterminated"


def test_an_empty_fence_builds_nothing() -> None:
    assert _render("```\n```") == []


def test_a_tilde_fence_is_a_fence_too() -> None:
    element = _only("~~~\ncode\n~~~")
    assert element["type"] == "rich_text_preformatted"


def test_a_chart_fence_is_code_here_and_not_a_chart() -> None:
    # ``mermaid`` draws a chart in ``render_blocks``. There is no chart element
    # in rich text, so it is the source it was written as.
    element = _only("```mermaid\npie title Votes\n  \"yes\" : 3\n```")
    assert element["type"] == "rich_text_preformatted"
    assert element["language"] == "mermaid"


# ------------------------------------------------------- paragraphs and runs


def test_a_soft_line_break_stays_inside_one_section() -> None:
    section = _only("first line\nsecond line")
    assert section["elements"] == [
        {"type": "text", "text": "first line\nsecond line"}
    ]


def test_a_paragraph_break_is_carried_as_characters() -> None:
    # Nothing documents how two adjacent sections are spaced, so the blank line
    # travels inside one section rather than between two.
    section = _only("para one\n\npara two")
    assert section["elements"] == [
        {"type": "text", "text": "para one\n\npara two"}
    ]


def test_a_longer_run_of_blank_lines_collapses_to_one() -> None:
    section = _only("para one\n\n\n\npara two")
    assert section["elements"][0]["text"] == "para one\n\npara two"


def test_leading_and_trailing_blank_lines_are_dropped() -> None:
    section = _only("\n\n  words  \n\n")
    assert section["elements"] == [{"type": "text", "text": "  words  "}]


def test_styles_survive_across_a_paragraph_break() -> None:
    section = _only("**one**\n\n**two**")
    assert _styled(section, {"bold": True}) == ["one", "two"]


# ------------------------------------------------ mentions, emoji, broadcasts


def test_a_user_token_becomes_a_user_element() -> None:
    section = _only("thanks <@U01ABCDEF>")
    assert section["elements"][1] == {"type": "user", "user_id": "U01ABCDEF"}


def test_a_user_tokens_stale_display_name_is_dropped() -> None:
    section = _only("<@U01ABCDEF|alice>")
    assert section["elements"] == [{"type": "user", "user_id": "U01ABCDEF"}]


def test_a_channel_token_becomes_a_channel_element() -> None:
    section = _only("in <#C01ABCDEF|general>")
    assert section["elements"][1] == {
        "type": "channel",
        "channel_id": "C01ABCDEF",
    }


def test_a_usergroup_token_becomes_a_usergroup_element() -> None:
    section = _only("<!subteam^S01ABCDEF|@ops>")
    assert section["elements"] == [
        {"type": "usergroup", "usergroup_id": "S01ABCDEF"}
    ]


@pytest.mark.parametrize(
    ("token", "range_"),
    [("here", "here"), ("channel", "channel"), ("everyone", "everyone")],
)
def test_a_broadcast_token_becomes_a_broadcast_element(token, range_) -> None:
    section = _only(f"<!{token}>")
    assert section["elements"] == [{"type": "broadcast", "range": range_}]


def test_the_legacy_group_broadcast_is_mapped_onto_everyone() -> None:
    section = _only("<!group>")
    assert section["elements"] == [{"type": "broadcast", "range": "everyone"}]


def test_emphasis_around_a_mention_reaches_the_mention() -> None:
    section = _only("**<@U01ABCDEF>**")
    assert section["elements"] == [
        {"type": "user", "user_id": "U01ABCDEF", "style": {"bold": True}}
    ]


def test_a_shortcode_becomes_an_emoji_element() -> None:
    # A shortcode written into a text element renders as literal colons, so the
    # element form is the only one that shows an emoji.
    section = _only("shipped :tada:")
    assert section["elements"][1] == {"type": "emoji", "name": "tada"}


def test_a_skin_toned_shortcode_keeps_its_modifier() -> None:
    section = _only(":wave::skin-tone-2:")
    assert section["elements"] == [
        {"type": "emoji", "name": "wave::skin-tone-2"}
    ]


def test_a_colon_after_a_word_character_does_not_open_a_shortcode() -> None:
    section = _only("at 10:30:45 see http://example.com/a")
    assert section["elements"][0]["text"].startswith("at 10:30:45 see ")


# --------------------------------------------------- deliberately not covered


def test_a_markdown_table_reaches_the_reader_as_its_pipes() -> None:
    table = "| Name | Count |\n| --- | --- |\n| alpha | 1 |"
    section = _only(table)
    assert section["type"] == "rich_text_section"
    assert _texts(section) == table


def test_a_heading_loses_its_level_and_renders_bold() -> None:
    small = _only("###### six")
    large = _only("# one")
    assert small["elements"] == [
        {"type": "text", "text": "six", "style": {"bold": True}}
    ]
    assert large["elements"] == [
        {"type": "text", "text": "one", "style": {"bold": True}}
    ]


def test_a_thematic_break_reaches_the_reader_as_characters() -> None:
    section = _only("above\n\n---\n\nbelow")
    assert _texts(section) == "above\n\n---\n\nbelow"


def test_an_indented_code_block_is_not_preformatted() -> None:
    section = _only("    indented = True")
    assert section["type"] == "rich_text_section"
    assert _texts(section) == "    indented = True"


def test_a_nested_quote_keeps_only_its_outer_marker() -> None:
    element = _only("> > twice quoted")
    assert element["type"] == "rich_text_quote"
    assert _texts(element) == "> twice quoted"


def test_a_continuation_line_is_not_folded_into_the_item_above_it() -> None:
    elements = _render("- one\n  still one")
    assert [element["type"] for element in elements] == [
        "rich_text_list",
        "rich_text_section",
    ]
    assert _texts(elements[1]) == "  still one"


def test_a_task_list_checkbox_is_text_inside_a_bullet() -> None:
    element = _only("- [ ] not done")
    assert element["type"] == "rich_text_list"
    assert _texts(element["elements"][0]) == "[ ] not done"


def test_a_mention_written_as_a_name_is_not_resolved() -> None:
    section = _only("ask @alice in #general")
    assert section["elements"] == [
        {"type": "text", "text": "ask @alice in #general"}
    ]


def test_html_reaches_the_reader_as_characters() -> None:
    section = _only("<b>not bold</b>")
    assert _texts(section) == "<b>not bold</b>"


def test_no_undocumented_style_flag_is_ever_emitted() -> None:
    # Markdown says nothing that means highlight, underline or unlink, so a
    # payload built here never claims one.
    markdown = "**b** *i* ~~s~~ `c` [l](https://a.example) <@U01ABCDEF> :tada:"
    flags = set()
    for element in _render(markdown):
        for child in element["elements"]:
            flags.update(child.get("style", {}))
    assert flags == {"bold", "italic", "strike", "code"}


def test_no_border_field_is_ever_emitted() -> None:
    markdown = "> quoted\n\n- one\n\n```\ncode\n```"
    assert all("border" not in element for element in _render(markdown))


# ---------------------------------------------------------------- everything


def test_one_document_holding_every_supported_construct() -> None:
    markdown = "\n".join(
        [
            "# Report",
            "",
            "A **bold** claim with a [link](https://example.com).",
            "",
            "- first",
            "- second with `code`",
            "  - nested",
            "",
            "1. step one",
            "2. step two",
            "",
            "> quoted, with [a link](https://example.com) inside",
            "",
            "```python",
            "print('done')",
            "```",
        ]
    )
    assert [element["type"] for element in _render(markdown)] == [
        "rich_text_section",
        "rich_text_section",
        "rich_text_list",
        "rich_text_list",
        "rich_text_list",
        "rich_text_quote",
        "rich_text_preformatted",
    ]
