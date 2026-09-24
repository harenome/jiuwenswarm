# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Render Markdown into Slack's ``rich_text`` element tree.

The third of the three shapes this repository can put Slack-facing text into,
and the only one some surfaces will take. :mod:`jiuwenswarm.common.slack_text`
produces **mrkdwn**, a string that carries its formatting as markup (``*bold*``).
:mod:`jiuwenswarm.common.slack_blocks` produces **Block Kit**, whose prose is
``section`` blocks holding an mrkdwn string. ``rich_text`` is neither: formatting
is a typed tree, ``{"type": "text", "text": "bold", "style": {"bold": true}}``,
with ``rich_text_section``, ``rich_text_list``, ``rich_text_quote`` and
``rich_text_preformatted`` as the four containers a ``rich_text`` block may hold.

A converter is needed rather than wanted because Slack refuses the other two on
at least one surface. A Slack List cell is always rich text, and the reference
says so outright: *"While a bit counterintuitive, you must use rich_text blocks
in a request that includes plain text… You may see the text property appear in a
response as a fallback, but it is not accepted in the request payload."* Text
that has already been reduced to mrkdwn cannot be promoted back, because the
markup and the words are the same characters by then.

``slack_blocks`` already builds ``rich_text``, in ``_rich_text_elements``, and
that builder was deliberately not widened into this one. It exists so a link
inside a *table cell* keeps its destination, and it reduces bold and code inside
a cell to their words on purpose -- every other cell in the same table is
``raw_text``, which renders characters and no markup, so a cell that suddenly
rendered bold would be the odd one out in its own grid. Widening it would change
table rendering as a side effect of a change that is not about tables.

Markdown is the input language, not mrkdwn, and the distinction is load-bearing
in one place: ``*x*`` is italic here and bold in mrkdwn. A caller must therefore
hand this function the author's Markdown and *not* run
``normalize_slack_mrkdwn`` first, or every ``**bold**`` arrives as ``*bold*`` and
renders italic.

Slack's angle-bracket tokens are recognised anyway, on top of Markdown. They have
no Markdown spelling, they arrive verbatim in text written for Slack and in text
read back out of it, and left alone they would reach a reader as the literal
characters ``<@U01ABCDEF>``. No identifier is resolved to produce one: a token
already carries its own id, and translating ``<@U01ABCDEF>`` into a ``user``
element is a change of shape rather than a lookup. A mention written as a *name*
-- ``@alice``, ``#general`` -- stays text, because turning it into an element
would need a directory this function must not consult.

Nothing here declines. ``render_blocks`` returns ``None`` when Block Kit is not
possible, because a table over Slack's character budget has no truthful partial
rendering; this renderer has no such case. Every construct it does not support
degrades to the characters it was written as, which is what a reader would have
seen from a plain-text post, so there is no state in which returning nothing
would tell the caller something a list of elements does not. Empty or
whitespace-only input is the one empty result, and it means the input was empty.

What is **not** covered, so that a caller's tool description can promise exactly
what arrives:

* **Tables.** ``rich_text`` has no table container of any kind. A Markdown table
  reaches the reader as its pipes. ``render_blocks`` is where a table becomes a
  ``table`` or ``data_table`` block, and that path is unaffected by this one.
* **Headings.** There is no heading element. A heading renders as a section whose
  text is bold, which is the same substitution ``normalize_markdown_line``
  already makes for mrkdwn. The level is lost: ``#`` and ``######`` render alike.
* **Images.** There is no image element. ``![alt](url)`` renders as a link to the
  image, labelled with its alt text.
* **Thematic breaks.** ``---`` and ``***`` reach the reader as characters.
* **Indented code blocks.** Only a fenced block becomes ``rich_text_preformatted``.
  Four-space-indented code keeps its spaces inside a section.
* **Nested block quotes.** One level. The second ``>`` is characters.
* **Multi-paragraph list items.** An item is the one line it was written on. A
  continuation line under it becomes prose after the list rather than part of it.
* **Task-list checkboxes.** ``- [ ]`` is a bullet whose text begins ``[ ]``.
* **Mentions by name.** See above -- no directory is consulted.
* **Styling inside a link label**, except where the *whole* label carries one of
  bold, italic or strike, which is hoisted onto the link's own ``style``. A
  partly styled label is reduced to its words, and a code span in a label always
  is: the ``link`` element's ``style`` object has no ``code`` flag.
* **Relative links.** ``[here](/docs)`` has no destination Slack can open, so it
  stays characters. A link needs a scheme.
* **Elements with no Markdown source at all**, which are emitted never rather
  than badly: ``date``, ``color``, ``file``, ``citation``, ``canvas``,
  ``canvas_user_mention``, ``canvas_message_unfurl``, ``list_record``, ``team``,
  ``tag``, ``attachment_mention``, ``message_mention``, ``work_object_mention``,
  ``workflow_mention`` and ``salesforce_data_field``. The same goes for the
  ``border`` field, the link element's ``unsafe``, ``from_llm``, ``is_slack_url``
  and ``truncated`` flags, and the ``highlight``, ``client_highlight``,
  ``underline`` and ``unlink`` style flags: Markdown says nothing that means any
  of them.
* **Chart and Block Kit fences.** ``mermaid``, ``vega-lite`` and ``blockkit``
  are code here. Only ``render_blocks`` draws them, and a chart is not a
  ``rich_text`` element.

Two places the reference is silent, and what was done instead of guessing:

* **Spacing between adjacent containers.** Nothing documents whether two
  neighbouring ``rich_text_section`` elements are drawn with a blank line between
  them. A paragraph break is therefore carried as characters -- a run of
  paragraphs becomes *one* section with ``"\\n\\n"`` between them -- so the gap
  does not depend on a client behaviour nobody wrote down.
* **Size limits.** Slack publishes no character, element or nesting ceiling for
  ``rich_text``, so none is enforced here. The 50-blocks-per-message limit is the
  caller's, and is already named in ``slack_blocks``.

Field and element names are taken from Slack's reference: the ``rich_text``
block page and the element pages beneath it. **Nothing in this module has been
posted to a real workspace.** One detail decides a behaviour and is worth
naming: ``code`` appears among the ``style`` object's boolean properties on the
text element and not on ``link``, which is why a code span in a link label is
reduced rather than styled.

Pure by construction, and in ``common`` for the same reason its two neighbours
are: the connector package's ``__init__`` pulls in ``slack_bolt`` and the whole
channel manager, the agent runtime has neither, and both sides have to be able to
reach a renderer. Nothing here imports outside the standard library except the
two fence patterns, which are shared with ``slack_text`` so that a fence of four
backticks cannot be read two ways by two modules.

It is a new module rather than an addition to ``slack_rich_text``, whose name is
the closest fit and whose direction is the opposite one: that module *reads* text
out of inbound payloads. A writer beside it would leave one module doing both
directions under a name that says neither.
"""

from __future__ import annotations

import re
from typing import Any

from jiuwenswarm.common.slack_text import (
    MARKDOWN_FENCE_CLOSE_RE,
    MARKDOWN_FENCE_OPEN_RE,
)

# The two ``rich_text_list`` styles, spelled as Slack spells them: *"Either
# bullet or ordered, the latter meaning a numbered list."*
LIST_STYLE_BULLET = "bullet"
LIST_STYLE_ORDERED = "ordered"

# The ``style`` flags this renderer ever sets on a ``text`` element, which is a
# strict subset of what the element accepts -- see the module docstring for the
# four it leaves alone and why.
TEXT_STYLE_FLAGS = ("bold", "italic", "strike", "code")
# What a ``link`` element accepts of those. ``code`` is absent from the link
# element's reference page, so a code span in a label loses its styling rather
# than being sent as a flag Slack does not document.
LINK_STYLE_FLAGS = ("bold", "italic", "strike")

# How wide a tab is when working out which level of a nested list an item sits
# at. Markdown itself does not say, and the choice only has to be consistent:
# what matters is the *ordering* of two items' indents, not their absolute width.
TAB_WIDTH = 4

# Broadcast ranges Slack names: *"value can be here, channel, or everyone"*.
# ``<!group>`` is the legacy spelling of ``<!everyone>`` and is mapped onto it
# rather than sent through, because ``range`` is a closed set.
BROADCAST_RANGES = {
    "here": "here",
    "channel": "channel",
    "everyone": "everyone",
    "group": "everyone",
}

_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(?P<text>.*?)[ \t]*#*[ \t]*$")
_QUOTE_RE = re.compile(r"^[ \t]{0,3}>[ \t]?(?P<body>.*)$")
_BULLET_ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)[-+*][ \t]+(?P<body>.*)$")
_ORDERED_ITEM_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<number>\d{1,9})[.)][ \t]+(?P<body>.*)$"
)

# Inline scanning. Each of these is matched *anchored at a position*, never
# searched for, so that the scanner always consumes the earliest construct and
# a later one cannot reach back past text already taken.
_CODE_SPAN_RE = re.compile(r"(?P<ticks>`+)(?P<body>.+?)(?P=ticks)", re.DOTALL)
_BOLD_STAR_RE = re.compile(r"\*\*(?=\S)(?P<body>.+?)(?<=\S)\*\*", re.DOTALL)
_BOLD_UNDER_RE = re.compile(r"__(?=\S)(?P<body>.+?)(?<=\S)__(?![0-9A-Za-z_])")
_ITALIC_STAR_RE = re.compile(r"\*(?=\S)(?P<body>[^*\n]+?)(?<=\S)\*")
_ITALIC_UNDER_RE = re.compile(r"_(?=\S)(?P<body>[^_\n]+?)(?<=\S)_(?![0-9A-Za-z_])")
_STRIKE_DOUBLE_RE = re.compile(r"~~(?=\S)(?P<body>.+?)(?<=\S)~~", re.DOTALL)
_STRIKE_SINGLE_RE = re.compile(r"~(?=\S)(?P<body>[^~\n]+?)(?<=\S)~")

# ``:shortcode:``, which a ``text`` element renders as literal colons -- the
# reference is explicit that nothing substitutes an emoji into the tree, so the
# only way to show one is an ``emoji`` element beside the text.
_EMOJI_RE = re.compile(r":(?P<name>[a-z0-9][a-z0-9_+\-]*(?:::skin-tone-[2-6])?):")

# Slack's angle-bracket tokens. Ordered so that the mention forms are tried
# before the bare autolink, which would otherwise swallow ``<@U01ABCDEF>`` as a
# url with no scheme.
_USER_TOKEN_RE = re.compile(r"<@(?P<id>[UW][A-Z0-9]+)(?:\|[^>]*)?>")
_CHANNEL_TOKEN_RE = re.compile(r"<#(?P<id>[C][A-Z0-9]+)(?:\|[^>]*)?>")
_USERGROUP_TOKEN_RE = re.compile(r"<!subteam\^(?P<id>[S][A-Z0-9]+)(?:\|[^>]*)?>")
_BROADCAST_TOKEN_RE = re.compile(
    r"<!(?P<range>here|channel|everyone|group)(?:\|[^>]*)?>"
)
_AUTOLINK_RE = re.compile(r"<(?P<url>[a-zA-Z][a-zA-Z0-9+.\-]*:[^<>\s|]+)>")

# A url needs a scheme to be a destination. Without one Slack has nothing to
# open, so the source stays on screen as the characters somebody typed.
_URL_SCHEME_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*:")
# Markdown's backslash escape, which covers ASCII punctuation and nothing else.
_ESCAPABLE = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")


def render_rich_text(markdown: str) -> list[dict[str, Any]]:
    """The ``elements`` array of a ``rich_text`` block, rendered from *markdown*.

    The array rather than the block, because the two callers that want this want
    different wrappers around the same array: a message wraps it in a
    ``rich_text`` block, and a Slack List cell or a table cell wraps it in a
    ``rich_text`` *cell*, which is the same two keys and is not a block at all.
    ``rich_text_block`` is the wrapper for the first of those.

    Empty for input that is empty or nothing but whitespace, and never empty for
    input that holds a character a reader would see. An unsupported construct is
    rendered as its own source text rather than dropped -- see the module
    docstring for the full list of what that covers.
    """
    if not markdown or not markdown.strip():
        return []

    lines = markdown.splitlines()
    elements: list[dict[str, Any]] = []
    prose: list[str] = []
    index = 0

    def flush_prose() -> None:
        section = _section(prose, {})
        prose.clear()
        if section is not None:
            elements.append(section)

    while index < len(lines):
        line = lines[index]

        fence = MARKDOWN_FENCE_OPEN_RE.match(line)
        if fence:
            flush_prose()
            block, index = _preformatted(lines, index, fence)
            if block is not None:
                elements.append(block)
            continue

        if _QUOTE_RE.match(line):
            flush_prose()
            block, index = _quote(lines, index)
            if block is not None:
                elements.append(block)
            continue

        if _list_item(line) is not None:
            flush_prose()
            blocks, index = _lists(lines, index)
            elements.extend(blocks)
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            flush_prose()
            # No heading element exists, so the level is dropped and the text is
            # set bold -- the same substitution the mrkdwn normaliser makes.
            section = _section([heading.group("text")], {"bold": True})
            if section is not None:
                elements.append(section)
            index += 1
            continue

        prose.append(line)
        index += 1

    flush_prose()
    return elements


def rich_text_block(
    markdown: str, *, block_id: str | None = None
) -> dict[str, Any] | None:
    """*markdown* as one ``rich_text`` block, or ``None`` when it holds nothing.

    ``None`` carries the meaning it carries in ``render_blocks``: there is
    nothing here worth sending, post the message however you would have. It is
    returned for empty input alone; a block with an empty ``elements`` array
    would be a block that draws nothing while still counting against the
    caller's fifty.
    """
    elements = render_rich_text(markdown)
    if not elements:
        return None
    block: dict[str, Any] = {"type": "rich_text", "elements": elements}
    if block_id:
        block["block_id"] = block_id
    return block


def _section(lines: list[str], style: dict[str, bool]) -> dict[str, Any] | None:
    """One ``rich_text_section`` from a run of prose lines, or ``None`` if blank.

    Blank lines are the reason this takes a run rather than a line. Nothing
    documents how two adjacent sections are spaced, so a paragraph break is
    carried inside the section as a newline rather than left to the client: the
    run is trimmed at both ends, any stretch of blank lines is collapsed to one,
    and every line boundary contributes a ``"\\n"`` that merges with its
    neighbours into the ``"\\n\\n"`` a paragraph break looks like.
    """
    trimmed = _collapse_blanks(lines)
    if not trimmed:
        return None
    elements: list[dict[str, Any]] = []
    for position, line in enumerate(trimmed):
        if position:
            elements.append({"type": "text", "text": "\n"})
        elements.extend(_inline(line, style))
    merged = _merge(elements)
    if not merged:
        return None
    return {"type": "rich_text_section", "elements": merged}


def _collapse_blanks(lines: list[str]) -> list[str]:
    """Drop the outer blank lines and reduce any inner run of them to one."""
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    collapsed: list[str] = []
    for line in lines[start:end]:
        if not line.strip():
            if collapsed and not collapsed[-1]:
                continue
            collapsed.append("")
            continue
        collapsed.append(line)
    return collapsed


def _quote(lines: list[str], start: int) -> tuple[dict[str, Any] | None, int]:
    """A ``rich_text_quote`` from the run of ``>`` lines beginning at *start*.

    Only the first ``>`` of a line is the quote marker. A second one is the
    characters it was written as, because ``rich_text_quote`` has no nesting and
    a quote inside a quote would otherwise be indistinguishable from the outer
    one.
    """
    body: list[str] = []
    index = start
    while index < len(lines):
        match = _QUOTE_RE.match(lines[index])
        if not match:
            break
        body.append(match.group("body"))
        index += 1
    section = _section(body, {})
    if section is None:
        return None, index
    return {"type": "rich_text_quote", "elements": section["elements"]}, index


def _preformatted(
    lines: list[str], start: int, fence: "re.Match[str]"
) -> tuple[dict[str, Any] | None, int]:
    """A ``rich_text_preformatted`` from the fenced block opening at *start*.

    The body is characters and is not parsed. A fence is where an author puts
    text they want shown exactly as written, so a ``*`` in it is an asterisk and
    a ``[label](url)`` in it is six words and some punctuation.

    An unterminated fence still closes at the end of the input rather than
    reverting to prose, which is the reading that keeps the author's intent: the
    alternative shows a code block as a paragraph with its opening backticks
    still on screen.
    """
    marker = fence.group("fence")
    info = fence.group("info").strip()
    body: list[str] = []
    index = start + 1
    while index < len(lines):
        closing = MARKDOWN_FENCE_CLOSE_RE.match(lines[index])
        if closing:
            found = closing.group("fence")
            if found[0] == marker[0] and len(found) >= len(marker):
                index += 1
                break
        body.append(lines[index])
        index += 1

    text = "\n".join(body)
    if not text:
        # An empty fence holds nothing to show, and ``elements`` is required, so
        # there is no block to build rather than a block holding an empty string.
        return None, index
    block: dict[str, Any] = {
        "type": "rich_text_preformatted",
        "elements": [{"type": "text", "text": text}],
    }
    language = info.split()[0].lower() if info else ""
    if language:
        block["language"] = language
    return block, index


def _list_item(line: str) -> tuple[int, str, int, str] | None:
    """``(indent, style, number, body)`` if *line* opens a list item, else None.

    ``number`` is meaningless for a bullet and is the written number for an
    ordered item, which is what an ``offset`` is worked out from.
    """
    ordered = _ORDERED_ITEM_RE.match(line)
    if ordered:
        return (
            _indent_width(ordered.group("indent")),
            LIST_STYLE_ORDERED,
            int(ordered.group("number")),
            ordered.group("body"),
        )
    bullet = _BULLET_ITEM_RE.match(line)
    if bullet:
        return (
            _indent_width(bullet.group("indent")),
            LIST_STYLE_BULLET,
            1,
            bullet.group("body"),
        )
    return None


def _indent_width(indent: str) -> int:
    return len(indent.replace("\t", " " * TAB_WIDTH))


def _lists(lines: list[str], start: int) -> tuple[list[dict[str, Any]], int]:
    """Every ``rich_text_list`` the run of items at *start* produces.

    Nesting is flat, which is Slack's own shape rather than a simplification:
    the reference builds a nested list from *three sibling* ``rich_text_list``
    elements, the middle one carrying ``indent: 1``. So a change of depth or of
    style ends one list and opens the next, and the depth travels in ``indent``.

    Depths come from the order of the indents seen, not from their width. Two
    spaces and four spaces are both "one level in", and which one an author used
    is not something a reader should be able to tell.
    """
    items: list[tuple[int, str, int, str]] = []
    index = start
    while index < len(lines):
        item = _list_item(lines[index])
        if item is not None:
            items.append(item)
            index += 1
            continue
        if not lines[index].strip():
            # A blank line between two items is still one list. It ends the list
            # only when what follows is not another item.
            lookahead = index + 1
            while lookahead < len(lines) and not lines[lookahead].strip():
                lookahead += 1
            if lookahead < len(lines) and _list_item(lines[lookahead]) is not None:
                index = lookahead
                continue
        break

    widths: list[int] = []
    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    current_key: tuple[str, int] | None = None
    for width, style, number, body in items:
        while widths and width < widths[-1]:
            widths.pop()
        if not widths or width > widths[-1]:
            widths.append(width)
        depth = len(widths) - 1
        section = _section([body], {}) or {
            "type": "rich_text_section",
            # An item written as a bare marker has no text, and ``elements`` is
            # required, so it becomes an empty line rather than disappearing and
            # renumbering everything under it. Unprobed: nothing documents
            # whether Slack takes a ``text`` element holding an empty string,
            # and no payload was posted to find out.
            "elements": [{"type": "text", "text": ""}],
        }
        if current_key != (style, depth):
            current = {"type": "rich_text_list", "style": style, "elements": []}
            if depth:
                current["indent"] = depth
            if style == LIST_STYLE_ORDERED and number > 1:
                # *"if the offset = 4, the first number in the ordered list would
                # be 5"* -- so an author's "3." is an offset of two.
                current["offset"] = number - 1
            blocks.append(current)
            current_key = (style, depth)
        assert current is not None
        current["elements"].append(section)
    return blocks, index


def _inline(text: str, style: dict[str, bool]) -> list[dict[str, Any]]:
    """The elements one line of *text* holds, every one of them carrying *style*.

    A single left-to-right scan, taking whichever construct starts at the current
    position and falling through to one more plain character when none does. The
    ordering matters only where two constructs can open at the same character: a
    code span wins over emphasis, because its contents are literal, and Slack's
    mention tokens win over an autolink, which would otherwise read a mention as
    a url.

    Emphasis recurses with the flag added rather than wrapping an element in
    another element. Slack's ``style`` object takes several flags at once, so
    ``**bold _and italic_**`` is one ``text`` element with both set, and there is
    no nesting available to express it any other way.
    """
    elements: list[dict[str, Any]] = []
    buffer: list[str] = []
    position = 0
    length = len(text)

    def flush() -> None:
        if buffer:
            elements.append(_text("".join(buffer), style))
            buffer.clear()

    while position < length:
        character = text[position]

        escaped = position + 1 < length and text[position + 1] in _ESCAPABLE
        if character == "\\" and escaped:
            buffer.append(text[position + 1])
            position += 2
            continue

        if character == "`":
            code = _CODE_SPAN_RE.match(text, position)
            if code:
                flush()
                elements.append(_text(code.group("body"), {**style, "code": True}))
                position = code.end()
                continue

        if character in "![":
            link = _markdown_link(text, position, style)
            if link is not None:
                element, end = link
                flush()
                elements.append(element)
                position = end
                continue

        if character == "<":
            token = _angle_token(text, position, style)
            if token is not None:
                element, end = token
                flush()
                elements.append(element)
                position = end
                continue

        if character == ":" and _emoji_boundary(text, position):
            emoji = _EMOJI_RE.match(text, position)
            if emoji:
                flush()
                elements.append({"type": "emoji", "name": emoji.group("name")})
                position = emoji.end()
                continue

        emphasis = _emphasis(text, position, style)
        if emphasis is not None:
            inner, end = emphasis
            flush()
            elements.extend(inner)
            position = end
            continue

        buffer.append(character)
        position += 1

    flush()
    return _merge(elements)


def _text(value: str, style: dict[str, bool]) -> dict[str, Any]:
    """One ``text`` element, with ``style`` present only when a flag is set.

    An empty ``style`` object is omitted rather than sent. It says nothing Slack
    does not already assume, and leaving it out keeps a payload comparable with
    the one Slack's own composer produces for the same words.
    """
    element: dict[str, Any] = {"type": "text", "text": value}
    flags = {name: True for name in TEXT_STYLE_FLAGS if style.get(name)}
    if flags:
        element["style"] = flags
    return element


def _emphasis(
    text: str, position: int, style: dict[str, bool]
) -> tuple[list[dict[str, Any]], int] | None:
    """Emphasis opening at *position*, parsed with its flag added, or ``None``.

    The two-character markers are tried before the one-character ones so that
    ``**x**`` is bold rather than an italic ``*x*`` with stray asterisks around
    it. ``_`` and ``__`` additionally have to sit on a word boundary, or
    ``snake_case_name`` would render its middle word in italics.
    """
    character = text[position]
    if character == "*":
        candidates = ((_BOLD_STAR_RE, "bold"), (_ITALIC_STAR_RE, "italic"))
    elif character == "_":
        if position and (text[position - 1].isalnum() or text[position - 1] == "_"):
            return None
        candidates = ((_BOLD_UNDER_RE, "bold"), (_ITALIC_UNDER_RE, "italic"))
    elif character == "~":
        candidates = ((_STRIKE_DOUBLE_RE, "strike"), (_STRIKE_SINGLE_RE, "strike"))
    else:
        return None

    for pattern, flag in candidates:
        match = pattern.match(text, position)
        if match:
            return _inline(match.group("body"), {**style, flag: True}), match.end()
    return None


def _emoji_boundary(text: str, position: int) -> bool:
    """Whether a ``:`` at *position* may open a shortcode.

    A shortcode never follows a word character. Requiring that is what keeps
    ``http://example.com`` and ``10:30:45`` out of the emoji element: both hold a
    colon, and both hold it directly after a letter or a digit.
    """
    if position == 0:
        return True
    previous = text[position - 1]
    return not (previous.isalnum() or previous == "_")


def _markdown_link(
    text: str, position: int, style: dict[str, bool]
) -> tuple[dict[str, Any], int] | None:
    """``[label](url)`` or ``![alt](url)`` at *position*, or ``None``.

    Hand-scanned rather than matched, for the reason ``convert_markdown_links``
    gives: a url may hold balanced parentheses, and the depth counter below is
    what tracks them.

    An image becomes a link to itself. There is no image element in
    ``rich_text``, and a link to the file is the only rendering that keeps the
    destination a reader would otherwise lose entirely.
    """
    start = position
    if text[start] == "!":
        start += 1
    if start >= len(text) or text[start] != "[":
        return None

    depth = 0
    cursor = start + 1
    label_end = -1
    while cursor < len(text):
        if text[cursor] == "\\":
            cursor += 2
            continue
        if text[cursor] == "[":
            depth += 1
        elif text[cursor] == "]":
            if depth == 0:
                label_end = cursor
                break
            depth -= 1
        cursor += 1
    if label_end < 0 or label_end + 1 >= len(text) or text[label_end + 1] != "(":
        return None

    depth = 0
    cursor = label_end + 2
    url_end = -1
    while cursor < len(text):
        character = text[cursor]
        if character.isspace():
            return None
        if character == "(":
            depth += 1
        elif character == ")":
            if depth == 0:
                url_end = cursor
                break
            depth -= 1
        cursor += 1
    if url_end < 0:
        return None

    url = text[label_end + 2 : url_end]
    if not _URL_SCHEME_RE.match(url):
        return None
    label = text[start + 1 : label_end]
    return _link(url, label, style), url_end + 1


def _link(url: str, label: str, style: dict[str, bool]) -> dict[str, Any]:
    """A ``link`` element, with ``text`` present only when it says something new.

    Slack shows the url itself when a link carries no text, which is what a bare
    autolink already meant, so the field is omitted rather than repeating the url
    back at it. This is the convention ``slack_blocks._rich_text_elements``
    already settled for a link inside a table cell.

    A label's own markup is reduced to its words, because ``text`` is a string
    and cannot hold a tree. The one thing recovered is a style covering the
    *whole* label, which is hoisted onto the link -- ``[**PR #1**](url)`` is the
    common case, and the link element carries bold, italic and strike itself.
    """
    label_text, label_style = _label(label)
    element: dict[str, Any] = {"type": "link", "url": url}
    if label_text and label_text != url:
        element["text"] = label_text
    flags = {**style, **label_style}
    emitted = {name: True for name in LINK_STYLE_FLAGS if flags.get(name)}
    if emitted:
        element["style"] = emitted
    return element


def _label(label: str) -> tuple[str, dict[str, bool]]:
    """A link label's words, and the style every one of them shares, if any.

    A mixed label yields no style rather than the style of its first run: half a
    label in bold is not something the element can say, and picking one of the
    two halves to believe would be a guess about which one mattered.
    """
    elements = _inline(label, {})
    words: list[str] = []
    styles: list[frozenset[str]] = []
    for element in elements:
        if element["type"] == "text":
            words.append(element["text"])
        elif element["type"] == "emoji":
            # No emoji element fits inside a string field, so the shortcode goes
            # back the way it was written.
            words.append(f":{element['name']}:")
            styles.append(frozenset())
            continue
        else:
            # A mention inside a label has no textual form that would still
            # mention anybody, so the label is taken as its words alone.
            return "".join(words).strip(), {}
        styles.append(frozenset(element.get("style", {})))
    text = "".join(words).strip()
    shared = set(styles[0]) if styles else set()
    for flags in styles[1:]:
        shared &= flags
    return text, {name: True for name in shared if name in LINK_STYLE_FLAGS}


def _angle_token(
    text: str, position: int, style: dict[str, bool]
) -> tuple[dict[str, Any], int] | None:
    """One of Slack's angle-bracket tokens, or a Markdown autolink, or ``None``.

    Every one of these carries its own identifier, so none of them is a lookup.
    ``<@U01ABCDEF|alice>`` holds a display name as well, and it is dropped: the
    ``user`` element has no field for it, and Slack draws the current name from
    the id rather than from whatever the name was when the text was written.
    """
    user = _USER_TOKEN_RE.match(text, position)
    if user:
        return _styled({"type": "user", "user_id": user.group("id")}, style), user.end()
    channel = _CHANNEL_TOKEN_RE.match(text, position)
    if channel:
        element = {"type": "channel", "channel_id": channel.group("id")}
        return _styled(element, style), channel.end()
    usergroup = _USERGROUP_TOKEN_RE.match(text, position)
    if usergroup:
        element = {"type": "usergroup", "usergroup_id": usergroup.group("id")}
        return _styled(element, style), usergroup.end()
    broadcast = _BROADCAST_TOKEN_RE.match(text, position)
    if broadcast:
        element = {
            "type": "broadcast",
            "range": BROADCAST_RANGES[broadcast.group("range")],
        }
        return _styled(element, style), broadcast.end()
    autolink = _AUTOLINK_RE.match(text, position)
    if autolink:
        url = autolink.group("url")
        return _link(url, "", style), autolink.end()
    return None


def _styled(element: dict[str, Any], style: dict[str, bool]) -> dict[str, Any]:
    """Carry the surrounding emphasis onto a mention element.

    Mention elements take the same ``style`` object a link does, minus ``code``,
    so bold around a mention survives instead of stopping at it.
    """
    flags = {name: True for name in LINK_STYLE_FLAGS if style.get(name)}
    if flags:
        element["style"] = flags
    return element


def _merge(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join neighbouring ``text`` elements that agree, and drop the empty ones.

    The scanner emits a run per construct it steps over, so ``a*b*c`` with the
    emphasis stripped would otherwise arrive as three elements saying the same
    thing as one. Merging keeps a payload close to what Slack's own composer
    writes, which is what anybody comparing the two will have in front of them.
    """
    merged: list[dict[str, Any]] = []
    for element in elements:
        if element["type"] != "text":
            merged.append(element)
            continue
        if not element["text"]:
            continue
        if (
            merged
            and merged[-1]["type"] == "text"
            and merged[-1].get("style") == element.get("style")
        ):
            merged[-1]["text"] += element["text"]
            continue
        merged.append(element)
    return merged


__all__ = [
    "BROADCAST_RANGES",
    "LINK_STYLE_FLAGS",
    "LIST_STYLE_BULLET",
    "LIST_STYLE_ORDERED",
    "TAB_WIDTH",
    "TEXT_STYLE_FLAGS",
    "render_rich_text",
    "rich_text_block",
]
