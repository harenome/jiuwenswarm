"""Render Slack mrkdwn containing Markdown tables and charts into Block Kit.

Scoped deliberately to what Slack mrkdwn cannot express: a table, a chart, and
Block Kit an author wrote by hand. Headings, bold, bullets, links and code
fences all render correctly as mrkdwn already, so they stay mrkdwn here too --
prose becomes ``section`` blocks whose text is passed through untouched.

The input is therefore expected to be **already-normalised mrkdwn**, not raw
Markdown. Ordering is what reuses the connector's normaliser rather than a
parallel implementation of it: the caller normalises first, and a Markdown table
survives that pass intact -- no rule in it matches a line beginning with ``|``.

Pure by construction. Nothing here imports the Slack SDK or the connector, so the
renderer can be tested, reviewed and lifted on its own; a caller supplies text and
receives a list of plain dictionaries, or ``None``.

That purity is why the module sits in ``common`` rather than beside the
connector, and the position is load-bearing rather than tidy. The connector
package's ``__init__`` imports ``slack_connect``, which imports ``slack_bolt``,
so importing anything from that package at all pulls the Slack Bolt runtime and
the whole channel manager in. The agent runtime is a separate process that has
neither, and the posting tools there render exactly what an ordinary reply
renders. Reachable from both sides, the renderer has to sit where neither side's
dependencies are.

``None`` is the whole fallback protocol, and it is all-or-nothing per message.
Blocks cannot be chunked the way the connector chunks text -- there is no
per-message splitter for them -- so a message that will not fit inside Block
Kit's limits is reported as unrenderable and the caller posts it as plain text.
Truncating instead would drop content the user was told had been delivered.

A table is emitted as one of two block types, and which one is the operator's
choice rather than the table's size: ``render_tables`` names it outright.
``basic`` emits a plain ``table``, every row of it visible at once; ``data_table``
emits the block that pages, sorts, filters and offers a download in the client;
``off`` emits neither and leaves the pipes on screen as the text they were
written as.

Size does not decide it. The two blocks differ in what they can do as well as
in how much they hold, so a row count could pick between them only by accident:
an operator who wants sorting wants it for the tables worth sorting, which is
not the same set as the tables that are long. ``data_table``'s own
``page_size`` default of 5 would turn a twelve-row table into five rows and a
pager, and ``DATA_TABLE_PAGE_SIZE`` and ``_page_size`` hold that off directly --
Slack's default is never sent, and neither is a page smaller than the table it
holds.

Limits are Slack's. The ``section`` and block-count limits are verified against
slack_sdk 3.43.0's block models; the ``data_table`` limits are not, because the
SDK does not model that block at all and, on five separate occasions, validated
payloads Slack then refused. They were established by posting to a real channel:

* 50 blocks per message (``docs.slack.dev/block-kit``).
* 3,000 characters per ``section`` text (``SectionBlock.text_max_length``).
* A ``table`` block holds at most 100 rows of at most 20 cells
  (``TableBlock.__init__`` docstring), and one message's table cells may total at
  most 10,000 characters (table-block reference).
* A ``data_table`` holds at most 201 rows (1 header and 200 data) of 1 to 20
  cells, needs a ``caption``, and one message's ``data_table`` cells may total at
  most 20,000 characters.

The raised budget is not reachable on every path. The connector splits a reply
into chunks before it offers any of them here, and the first chunk of a
*streamed* reply is cut at the 4,000 characters ``chat.update`` accepts rather
than the 38,000 a fresh post does. A table cut at that boundary loses its
delimiter row, ``_parse_table`` declines the half that no longer has one, and it
reaches the channel as raw pipes. So 20,000 characters is the budget on a fresh
post and a table over roughly 4,000 still degrades on the opening chunk of a
streamed one. Only splitting text after rendering rather than before it would
close that, and the splitter belongs to the connector.

Cells are ``raw_text``, which renders characters and no markup, except where a
cell contains a link: those are ``rich_text``, because a ``raw_text`` cell can
only show a link's label and the destination would be lost. Both types are
allowed -- *"Table cells can have a type of rich_text, raw_text, or raw_number"*
(table-block reference; ``TableBlock.__init__`` in slack_sdk 3.43.0 says the
same). This is not a general mrkdwn-to-rich_text parser and is not meant to
become one: bold and code inside a cell are still reduced to their words.

The third cell type is what makes a ``data_table`` column sort as numbers rather
than as strings, and it is all-or-nothing per column -- see ``_numeric_columns``.

Beside the Markdown tables it translates, this module renders what an author
fenced deliberately. Five fence languages decide that, and nothing else does:

* ``mermaid`` -- a diagram, rendered wherever this connector can draw it.
* ``vega-lite`` -- a chart specification, rendered however this connector can.
* ``blockkit`` -- Block Kit JSON written by hand, rendered as the blocks it holds.
* ``slack-raw`` -- never rendered, shown as the source it holds.
* an unnamed fence -- never rendered, shown as the source it holds.

Naming the language is the whole of the request. There is no marker to add
beside it, no second word in the info string, and no reply-level switch that
turns a fence on: a fence that names one of the three rendering languages
renders, and every other fence is source.

That splits the tiers by portability rather than by ceremony. ``mermaid`` and
``vega-lite`` are the portable ones -- a renderer that has never heard of Slack
still knows both languages, and one that cannot draw the chart still shows a
description of it, labels and values in near-prose. ``blockkit`` is Slack-only
and explicit, is taken at the author's risk, and covers what neither of the
other two can say.

``vega-lite`` overlaps ``blockkit``, and deliberately. They are different layers
rather than competitors: a ``blockkit`` fence says "I know this is Slack, draw
exactly this", and a ``vega-lite`` fence says "here is a chart specification,
render it however you can". Only the second travels to a connector that has
never heard of Block Kit, which is why both exist.

``slack-raw`` is the escape hatch, and it exists because rendering is now the
default: an author showing a reader what a diagram or a block looks like *in
source* has to have a way to say so. A bare fence says the same thing, and is
what source arrives in already.

Slack's ``data_visualization`` draws four chart types, not the three its own
summary sentence names: ``pie``, ``bar``, ``area`` and ``line``. All four were
confirmed accepted by posting them to a real channel, ``line`` included. Four
sources now reach them:

========================  =========================================
mermaid ``pie``           ``pie``
mermaid ``xychart-beta``  ``bar`` and ``line``
``vega-lite``             ``bar``, ``area``, ``line`` and ``pie``
``blockkit``              all four, written out by hand
========================  =========================================

``area`` is the one no mermaid diagram reaches: mermaid has no area chart of any
kind, so it comes from a ``vega-lite`` or ``blockkit`` fence or not at all.

Axis titles go inside ``axis_config`` beside ``categories``, and nowhere else.
Written as siblings of ``series`` they do not merely go unread -- Slack refuses
the whole chart with *"failed to match exactly one allowed schema"* and the
message never arrives. Both spellings were posted to a real channel to settle
it, because the two disagreed in this repository.

``xychart-beta`` is beta and its syntax has moved between mermaid releases
twice already; both moves are handled -- see ``_XYCHART_OPEN_RE`` and
``_XYCHART_PLOT_RE``, which name the versions. The risk of supporting it is
bounded by the one rule every parser here follows: **an unrecognised shape is
declined, never guessed at**. A future mermaid that changes the syntax produces
a fence that falls through to source rather than a wrong chart, with the name
itself on screen in the reply as the warning.

Declining costs the fence and never the message. The fence stays where it was
written and becomes prose, so a table or a second chart beside it renders exactly
as it would have; only a breach of a *message-level* limit -- a third chart, too
many blocks, too many table characters -- is all-or-nothing, because only that
one is a property of the whole message rather than of one fence.

Every decline by a rendering language is logged at INFO, and a fence that named
no rendering language is not: the two are the same ``None`` internally and mean
opposite things -- see ``_fenced_blocks``.

Neither portable fence is subject to the ``blockkit`` allow-list. That list is
about hand-written Block Kit, and these two emit a chart this module built
itself from a language that is not Slack's.

Which block types a ``blockkit`` fence may hold is the operator's to decide --
see ``DEFAULT_ALLOWED_BLOCK_TYPES``, which restricts nothing until it is
configured. What a reader is asked to click is not: interactive elements are
refused by a separate check that defaults to refusing them, whatever the type
allow-list says -- see ``INTERACTIVE_TYPES``.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Collection
from typing import Any

# Declining never costs the reader the content, but it is invisible from
# outside: the fence reaches the channel as source and looks exactly like source
# an author meant to write, leaving nothing for an operator to read when a chart
# was not drawn. This is the only side effect in the module.
logger = logging.getLogger(__name__)

# Slack's published ceilings. Named rather than inlined so a limit breach in a
# test reads as the limit it broke.
MAX_BLOCKS_PER_MESSAGE = 50
# A published view -- an App Home tab -- is held to a different ceiling from a
# message, and Slack's App Home reference states it as "Max of 100 blocks". Kept
# beside the message ceiling rather than in the tool that publishes, so the two
# numbers are read together and neither can be mistaken for the other.
MAX_BLOCKS_PER_VIEW = 100
MAX_SECTION_TEXT_LENGTH = 3000
# ``data_table``'s ceilings. The row count includes the header row, which is
# ``rows[0]``: Slack accepts 201 rows in all, and 200 is set one short of it
# deliberately rather than matched to it exactly.
MAX_TABLE_ROWS = 200
MAX_TABLE_COLUMNS = 20
MAX_TABLE_CHARACTERS_PER_MESSAGE = 20000
# ``table``'s ceilings, which are half of those and still bind every block that
# is still emitted as a plain ``table``. Counted separately rather than folded
# into the pair above because one message can hold both kinds and Slack holds
# each kind to its own budget: a message of short tables totalling 15,000
# characters is over the ``table`` budget even though it is under the other one.
MAX_PLAIN_TABLE_ROWS = 100
MAX_PLAIN_TABLE_CHARACTERS_PER_MESSAGE = 10000

# What ``channels.slack.render_tables`` accepts, and what each value emits for a
# Markdown table found in a reply.
#   "off"        -- no table block at all. The lines stay the mrkdwn pipes they
#                   arrived as, which is what an unparseable table also falls
#                   back to.
#   "basic"      -- a plain ``table``: every row at once, no controls, and half
#                   the row and character budget of the other one.
#   "data_table" -- the block that pages, sorts, filters and downloads.
# The names are the block types Slack calls them by, minus "off", so what is
# configured and what is sent are one word.
RENDER_TABLES_OFF = "off"
RENDER_TABLES_BASIC = "basic"
RENDER_TABLES_DATA = "data_table"
RENDER_TABLES_MODES = (RENDER_TABLES_OFF, RENDER_TABLES_BASIC, RENDER_TABLES_DATA)
# ``data_table`` is the default because it is the only value that cannot cost a
# reply its rendering: it holds 200 rows and 20,000 characters where ``basic``
# holds 100 and 10,000, and a table over those limits declines the whole message
# back to raw pipes rather than being truncated. Its one readability objection,
# Slack paging five rows at a time, is answered by ``_page_size``.
RENDER_TABLES_DEFAULT = RENDER_TABLES_DATA

# How many rows one page of a ``data_table`` shows. Never Slack's default of 5,
# which would page a twelve-row table three times over; this is how many rows
# this module is willing to put on one page, and a table shorter than it is
# shown whole with no pager at all -- see ``_page_size``.
DATA_TABLE_PAGE_SIZE = 20
# Slack's own bounds on the field, so a page size raised past them is clamped
# rather than sent and refused.
MIN_DATA_TABLE_PAGE_SIZE = 1
MAX_DATA_TABLE_PAGE_SIZE = 100

# ``caption`` is required and has no Markdown source, so one is taken from the
# heading the table sits under and this is what stands in when there is none.
DEFAULT_TABLE_CAPTION = "Table"
# Unprobed: Slack documents no ceiling for ``caption`` and none was established.
# Clamped to the tightest plain-text ceiling elsewhere in Block Kit -- a button's
# 75 characters -- because the two failures are not comparable. A caption cut
# short loses a few words of a label; a caption Slack refuses costs the whole
# message its formatting.
MAX_TABLE_CAPTION_LENGTH = 75

# Which block types a hand-written fence may hold, and the first of the two
# independent checks it is put through.
#
# Empty means no restriction, and that is the default: which block types Slack
# accepts is Slack's to say, the set changes without this module hearing about
# it, and a name missing from a hard-coded list would cost an author the whole
# message's rendering for a block Slack would have drawn. An operator who wants
# a narrower surface configures one.
#
# This is *not* where a forged approval prompt is stopped. Type names answer a
# portability question -- which blocks this workspace's clients draw well. The
# safety question is below, and is answered on its own.
DEFAULT_ALLOWED_BLOCK_TYPES: frozenset[str] = frozenset()

# The second check, which fails independently of the first and defaults to
# refusing rather than to allowing.
#
# This connector posts real permission-approval buttons into the same channels
# it posts replies into. A reader cannot tell a button an author wrote from a
# button the approval flow wrote -- they are the same pixels -- so a block that
# posts back to the app is refused by default however wide the type allow-list
# is. Widening the block-type list must not also widen which elements a reader
# can be asked to click.
#
# Two ways in, because either alone leaves a hole. ``action_id`` catches a field
# smuggled into a block that otherwise looks inert, including a block type that
# grows one in some later Slack release. The type list catches an element that
# holds no ``action_id`` at all -- Slack generates one for a button that omits
# it, and the interaction is delivered either way.
INTERACTIVE_FIELD = "action_id"
INTERACTIVE_TYPES = frozenset(
    {
        # Blocks that exist to be interacted with.
        "actions",
        "input",
        # Elements that post an interaction back to the app.
        "button",
        "workflow_button",
        "checkboxes",
        "radio_buttons",
        "overflow",
        "datepicker",
        "datetimepicker",
        "timepicker",
        "file_input",
        "email_text_input",
        "url_text_input",
        "number_input",
        "plain_text_input",
        "rich_text_input",
        "static_select",
        "external_select",
        "users_select",
        "conversations_select",
        "channels_select",
        "multi_static_select",
        "multi_external_select",
        "multi_users_select",
        "multi_conversations_select",
        "multi_channels_select",
    }
)
# Refusing is the default. An operator who has a use for hand-written
# interactive blocks turns them on knowingly, having read why they are off.
DEFAULT_ALLOW_INTERACTIVE_BLOCKS = False
# Enforced by the API, so enforcing it here turns a rejected message into a
# legible text one instead.
MAX_DATA_VISUALIZATIONS_PER_MESSAGE = 2

# The whole fence vocabulary. A fence's language is the entire request: these
# two render, and every other fence -- ``slack-raw``, ``json``, ``python`` or no
# language at all -- is source and is left where the author wrote it.
#
# ``mermaid`` renders unasked because a mermaid fence in a reply is a diagram the
# author wanted drawn far more often than it is mermaid syntax they wanted
# quoted, and because the language is portable: the same fence is a diagram in
# every other renderer that reads the reply.
#
# ``blockkit`` names the payload rather than the channel. Block Kit is Slack's
# format, so the language already says which connector can do anything with the
# fence, and another connector reading the tag knows to skip it rather than
# guess at it.
#
# ``vega-lite`` names a chart specification rather than a channel, exactly as
# ``mermaid`` does. It overlaps ``blockkit`` on Slack and is the only one of the
# two that means anything anywhere else.
MERMAID_FENCE_LANGUAGE = "mermaid"
VEGA_LITE_FENCE_LANGUAGE = "vega-lite"
BLOCK_KIT_FENCE_LANGUAGE = "blockkit"
# Not a renderer: the escape hatch that says "show this as written". Named for
# the channel because that is what it is for -- an author demonstrating what
# this connector would otherwise have drawn.
RAW_FENCE_LANGUAGE = "slack-raw"

# What a rendering was built from, kept alongside the blocks so the connector
# can say something useful when Slack refuses them.
#
# The kind is recorded where the branch is taken -- here, in the segmenter that
# chose a renderer for a fence, and at the connector's own typed builders --
# rather than read back off the finished payload. Reading it back would be a
# guess: a ``section`` block looks identical whether this module wrote it around
# a paragraph or an author pasted it into a ```blockkit fence, and the whole
# point of the distinction is that those two failures are shown differently.
#
# ``interactive`` outranks the rest because it is the one kind whose payload
# must never be shown to the reader: an approval button's ``value`` is this
# connector's internals, and a control that did not draw is a broken function
# rather than a formatting loss. ``fence`` outranks ``data`` because a fence is
# the author's own text and they are the only person who can repair it.
BLOCK_KIND_UNKNOWN = "unknown"
BLOCK_KIND_CHROME = "chrome"
BLOCK_KIND_DATA = "data"
BLOCK_KIND_FENCE = "fence"
BLOCK_KIND_INTERACTIVE = "interactive"
# Most specific first; ``combine_block_kinds`` returns the first one it sees.
BLOCK_KIND_PRECEDENCE: tuple[str, ...] = (
    BLOCK_KIND_INTERACTIVE,
    BLOCK_KIND_FENCE,
    BLOCK_KIND_DATA,
    BLOCK_KIND_CHROME,
    BLOCK_KIND_UNKNOWN,
)


def combine_block_kinds(kinds: Collection[str]) -> str:
    """The kind one message is treated as, given everything it was built from.

    One Slack message holds one payload and Slack refuses it whole, so a chunk
    holding a table *and* a hand-written fence has to be shown one way. The
    precedence is by how much damage the wrong treatment does rather than by how
    much of the message each kind contributed: dumping an approval button's JSON
    at a reader is worse than withholding a table's rows, so a single
    interactive element decides for the whole message.
    """
    present = {kind for kind in kinds if kind}
    for kind in BLOCK_KIND_PRECEDENCE:
        if kind in present:
            return kind
    return BLOCK_KIND_UNKNOWN

# ``data_visualization``'s own bounds, established by posting to a real channel.
MAX_CHART_TITLE_LENGTH = 50
MAX_CHART_SEGMENTS = 12
MAX_CHART_LABEL_LENGTH = 20
# The bounds a series chart adds on top of those. A ``bar``, ``area`` or ``line``
# chart holds up to twelve series, each of which must hold exactly one point
# for every one of up to twenty x-axis categories. "Exactly one for every" is
# Slack's rule and not a convenience: a series that skips a category is refused,
# so a source that leaves a gap is declined here rather than filled with a zero
# the author never wrote.
MAX_CHART_SERIES = 12
MAX_CHART_CATEGORIES = 20
# ``x_label`` and ``y_label``, which are axis titles rather than tick labels and
# get the title's ceiling rather than the label's.
MAX_AXIS_LABEL_LENGTH = 50
# Stands in when a chart holds no title and sits under no heading.
DEFAULT_CHART_TITLE = "Chart"

# The four chart types Slack draws. ``line`` is in the published reference and
# is easy to miss because the block's own summary sentence names three; it was
# confirmed accepted by posting a ``line`` chart to a real channel.
PIE_CHART = "pie"
BAR_CHART = "bar"
AREA_CHART = "area"
LINE_CHART = "line"

# Which mermaid ``xychart-beta`` plot keyword produces which Slack chart.
# mermaid has only these two, and no area plot at all, so ``area`` is reachable
# from a ``vega-lite`` or ``blockkit`` fence and not from mermaid.
MERMAID_PLOT_CHARTS = {"bar": BAR_CHART, "line": LINE_CHART}

# Which Vega-Lite ``mark`` produces which Slack chart, and the whole of what is
# supported: a mark absent from this table is declined rather than approximated
# by the nearest one that is present. ``arc`` is Vega-Lite's pie -- there is no
# ``pie`` mark -- and it is recognised by its ``theta`` encoding.
#
# Deliberately absent, each because Slack has no comparable mark and drawing one
# of the four below in its place would answer a question the author did not ask:
# ``point``, ``circle``, ``square`` and ``tick`` (a scatter needs a continuous
# x-axis, which ``data_visualization`` has not got), ``rect`` (a heatmap),
# ``rule`` and ``trail``, ``text``, ``geoshape``, ``image``, and the composite
# ``boxplot``, ``errorbar`` and ``errorband`` marks, whose statistics Slack
# cannot show at all.
VEGA_LITE_MARKS = {
    "bar": BAR_CHART,
    "area": AREA_CHART,
    "line": LINE_CHART,
    "arc": PIE_CHART,
}

# Top-level Vega-Lite keys that make a spec more than one chart. Every one of
# them is declined: ``data_visualization`` is a single chart, and picking one
# panel of a faceted spec to draw would silently drop the rest.
VEGA_LITE_COMPOSITION_KEYS = (
    "layer",
    "facet",
    "repeat",
    "concat",
    "hconcat",
    "vconcat",
    "spec",
)
# Encoding-level keys that change what is drawn from what the rows literally
# say. None of them has a ``data_visualization`` equivalent, so each is declined
# rather than ignored -- ignoring one draws a chart that disagrees with its own
# spec, which is the failure this module exists to avoid.
#
# ``aggregate``, ``bin`` and ``timeUnit`` compute the value: honouring them means
# implementing Vega-Lite's aggregation here, and ignoring them means charting the
# raw rows under a legend that promises a mean. ``stack`` chooses between stacked
# and overlaid series; Slack always layers, so a spec asking to stack or to
# normalise would come back overlaid.
#
# ``sort`` is deliberately not one of them. It names the category order, and
# ``axis_config.categories`` is exactly that order, so the one spelling that
# states it outright is read rather than refused -- see
# ``_vega_lite_category_order`` for which spelling that is and why the rest still
# decline.
VEGA_LITE_DERIVED_KEYS = ("aggregate", "bin", "timeUnit", "stack")
# The faceting channels, which are ``facet`` by another name.
VEGA_LITE_FACET_CHANNELS = ("row", "column", "facet")

# A fenced block is verbatim: a pipe table inside one is sample text the user
# asked to see as written, not a table to render. Deliberately a local copy of
# the connector's fence patterns -- importing them would couple a pure renderer
# to the connector for two regexes, and the renderer must stay liftable.
_FENCE_OPEN_RE = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
_FENCE_CLOSE_RE = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})[ \t]*$")

# "---", ":---", "---:" and ":---:" are the four GFM alignment spellings. The
# delimiter row is what distinguishes a table from an ordinary sentence that
# happens to contain a pipe, so it is required rather than inferred.
_DELIMITER_CELL_RE = re.compile(r"^:?-{1,}:?$")

# mrkdwn a raw_text cell would show as punctuation rather than render.
_LINK_RE = re.compile(r"<(?P<url>[^<>|]*)(?:\|(?P<label>[^<>]*))?>")
# Tells a link apart from the other things Slack wraps in angle brackets. A
# "<@U01ABCDEF>" mention, a "<#C01ABCDEF|general>" channel reference and a
# "<!here>" all reach this module from the normaliser and none of them is a
# destination; a link always holds a scheme and none of those do.
_URL_SCHEME_RE = re.compile(r"[a-z][a-z0-9+.\-]*:", re.IGNORECASE)
_CODE_SPAN_RE = re.compile(r"(?P<ticks>`+)(?P<body>.+?)(?P=ticks)")
# The two shapes a caption is taken from. A Markdown heading survives the
# connector's normaliser as a bold line rather than as "##", so both spellings
# are recognised: the renderer is handed normalised mrkdwn, but the same module
# is testable on either.
_HEADING_RE = re.compile(r"^#{1,6}\s+(?P<text>.+?)\s*#*$")
_BOLD_LINE_RE = re.compile(
    r"^(?P<marker>\*{1,2}|_{1,2})(?P<text>\S.*?)(?P=marker)\s*:?$"
)
# What a cell has to look like for its column to sort as numbers. Deliberately
# strict: thousands separators and a sign, and nothing else. A percentage or a
# currency amount would have to be reinterpreted to produce a value, and a
# column that sorts by a number the reader cannot see in the cell is worse than
# one that sorts alphabetically.
_NUMBER_RE = re.compile(r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")
# mermaid's pie chart, which is the one chart type of its own that is not beta.
# "pie", optionally "showData", optionally a title on the same line.
_MERMAID_PIE_RE = re.compile(
    r"^pie(?:\s+showData)?(?:\s+title\s+(?P<title>.*\S))?\s*$", re.IGNORECASE
)
_MERMAID_TITLE_RE = re.compile(r"^title\s+(?P<title>.*\S)\s*$", re.IGNORECASE)
_MERMAID_SEGMENT_RE = re.compile(
    r"^(?:\"(?P<quoted>[^\"]*)\"|(?P<bare>[^:]+?))\s*:\s*"
    r"(?P<value>[+-]?\d+(?:\.\d+)?)$"
)
# mermaid's bar and line charts, which share one diagram type. ``xychart`` is an
# accepted alias for ``xychart-beta`` from mermaid 11.10 onwards, and mermaid's
# own detector reads ``xychart(-beta)?``, so both spellings are read here rather
# than making an author's mermaid version decide whether the chart draws.
#
# The opening word takes an optional orientation, and the two are not
# equivalent: ``vertical`` is mermaid's default and is exactly what Slack draws,
# so it is accepted, while ``horizontal`` is declined -- see ``_xychart``.
_XYCHART_OPEN_RE = re.compile(
    r"^xychart(?:-beta)?(?P<rest>\s+\S.*?)?\s*$", re.IGNORECASE
)
XYCHART_DEFAULT_ORIENTATION = "vertical"
_XYCHART_TITLE_RE = re.compile(
    r'^title\s+(?:"(?P<quoted>[^"]*)"|(?P<bare>.*\S))\s*$', re.IGNORECASE
)
# ``bar [1, 2, 3]``, and the named form ``bar "Revenue" [1, 2, 3]`` that fills
# mermaid's legend. The name is optional in mermaid, so a plot without one gets
# a derived name here -- see ``_xychart``.
#
# Values are plain numbers. mermaid 11.16 added a per-point label,
# ``line [540 "PaLM", 65 "LLaMA-65B"]``, which is declined: it is an annotation
# drawn beside the point, and Slack's ``label`` is the x-axis category a point
# belongs to rather than a caption, so there is nowhere for it to go. It
# declines by itself, on the number check, rather than needing a rule.
_XYCHART_PLOT_RE = re.compile(
    r'^(?P<plot>bar|line)\s*(?:"(?P<quoted>[^"]*)"|(?P<bare>[^\[\]"]*\S))?\s*'
    r"\[(?P<values>[^\[\]]*)\]\s*$",
    re.IGNORECASE,
)
# An axis line, split from its keyword so the remainder can be read in order.
_XYCHART_AXIS_RE = re.compile(r"^(?P<axis>x-axis|y-axis)\b(?P<rest>.*)$", re.IGNORECASE)
# mermaid's numeric axis range, ``4000 --> 11000``. Recognised so that it can be
# told apart from an axis title and dropped: Slack scales an axis to its data
# and offers no way to pin the range, and a range is a hint about presentation
# rather than data, so dropping it loses nothing the reader can see.
_XYCHART_RANGE_RE = re.compile(
    r"^[+-]?\d+(?:\.\d+)?\s*-->\s*[+-]?\d+(?:\.\d+)?$"
)
_XYCHART_RANGE_SEARCH_RE = re.compile(
    r"[+-]?\d+(?:\.\d+)?\s*-->\s*[+-]?\d+(?:\.\d+)?"
)
_NUMERIC_LITERAL_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
# Comments and the accessibility directives, none of which are data.
_MERMAID_NOISE_RE = re.compile(r"^(?:%%|accTitle\s*:|accDescr\s*[:{])", re.IGNORECASE)
_EMPHASIS_RE = re.compile(r"(?<!\w)(?P<marker>[*_~])(?=\S)(?P<body>.+?)(?<=\S)(?P=marker)(?!\w)")


class _Table:
    """One parsed Markdown table: a rectangular grid of cells.

    Held twice. ``rows`` is each cell reduced to what a ``raw_text`` cell can
    show, which is what the character budget is measured against and what a cell
    without a link is rendered as. ``raw_rows`` is the same grid before that
    reduction, because reducing a link throws its destination away and only the
    unreduced cell still knows where it pointed.
    """

    def __init__(
        self,
        rows: list[list[str]],
        alignments: list[str],
        raw_rows: list[list[str]],
    ) -> None:
        self.rows = rows
        self.alignments = alignments
        self.raw_rows = raw_rows


class _Blocks:
    """One fence's rendering, with the kind of source it was rendered from.

    ``kind`` is set by whichever branch of ``_fenced_blocks`` produced the
    blocks, which is the only place that still knows: a ``data_visualization``
    built from mermaid and one pasted into a ```blockkit fence are the same
    object by the time anyone downstream sees them.
    """

    def __init__(self, blocks: list[dict[str, Any]], kind: str) -> None:
        self.blocks = blocks
        self.kind = kind


def contains_table(mrkdwn: str) -> bool:
    """True when *mrkdwn* holds at least one renderable Markdown table.

    Cheap enough to gate on before rendering, and exact: it runs the same
    segmentation the renderer does, so it cannot report a table the renderer
    would then decline to find.
    """
    return any(isinstance(segment, _Table) for segment in _segment(mrkdwn))


def render_blocks(
    mrkdwn: str,
    *,
    requested: bool = False,
    render_tables: str = RENDER_TABLES_DEFAULT,
    allowed_block_types: Collection[str] | None = None,
    allow_interactive: bool = DEFAULT_ALLOW_INTERACTIVE_BLOCKS,
) -> list[dict[str, Any]] | None:
    """The blocks alone, for every caller that does not care what built them."""
    rendered = render_blocks_with_kind(
        mrkdwn,
        requested=requested,
        render_tables=render_tables,
        allowed_block_types=allowed_block_types,
        allow_interactive=allow_interactive,
    )
    return None if rendered is None else rendered[0]


def render_blocks_with_kind(
    mrkdwn: str,
    *,
    requested: bool = False,
    render_tables: str = RENDER_TABLES_DEFAULT,
    allowed_block_types: Collection[str] | None = None,
    allow_interactive: bool = DEFAULT_ALLOW_INTERACTIVE_BLOCKS,
) -> tuple[list[dict[str, Any]], str] | None:
    """Return ``(blocks, kind)`` for *mrkdwn*, or ``None`` to keep it plain text.

    ``None`` means "post this the way you always would", and is returned for
    every case where blocks are not an improvement or not possible: no table in
    the content, nothing left after the split, or a result that would breach one
    of Slack's limits. It is never a failure -- the caller has a complete text
    rendering of the same content in hand either way.

    ``requested`` is the reply's own Block Kit marker, already read and stripped
    by the connector. It asks for a ``data_table``, which is the one way an
    author can ask for paging, sorting and filtering on a table an operator
    configured as ``basic``.

    It says nothing about fences. A ``mermaid`` or ``blockkit`` fence renders on
    its language alone, so the marker neither turns one on nor off; it decides
    how a Markdown *table* is shaped and nothing else.

    ``render_tables`` is the operator's choice between the two table blocks, or
    ``off`` for neither. ``off`` outranks ``requested``: an author asking for a
    ``data_table`` is asking within what the operator allows, and a table is not
    even looked for under ``off``, so the pipes stay in the prose they were
    written in and reach the channel unchanged. An unknown value is read as the
    default here rather than refused, because this is a rendering choice and the
    caller that resolved the config has already said so in the log.

    ``allowed_block_types`` restricts what a ``blockkit`` fence may hold.
    ``None`` or empty is no restriction, which is the default; a non-empty
    collection admits those names and refuses every other. ``allow_interactive``
    is the separate control over elements that post back to the app, and it
    stays independent of the type list on purpose -- see ``INTERACTIVE_TYPES``.

    ``kind`` is one of the ``BLOCK_KIND_*`` names and is collected as the
    segments are consumed, while it is still known which renderer produced
    which block. It is only ever read when Slack refuses the payload, but it has
    to be recorded here because by then the payload is a flat list of blocks
    that no longer says where any of them came from.
    """
    mode = (
        render_tables
        if render_tables in RENDER_TABLES_MODES
        else RENDER_TABLES_DEFAULT
    )
    segments = _segment(
        mrkdwn,
        tables=mode != RENDER_TABLES_OFF,
        allowed_block_types=allowed_block_types,
        allow_interactive=allow_interactive,
    )
    if not any(isinstance(segment, (_Table, _Blocks)) for segment in segments):
        return None

    blocks: list[dict[str, Any]] = []
    # One budget per block type, because Slack counts them apart.
    characters = {"table": 0, "data_table": 0}
    budgets = {
        "table": MAX_PLAIN_TABLE_CHARACTERS_PER_MESSAGE,
        "data_table": MAX_TABLE_CHARACTERS_PER_MESSAGE,
    }
    # The prose immediately above a table, which is where its caption comes
    # from. Cleared by a table so the second of two adjacent tables cannot
    # inherit the caption the first one already used.
    preceding_prose: str | None = None
    kinds: list[str] = []
    for segment in segments:
        if isinstance(segment, _Table):
            block = _table_block(
                segment,
                caption=_caption_for(preceding_prose),
                force_data_table=requested or mode == RENDER_TABLES_DATA,
            )
            if block is None:
                return None
            kind = block["type"]
            characters[kind] += _table_characters(segment) + len(
                block.get("caption", "")
            )
            if characters[kind] > budgets[kind]:
                return None
            blocks.append(block)
            kinds.append(BLOCK_KIND_DATA)
            preceding_prose = None
            continue
        if isinstance(segment, _Blocks):
            blocks.extend(segment.blocks)
            kinds.append(segment.kind)
            preceding_prose = None
            if len(blocks) > MAX_BLOCKS_PER_MESSAGE:
                return None
            continue
        preceding_prose = segment
        blocks.extend(_section_blocks(segment))
        if len(blocks) > MAX_BLOCKS_PER_MESSAGE:
            return None

    if not blocks or len(blocks) > MAX_BLOCKS_PER_MESSAGE:
        return None
    charts = sum(1 for block in blocks if block["type"] == "data_visualization")
    if charts > MAX_DATA_VISUALIZATIONS_PER_MESSAGE:
        return None
    return blocks, combine_block_kinds(kinds)


def streamable_prose_prefix(
    mrkdwn: str,
    *,
    allowed_block_types: Collection[str] | None = None,
    allow_interactive: bool = DEFAULT_ALLOW_INTERACTIVE_BLOCKS,
) -> int:
    """How much of *mrkdwn* may be sent before the rest of the reply is known.

    A streamed reply is appended a piece at a time and nothing already sent can
    be taken back, so a piece may only go out once no continuation can change
    what it is. Prose is safe: it stays prose however much follows it, and a
    ``section`` renders it the same whether it arrives whole or a line at a
    time. Structure is not. A fence is its own source text until the closing
    marker arrives and a rendering afterwards; a line of pipes is a paragraph
    until the delimiter row under it turns it into a table header. Sending
    either one early would put it on screen twice -- once as the text it looked
    like, once as the block it turned out to be.

    Everything from the first such line onwards therefore waits, and is composed
    in one go when the reply ends and the whole of it is known. Returns the
    length of the prefix that is safe to send: ``0`` when the content opens with
    structure, ``len(mrkdwn)`` when it is prose throughout.

    Dialect-agnostic, unlike ``render_blocks``, and it has to be: the streaming
    path calls this on the Markdown the model wrote and calls the renderer on the
    normalised form of the same text. The two agree because the only things
    looked at here are a fence marker and a pipe-and-delimiter table, which are
    written identically in Markdown and in mrkdwn. Normalisation adds pipes when
    it turns a link into ``<url|label>`` and ``_split_row`` already discounts
    those, and it never writes a delimiter row -- so a table found in one form is
    found in the other, and neither form invents one.

    The result only ever grows as *mrkdwn* grows, which is what makes it usable
    as an append offset. A fence opener and a table header both hold from where
    they start, and the last line of what has arrived so far is held whenever it
    could still become a header.
    """
    allowed = _normalised_block_types(allowed_block_types)
    lines = (mrkdwn or "").splitlines(keepends=True)
    bodies = [line.rstrip("\r\n") for line in lines]
    prose: list[str] = []
    offset = 0
    index = 0

    while index < len(lines):
        body = bodies[index]

        opening = _FENCE_OPEN_RE.match(body)
        if opening:
            span, consumed, closed = _fence_span(
                bodies, index, opening.group("fence")
            )
            if not closed:
                return offset
            rendered = _fenced_blocks(
                (opening.group("info") or "").strip(),
                span,
                allowed_block_types=allowed,
                allow_interactive=allow_interactive,
                preceding_prose="\n".join(prose),
                # A probe, not a decision. This runs on every streaming update
                # and rescans the whole reply each time, so a fence that
                # declines would report itself once per update rather than once.
                # The compose-time call reports it.
                log=False,
            )
            if rendered is not None:
                return offset
            # A fence that renders to nothing is its own text, exactly as it is
            # for ``_segment``, so it streams like the prose it will become.
            prose.extend(bodies[index : index + consumed])
            offset += sum(len(line) for line in lines[index : index + consumed])
            index += consumed
            continue

        if "|" in body:
            if index + 1 >= len(lines):
                return offset
            table, _ = _parse_table(bodies, index)
            if table is not None:
                return offset

        prose.append(body)
        offset += len(lines[index])
        index += 1

    return offset


def _table_characters(table: _Table) -> int:
    """What one table costs the message's character budget."""
    return sum(len(cell) for row in table.rows for cell in row)


def _segment(
    mrkdwn: str,
    *,
    tables: bool = True,
    allowed_block_types: Collection[str] | None = None,
    allow_interactive: bool = DEFAULT_ALLOW_INTERACTIVE_BLOCKS,
) -> list[Any]:
    """Split *mrkdwn* into prose strings, ``_Table``s and ``_Blocks``.

    Prose is handed on verbatim: it is already mrkdwn and a ``section`` renders
    it as-is. Only a header row immediately followed by a delimiter row starts a
    table, and only outside a code fence.

    ``tables=False`` is ``render_tables: off``, expressed by not looking for a
    table rather than by discarding one afterwards. The lines fall through to
    prose exactly as a table with no delimiter row does, so a reply whose only
    structure was a table produces no blocks at all, while a chart fenced beside
    it still renders on its own language.

    A fence is taken whole and then asked what language it named. One that named
    a rendering language becomes a ``_Blocks``; every other fence, including one
    whose contents turn out to be unusable, stays prose -- which is what makes a
    fenced pipe table sample text rather than a table, and what leaves a broken
    chart on screen as source the author can see is broken.
    """
    allowed = _normalised_block_types(allowed_block_types)
    lines = (mrkdwn or "").splitlines()
    segments: list[Any] = []
    prose: list[str] = []
    index = 0

    while index < len(lines):
        line = lines[index]

        opening = _FENCE_OPEN_RE.match(line)
        if opening:
            body, consumed, closed = _fence_span(lines, index, opening.group("fence"))
            rendered = (
                _fenced_blocks(
                    (opening.group("info") or "").strip(),
                    body,
                    allowed_block_types=allowed,
                    allow_interactive=allow_interactive,
                    preceding_prose="\n".join(prose),
                )
                if closed
                else None
            )
            if rendered is not None:
                if prose:
                    segments.append("\n".join(prose))
                    prose = []
                segments.append(rendered)
            else:
                prose.extend(lines[index : index + consumed])
            index += consumed
            continue

        table, consumed = _parse_table(lines, index) if tables else (None, 0)
        if table is not None:
            if prose:
                segments.append("\n".join(prose))
                prose = []
            segments.append(table)
            index += consumed
            continue

        prose.append(line)
        index += 1

    if prose:
        segments.append("\n".join(prose))
    return segments


def _fence_span(lines: list[str], start: int, marker: str) -> tuple[list[str], int, bool]:
    """The fence opening at ``lines[start]``: its body, its length, and whether it closed.

    An unclosed fence runs to the end of the content and is reported as such, so
    that the rest of the message stays verbatim prose rather than being scanned
    for tables the author fenced deliberately.
    """
    index = start + 1
    while index < len(lines):
        closing = _FENCE_CLOSE_RE.match(lines[index])
        if closing:
            fence = closing.group("fence")
            if fence[0] == marker[0] and len(fence) >= len(marker):
                return lines[start + 1 : index], index - start + 1, True
        index += 1
    return lines[start + 1 :], len(lines) - start, False


def _fenced_blocks(
    info: str,
    body: list[str],
    *,
    allowed_block_types: frozenset[str],
    allow_interactive: bool,
    preceding_prose: str = "",
    log: bool = True,
) -> _Blocks | None:
    """The blocks a fence stands for, or ``None`` to leave it as prose.

    The language decides, and only the language. A fence is ambiguous in a way a
    Markdown table is not -- ```` ```mermaid ```` means "draw this" and "show me
    this source" equally well -- and the ambiguity is resolved by giving each
    reading its own word rather than by adding a marker to one of them:
    ``mermaid``, ``vega-lite`` and ``blockkit`` draw, ``slack-raw`` and
    everything else shows.

    Only the first word of the info string is read, which is CommonMark's own
    rule. Anything after it is ignored rather than being another place a
    decision could hide.

    A rendering language that declines is logged, and a fence that named no
    rendering language is not. The two returns are the same ``None`` and mean
    entirely different things: one is a ```` ```python ```` fence behaving as
    intended, the other is an author who asked for a drawing and did not get
    one. The channel shows source in both cases, so only the log tells them
    apart.

    ``log`` exists because that line belongs once per *decision* and this
    function is not called once per decision. ``streamable_prose_prefix`` calls
    it to ask whether a fence is structure or prose it may send now, and the
    streaming path asks that on every update, rescanning the reply from the
    start. The probe passes ``log=False``; the decision is reported once, at
    compose time. The default is the logging one, so a new caller has to say it
    is a probe.
    """
    tokens = info.split()
    language = tokens[0].lower() if tokens else ""
    rendered: list[dict[str, Any]] | None
    # The language is also what the kind is read from, because the language is
    # the branch: only ```blockkit holds JSON the author wrote by hand, and
    # only that JSON can be shown back to them as something to fix. A chart
    # fence is a specification this module compiled, so its failure is a data
    # failure like a table's and is shown the same way.
    kind = BLOCK_KIND_FENCE
    if language == BLOCK_KIT_FENCE_LANGUAGE:
        rendered = _raw_blocks(
            body,
            allowed_block_types=allowed_block_types,
            allow_interactive=allow_interactive,
        )
        # An operator who switched interactive fences on has admitted elements
        # that post back to this app, and a reader cannot tell one of those from
        # a real approval prompt. So the fence stops being an author's text that
        # can be quoted back and becomes a control, with a control's failure.
        if rendered is not None and any(_is_interactive(block) for block in rendered):
            kind = BLOCK_KIND_INTERACTIVE
    elif language == MERMAID_FENCE_LANGUAGE:
        chart = _mermaid_chart(body, preceding_prose)
        rendered = None if chart is None else [chart]
        kind = BLOCK_KIND_DATA
    elif language == VEGA_LITE_FENCE_LANGUAGE:
        chart = _vega_lite_chart(body, preceding_prose)
        rendered = None if chart is None else [chart]
        kind = BLOCK_KIND_DATA
    else:
        return None
    if rendered is None:
        if not log:
            return None
        # Structural only, no fence body: the surrounding logs describe
        # deliveries and not what was said in them, and a spec pasted into a log
        # is message content by another route.
        logger.info(
            "[SlackChannel] a `%s` fence is outside what this connector can "
            "render and stays in the message as its own source (%d line(s))",
            language,
            len(body),
        )
        return None
    return _Blocks(rendered, kind)


def _normalised_block_types(
    allowed_block_types: Collection[str] | None,
) -> frozenset[str]:
    """The allow-list as a set of comparable names, empty when unrestricted.

    Trimmed and lower-cased on both sides of the comparison so an operator who
    writes ``Data_Table`` in their config gets the block they meant rather than
    a silently refused fence. Nothing is lost by being lenient here: this list
    is a portability preference, and the check that is not one does not consult
    it.
    """
    if not allowed_block_types:
        return DEFAULT_ALLOWED_BLOCK_TYPES
    return frozenset(
        name.strip().lower()
        for name in allowed_block_types
        if isinstance(name, str) and name.strip()
    )


def _raw_blocks(
    body: list[str],
    *,
    allowed_block_types: frozenset[str] = DEFAULT_ALLOWED_BLOCK_TYPES,
    allow_interactive: bool = DEFAULT_ALLOW_INTERACTIVE_BLOCKS,
) -> list[dict[str, Any]] | None:
    """Validate hand-written Block Kit JSON, or decline the whole fence.

    Three checks, of which two are separately configurable and the third is not.

    1. Every block is an object naming a ``type``. A block without one is not a
       block, whatever the allow-list says, and Slack would refuse the message.
    2. That type is on ``allowed_block_types`` -- unless the list is empty, which
       is the default and means "no restriction".
    3. Nothing in the block is interactive, unless ``allow_interactive`` says
       otherwise. Independent of the list above on purpose: widening which
       blocks may be drawn is a portability decision, and it must not be a way
       of also widening what a reader can be asked to click.

    Declining returns ``None``, which leaves the fence on screen as the source
    the author wrote. That is the right failure: the author is the only person
    who can fix it, and it is their own text they are shown.
    """
    try:
        payload = json.loads("\n".join(body))
    except ValueError:
        return None
    return check_blocks(
        payload,
        allowed_block_types=allowed_block_types,
        allow_interactive=allow_interactive,
    )


def check_blocks(
    payload: Any,
    *,
    allowed_block_types: "Collection[str] | None" = None,
    allow_interactive: bool = DEFAULT_ALLOW_INTERACTIVE_BLOCKS,
) -> "list[dict[str, Any]] | None":
    """The three checks above, applied to a payload somebody already parsed.

    Split out of :func:`_raw_blocks` so that a Block Kit payload a model passed
    as a tool argument goes through this and nothing else. That is the whole
    point: the fence and the argument are two ways of writing the same thing,
    and a second reading of either check would eventually admit through one of
    them what the other refuses.

    Both shapes are accepted, as they are from a fence: a list of blocks, and
    the ``{"blocks": [...]}`` object Block Kit Builder copies out. A single
    bare block object is read as a list of one.

    ``allowed_block_types`` is normalised here rather than expected normalised,
    because the two callers hold it in different forms -- the fence path has
    already reduced the operator's list to a set, and a tool has the raw config
    value. Reducing an already-reduced set costs nothing and removes the one way
    the two callers could pass different things.
    """
    if isinstance(payload, dict):
        # The shape Block Kit Builder copies out, and a bare single block.
        inner = payload.get("blocks")
        payload = inner if isinstance(inner, list) else [payload]
    if not isinstance(payload, list) or not payload:
        return None
    allowed = _normalised_block_types(allowed_block_types)
    for block in payload:
        if not isinstance(block, dict):
            return None
        kind = block.get("type")
        if not isinstance(kind, str) or not kind.strip():
            return None
        if allowed and kind.strip().lower() not in allowed:
            return None
        if not allow_interactive and _is_interactive(block):
            return None
    return payload


def _mermaid_chart(body: list[str], preceding_prose: str) -> dict[str, Any] | None:
    """Turn a mermaid diagram into a ``data_visualization``, or decline it.

    Two diagram types reach a chart: ``pie``, and ``xychart-beta`` for bar and
    line. mermaid has no area chart at all, so ``area`` is reachable only from a
    ``vega-lite`` or ``blockkit`` fence.

    Anything else -- a flowchart, a sequence diagram, a Gantt -- is declined and
    stays on screen as the diagram source, which still reads as a description of
    the picture rather than as a wall of syntax.
    """
    lines = [
        line.strip()
        for line in body
        if line.strip() and not _MERMAID_NOISE_RE.match(line.strip())
    ]
    if not lines:
        return None
    if _MERMAID_PIE_RE.match(lines[0]):
        return _mermaid_pie(lines, preceding_prose)
    if _XYCHART_OPEN_RE.match(lines[0]):
        return _xychart(lines, preceding_prose)
    return None


def _mermaid_pie(lines: list[str], preceding_prose: str) -> dict[str, Any] | None:
    """Turn a mermaid ``pie`` diagram into a pie ``data_visualization``.

    ``None`` for anything outside Slack's shape: an unrecognised line, no
    segments, more than twelve of them, or a value at or below zero. Labels and
    the title are clamped rather than declined, because a shortened label still
    says which slice is which while a refused post says nothing at all.
    """
    opening = _MERMAID_PIE_RE.match(lines[0])
    if opening is None:
        return None

    title = opening.group("title") or ""
    segments: list[dict[str, Any]] = []
    for line in lines[1:]:
        heading = _MERMAID_TITLE_RE.match(line)
        if heading is not None:
            title = title or heading.group("title")
            continue
        segment = _MERMAID_SEGMENT_RE.match(line)
        if segment is None:
            # An unrecognised line means this is not the diagram it looked like,
            # and half a pie chart is a misleading one.
            return None
        value = float(segment.group("value"))
        if value <= 0:
            return None
        label = (segment.group("quoted") or segment.group("bare") or "").strip()
        segments.append({"label": _clamp(label, MAX_CHART_LABEL_LENGTH), "value": _chart_number(value)})

    chart = _pie_chart(segments)
    if chart is None:
        return None
    return _chart_block(chart, title, preceding_prose)


def _chart_block(
    chart: dict[str, Any], title: str, preceding_prose: str
) -> dict[str, Any]:
    """Wrap a validated ``chart`` payload in its ``data_visualization`` block.

    ``title`` is required by Slack and every source this module reads can omit
    it, so the heading the chart sits under stands in, and a generic word stands
    in for that. Clamped rather than declined for the same reason a label is: a
    shortened title still names the chart.
    """
    return {
        "type": "data_visualization",
        "title": _clamp(
            title.strip() or _caption_for(preceding_prose, DEFAULT_CHART_TITLE),
            MAX_CHART_TITLE_LENGTH,
        ),
        "chart": chart,
    }


def _pie_chart(segments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Slack's ``pie`` payload for *segments*, or ``None`` if it is out of bounds."""
    if not segments or len(segments) > MAX_CHART_SEGMENTS:
        return None
    return {"type": PIE_CHART, "segments": segments}


def _chart_number(value: float) -> int | float:
    """A chart value as the narrowest number that still says the same thing.

    ``7.0`` is written by an author as ``7`` and is read back by one the same
    way, so it is sent as ``7``. Slack accepts either.
    """
    return int(value) if float(value).is_integer() else value


def _series_chart(
    kind: str,
    names: list[str],
    categories: list[str],
    rows: list[list[float]],
    *,
    x_label: str = "",
    y_label: str = "",
) -> dict[str, Any] | None:
    """Slack's ``bar``, ``area`` or ``line`` payload, or ``None`` to decline.

    *rows* is one list of values per name, each holding exactly one value for
    each of *categories* in the same order, which is Slack's own rule -- see
    ``MAX_CHART_CATEGORIES``.

    Declined when there are no series or more than twelve, no categories or more
    than twenty, or when clamping collides. The collision is the subtle one.
    Labels are cut to twenty characters and series names to twenty, so two
    categories that differ only past that point arrive identical -- and Slack
    matches a data point to its category by label, so a duplicate silently
    reassigns points to the wrong column. Merging them would be a wrong chart
    rather than a shortened one, which is the line this module does not cross.
    """
    if not names or len(names) > MAX_CHART_SERIES:
        return None
    if not categories or len(categories) > MAX_CHART_CATEGORIES:
        return None

    clamped_categories = [_clamp(label, MAX_CHART_LABEL_LENGTH) for label in categories]
    if len(set(clamped_categories)) != len(clamped_categories):
        return None
    clamped_names = [_clamp(name, MAX_CHART_LABEL_LENGTH) for name in names]
    if len(set(clamped_names)) != len(clamped_names):
        return None
    if any(len(values) != len(categories) for values in rows):
        return None

    chart: dict[str, Any] = {
        "type": kind,
        "series": [
            {
                "name": name,
                "data": [
                    {"label": label, "value": _chart_number(value)}
                    for label, value in zip(clamped_categories, values)
                ],
            }
            for name, values in zip(clamped_names, rows)
        ],
        # ``categories`` is what fixes the left-to-right order, and the axis
        # titles live in here beside it. Written as siblings of ``series``
        # instead they do not merely go unread: Slack refuses the whole chart
        # with "failed to match exactly one allowed schema", which was
        # established by posting both spellings to a real channel.
        "axis_config": {"categories": clamped_categories},
    }
    if x_label.strip():
        chart["axis_config"]["x_label"] = _clamp(x_label.strip(), MAX_AXIS_LABEL_LENGTH)
    if y_label.strip():
        chart["axis_config"]["y_label"] = _clamp(y_label.strip(), MAX_AXIS_LABEL_LENGTH)
    return chart


def _xychart(lines: list[str], preceding_prose: str) -> dict[str, Any] | None:
    """Turn a mermaid ``xychart-beta`` diagram into a bar or line chart.

    Every statement must be one this parser knows -- a title, an ``x-axis``, a
    ``y-axis``, or a ``bar`` or ``line`` plot -- and one it does not declines
    the whole diagram. That is what makes the diagram type's beta status
    survivable: a mermaid release that changes the syntax produces a fence that
    falls through to source, visibly undrawn, rather than a chart drawn from a
    misread of the new spelling.

    Declined, each for a reason Slack cannot express rather than for tidiness:

    * ``horizontal``. mermaid takes an orientation after the diagram keyword and
      ``data_visualization`` draws vertically only, so a chart asked for on its
      side would come back upright, which answers a different question.
      ``vertical`` is mermaid's own default and is accepted, because it asks for
      what Slack already draws.
    * an ``x-axis`` with no category list. mermaid's other spelling is a numeric
      range, and Slack's x-axis is a list of categories rather than a scale.
    * ``bar`` and ``line`` plots in the same diagram. mermaid draws that as a
      combination chart, and does so legitimately; Slack picks one chart type
      for the whole block and has no way to say "these bars and that line".
    * a plot whose value count disagrees with the number of categories, which is
      a gap Slack would refuse and this module will not invent a zero to fill.
    * a repeated title or axis, which is not a diagram that renders anywhere.

    The ``y-axis`` numeric range is read and dropped rather than declined: it
    pins mermaid's axis scale, Slack always scales to the data, and a scale is a
    statement about presentation rather than about what the numbers are.

    A plot may name itself, and that name is the series name. One unnamed plot
    takes the y-axis title, because that is what the single line of data is;
    several unnamed ones take ``Series 1``, ``Series 2`` and so on, which is
    honest about mermaid having said nothing rather than inventing a meaning for
    each.
    """
    opening = _XYCHART_OPEN_RE.match(lines[0])
    if opening is None:
        return None
    orientation = (opening.group("rest") or "").strip().lower()
    if orientation and orientation != XYCHART_DEFAULT_ORIENTATION:
        return None

    title = ""
    x_label = ""
    y_label = ""
    categories: list[str] | None = None
    seen_y_axis = False
    plots: list[tuple[str, str, list[float]]] = []

    for line in lines[1:]:
        heading = _XYCHART_TITLE_RE.match(line)
        if heading is not None:
            if title:
                return None
            title = (heading.group("quoted") or heading.group("bare") or "").strip()
            continue

        axis = _XYCHART_AXIS_RE.match(line)
        if axis is not None:
            parsed = _xychart_axis(axis.group("rest"))
            if parsed is None:
                return None
            label, items = parsed
            if axis.group("axis").lower() == "x-axis":
                if categories is not None or items is None or not items:
                    return None
                categories, x_label = items, label
            else:
                # A y-axis holds a title and a range, never a category list.
                if seen_y_axis or items is not None:
                    return None
                seen_y_axis, y_label = True, label
            continue

        plot = _XYCHART_PLOT_RE.match(line)
        if plot is None:
            return None
        values = _split_bracketed(plot.group("values"))
        if not values or any(
            _NUMERIC_LITERAL_RE.match(value) is None for value in values
        ):
            return None
        name = (plot.group("quoted") or plot.group("bare") or "").strip()
        plots.append(
            (plot.group("plot").lower(), name, [float(value) for value in values])
        )

    if not categories or not plots:
        return None
    kinds = {plot for plot, _, _ in plots}
    if len(kinds) != 1:
        return None
    kind = MERMAID_PLOT_CHARTS[kinds.pop()]

    names = [
        name or _xychart_default_name(index, len(plots), y_label, kind)
        for index, (_, name, _) in enumerate(plots, start=1)
    ]
    chart = _series_chart(
        kind,
        names,
        categories,
        [values for _, _, values in plots],
        x_label=x_label,
        y_label=y_label,
    )
    return None if chart is None else _chart_block(chart, title, preceding_prose)


def _xychart_default_name(index: int, total: int, y_label: str, kind: str) -> str:
    """A name for a plot that did not give itself one.

    Slack requires a name on every series and requires them to be distinct, so
    something has to be chosen. The sole plot of a chart is the y-axis, and the
    y-axis title names it better than anything invented would; where there is no
    title, the plot keyword at least says what it is. Several unnamed plots get
    numbered, which says only what mermaid said -- that these are different
    series -- rather than dressing them up as something they were not called.
    """
    if total == 1:
        return y_label.strip() or kind.title()
    return f"Series {index}"


def _xychart_axis(rest: str) -> tuple[str, list[str] | None] | None:
    """One ``x-axis`` or ``y-axis`` line's title and category list.

    Returns ``(title, categories)``, where ``categories`` is ``None`` when the
    line held none, or ``None`` outright when the line is not an axis this
    parser recognises. The two are distinct answers: a y-axis legitimately has
    no categories, and an x-axis without them is declined by the caller.

    The forms read are the ones mermaid documents -- a quoted or bare title,
    either alone or before a ``[...]`` list, and a ``4000 --> 11000`` range in
    place of the list.
    """
    rest = rest.strip()
    title = ""
    if rest.startswith('"'):
        closing = rest.find('"', 1)
        if closing < 0:
            return None
        title = rest[1:closing].strip()
        rest = rest[closing + 1 :].strip()

    bracket = rest.find("[")
    if bracket >= 0:
        leading = rest[:bracket].strip()
        if title and leading:
            # A quoted title and then loose words before the list: not a shape
            # mermaid draws, so not one to guess at.
            return None
        if not rest.endswith("]"):
            return None
        items = _split_bracketed(rest[bracket + 1 : -1])
        return None if items is None else (title or leading, items)

    # No category list, so what is left is a bare title, a numeric range, or a
    # title and then a range.
    if not title:
        span = _XYCHART_RANGE_SEARCH_RE.search(rest)
        if span is not None:
            title, rest = rest[: span.start()].strip(), rest[span.start() :].strip()
        else:
            title, rest = rest, ""
    if rest and _XYCHART_RANGE_RE.match(rest) is None:
        return None
    return title, None


def _split_bracketed(text: str) -> list[str] | None:
    """The comma-separated items of a mermaid ``[...]`` list.

    Split by hand rather than on a regex because a quoted label may hold a comma
    of its own, and splitting on every comma would turn one category into two.
    ``None`` for an unbalanced quote or an empty item, both of which mean the
    list is not the list it looked like.
    """
    if not text.strip():
        return []
    items: list[str] = []
    current: list[str] = []
    quoted = False
    for character in text:
        if character == '"':
            quoted = not quoted
            continue
        if character == "," and not quoted:
            items.append("".join(current).strip())
            current = []
            continue
        current.append(character)
    if quoted:
        return None
    items.append("".join(current).strip())
    return None if any(not item for item in items) else items


def _vega_lite_chart(body: list[str], preceding_prose: str) -> dict[str, Any] | None:
    """Turn a Vega-Lite specification into a ``data_visualization``, or decline.

    Vega-Lite is far larger than what Slack draws, so this reads the subset that
    maps onto ``data_visualization`` exactly and declines the rest rather than
    approximating it. A declined spec stays on screen as the JSON the author
    wrote, which is the same failure an unrecognised fence has always had: the
    reader sees the specification instead of a chart, rather than seeing a chart
    that disagrees with it.

    What maps:

    ==============================  ===========================================
    ``mark: "bar"``                 Slack ``bar``
    ``mark: "area"``                Slack ``area``
    ``mark: "line"``                Slack ``line``
    ``mark: "arc"``                 Slack ``pie`` -- Vega-Lite has no pie mark
    ``data.values``                 the rows every series is read from
    ``encoding.x``                  ``axis_config.categories``, in row order
    ``encoding.y``                  each series' ``data`` values
    ``encoding.color``              which series a row belongs to, by its value
    ``encoding.theta``              a pie segment's value, with ``color`` its label
    ``encoding.x.title``/``field``  ``axis_config.x_label``
    ``encoding.y.title``/``field``  ``axis_config.y_label``
    ``title``                       the block's ``title``
    ==============================  ===========================================

    What declines, and why Slack cannot say it:

    * any other ``mark`` -- see ``VEGA_LITE_MARKS`` for the list and the reason
      each is absent.
    * a spec that is more than one chart: ``layer``, ``facet``, ``repeat``,
      ``concat`` and their spellings, and a ``row``, ``column`` or ``facet``
      channel. A ``data_visualization`` is one chart, and drawing one panel of a
      faceted spec would drop the others silently.
    * ``transform``, and ``aggregate``, ``bin``, ``timeUnit`` or ``stack`` on a
      channel -- see ``VEGA_LITE_DERIVED_KEYS``.
    * a ``sort`` on any channel but ``x``, and on ``x`` any ``sort`` but an
      explicit array of the categories in the order they are to appear -- see
      ``_vega_lite_category_order``.
    * data this module cannot read: anything but inline ``data.values``, so a
      ``url``, a named source or a generated ``sequence`` all decline. Fetching
      a URL is not something a message renderer should be doing, and a spec
      whose data has not arrived cannot be drawn from.
    * an ``encoding`` channel that is neither read nor known to be inert. The
      inert ones are listed in the code below; anything else changes the picture
      in a way Slack has no field for.
    * a quantitative ``x``. Slack's x-axis is a list of categories rather than a
      scale, so a continuous x would be redrawn at equal spacing whatever its
      values -- which is the same chart only when the values happen to be evenly
      spaced. Nominal, ordinal and temporal x all map, and a spec with no
      declared ``x.type`` whose x values are all numbers is treated as the
      quantitative one it would be inferred to be.
    * a non-quantitative ``y``, which is Vega-Lite's horizontal bar chart.
      ``data_visualization`` draws vertically only, and turning it upright is
      the same refusal ``xychart-beta horizontal`` gets.
    * a gap: a series with no row for one of the categories, or two rows for the
      same series and category. Slack requires exactly one point per category
      (see ``MAX_CHART_CATEGORIES``), and neither inventing a zero nor picking
      one of two rows is this module's call to make.
    """
    try:
        spec = json.loads("\n".join(body))
    except ValueError:
        return None
    if not isinstance(spec, dict):
        return None
    if any(key in spec for key in VEGA_LITE_COMPOSITION_KEYS) or "transform" in spec:
        return None

    mark = spec.get("mark")
    if isinstance(mark, dict):
        mark = mark.get("type")
    if not isinstance(mark, str):
        return None
    kind = VEGA_LITE_MARKS.get(mark.strip().lower())
    if kind is None:
        return None

    rows = _vega_lite_rows(spec.get("data"))
    if rows is None:
        return None

    encoding = spec.get("encoding")
    if not isinstance(encoding, dict) or not encoding:
        return None
    # Read: the channels this module maps. Inert: channels that add a label or a
    # link and change nothing Slack draws. Anything else is declined, because an
    # unread channel that does change the picture would go missing in silence.
    read = {"x", "y", "color", "theta"}
    inert = {"tooltip", "description", "href", "key"}
    for channel, definition in encoding.items():
        if channel not in read and channel not in inert:
            return None
        if channel in inert:
            continue
        if not isinstance(definition, dict):
            return None
        if any(key in definition for key in VEGA_LITE_DERIVED_KEYS):
            return None
        if channel != "x" and "sort" in definition:
            # Read on ``x`` alone, where it is the category order Slack draws
            # left to right. On ``color`` it orders the series and on ``y`` or
            # ``theta`` it orders a scale, and Slack has a field for neither, so
            # honouring it there is not available and ignoring it would redraw
            # the spec in an order it asked against.
            return None

    chart = (
        _vega_lite_pie(encoding, rows)
        if kind == PIE_CHART
        else _vega_lite_series(kind, encoding, rows)
    )
    if chart is None:
        return None
    return _chart_block(chart, _vega_lite_title(spec.get("title")), preceding_prose)


def _vega_lite_rows(data: Any) -> list[dict[str, Any]] | None:
    """The inline rows of a spec's ``data``, or ``None`` for any other source.

    ``values`` and nothing beside it. A ``url``, a ``name`` pointing at a named
    source, a generated ``sequence`` and a ``format`` describing a parse this
    module does not do all decline by the same rule, which is why it is written
    as "no key but ``values``" rather than as a list of the ones to refuse.
    """
    if not isinstance(data, dict) or set(data) != {"values"}:
        return None
    values = data.get("values")
    if not isinstance(values, list) or not values:
        return None
    if not all(isinstance(row, dict) for row in values):
        return None
    return values


def _vega_lite_title(title: Any) -> str:
    """A spec's title, whichever of its three spellings it used.

    A bare string, a title object's ``text``, or that ``text`` as the list of
    lines Vega-Lite allows -- joined with a space, because Slack's title is one
    line and the alternative is dropping every line after the first.
    """
    if isinstance(title, str):
        return title
    if isinstance(title, dict):
        text = title.get("text")
        if isinstance(text, str):
            return text
        if isinstance(text, list) and all(isinstance(part, str) for part in text):
            return " ".join(text)
    return ""


def _vega_lite_label(definition: dict[str, Any]) -> str:
    """The axis title a channel asks for, which Vega-Lite defaults to its field.

    An explicit ``title`` wins, including ``null``, which is Vega-Lite's way of
    saying "no title" and is honoured as one rather than falling back to the
    field name the author asked to hide.
    """
    if "title" in definition:
        title = definition["title"]
        return title if isinstance(title, str) else ""
    field = definition.get("field")
    return field if isinstance(field, str) else ""


def _vega_lite_value(cell: Any) -> float | None:
    """One cell as a chart value, or ``None`` when it is not a number.

    ``bool`` is excluded explicitly: Python makes it a subclass of ``int``, so
    ``True`` would otherwise plot as 1 and a column of flags would come back as
    a chart of ones and zeroes that nobody wrote.
    """
    if isinstance(cell, bool) or not isinstance(cell, (int, float)):
        return None
    return float(cell)


def _vega_lite_key(cell: Any) -> str | None:
    """One cell as a category or series name, or ``None`` when it cannot be one.

    A number is accepted and rendered as written, because a year or a quarter is
    a perfectly good category label. ``bool`` is excluded for the reason above,
    and ``None`` and nested structures because neither names anything.
    """
    if isinstance(cell, bool) or not isinstance(cell, (str, int, float)):
        return None
    text = str(cell).strip()
    return text or None


def _vega_lite_pie(
    encoding: dict[str, Any], rows: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """A pie chart from an ``arc`` mark: ``theta`` is the value, ``color`` the label.

    Both channels are required. An ``arc`` with no ``theta`` has no value to
    size a slice by, and one with no ``color`` has no way to tell the slices
    apart -- Slack's segment holds a label and there is nowhere else to get
    one.
    """
    theta = encoding.get("theta")
    colour = encoding.get("color")
    if not isinstance(theta, dict) or not isinstance(colour, dict):
        return None
    value_field = theta.get("field")
    label_field = colour.get("field")
    if not isinstance(value_field, str) or not isinstance(label_field, str):
        return None

    segments: list[dict[str, Any]] = []
    for row in rows:
        if value_field not in row or label_field not in row:
            return None
        value = _vega_lite_value(row[value_field])
        label = _vega_lite_key(row[label_field])
        # Slack sizes a slice as its share of the total, so a slice of zero or
        # less has no share to draw.
        if value is None or value <= 0 or label is None:
            return None
        segments.append(
            {
                "label": _clamp(label, MAX_CHART_LABEL_LENGTH),
                "value": _chart_number(value),
            }
        )
    return _pie_chart(segments)


def _vega_lite_category_order(
    x_channel: dict[str, Any],
) -> tuple[bool, list[str] | None]:
    """Read an ``x`` channel's ``sort`` as the order its categories are drawn in.

    Returns ``(readable, order)``. ``order`` is ``None`` when the channel asked
    for nothing -- no ``sort`` key, or ``sort: null``, which is Vega-Lite for
    "leave the rows in the order they arrived" and is already what Slack draws.

    Only an explicit array is read. Such an array *is* the answer:
    ``axis_config.categories`` is a list of labels left to right, so honouring it
    costs a reordering and no interpretation, and a weekday chart written with
    ``"sort": ["Mon", "Tue", ...]`` -- which is how a chart of days gets written
    -- renders instead of showing its own source.

    Every other spelling is refused. ``"ascending"``, ``"-y"`` and a sort object
    keyed on another field all order the categories by something that has to be
    computed from the data first, and a computed order is declined for the same
    reason a computed value is: the alternative is drawing a chart that
    contradicts its own spec.

    Duplicates in the array are dropped rather than refused. Vega-Lite reads the
    first occurrence and so does this, and an author who repeated a label still
    said unambiguously where it goes.
    """
    if "sort" not in x_channel:
        return True, None
    sort = x_channel["sort"]
    if sort is None:
        return True, None
    if not isinstance(sort, list):
        return False, None
    order: list[str] = []
    for entry in sort:
        label = _vega_lite_key(entry)
        if label is None:
            return False, None
        if label not in order:
            order.append(label)
    return True, order


def _vega_lite_series(
    kind: str, encoding: dict[str, Any], rows: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """A bar, area or line chart from ``x``, ``y`` and an optional ``color``.

    Categories are the distinct ``x`` values in the order they first appear,
    which is Slack's left-to-right order and Vega-Lite's own default for a
    discrete axis with no ``sort``; an ``x`` that names an explicit order is
    reordered to it instead. Series are the distinct ``color`` values in
    the same first-seen order, or one series named for the y channel when there
    is no ``color`` -- or when the ``color`` names the same field as ``x``, which
    is the same thing said twice and is dropped rather than declined.
    """
    x_channel = encoding.get("x")
    y_channel = encoding.get("y")
    if not isinstance(x_channel, dict) or not isinstance(y_channel, dict):
        return None
    x_field = x_channel.get("field")
    y_field = y_channel.get("field")
    if not isinstance(x_field, str) or not isinstance(y_field, str):
        return None

    x_type = x_channel.get("type")
    if x_type is not None and not isinstance(x_type, str):
        return None
    if isinstance(x_type, str) and x_type.strip().lower() == "quantitative":
        return None
    y_type = y_channel.get("type")
    if y_type is not None and not isinstance(y_type, str):
        return None
    if isinstance(y_type, str) and y_type.strip().lower() != "quantitative":
        return None

    sortable, category_order = _vega_lite_category_order(x_channel)
    if not sortable:
        return None

    colour = encoding.get("color")
    series_field: str | None = None
    if colour is not None:
        if not isinstance(colour, dict):
            return None
        series_field = colour.get("field")
        if not isinstance(series_field, str):
            return None
        if series_field == x_field:
            # ``color`` on the same field as ``x`` is decoration: every category
            # is its own series of exactly one point, so the grouping says
            # nothing the x-axis does not. Vega-Lite draws it as a bar chart in n
            # colours; Slack has no per-category colour, and mapping it literally
            # asks ``data_visualization`` for n series each missing n-1 of the n
            # categories -- a grid that is all gaps, which is refused. Dropping
            # the channel draws the chart in one colour instead.
            #
            # The only redundant encoding read this way, because it is decidable
            # by equality of two field names. Guessing at intent for any other
            # pair would be the approximation the rest of this module refuses.
            series_field = None

    default_name = _vega_lite_label(y_channel) or y_field
    categories: list[str] = []
    order: list[str] = []
    grouped: dict[str, dict[str, float]] = {}
    x_all_numeric = True

    for row in rows:
        if x_field not in row or y_field not in row:
            return None
        raw_x = row[x_field]
        if not isinstance(raw_x, (int, float)) or isinstance(raw_x, bool):
            x_all_numeric = False
        label = _vega_lite_key(raw_x)
        value = _vega_lite_value(row[y_field])
        if label is None or value is None:
            return None

        if series_field is None:
            name = default_name
        else:
            if series_field not in row:
                return None
            name = _vega_lite_key(row[series_field])
            if name is None:
                return None

        if label not in categories:
            categories.append(label)
        if name not in grouped:
            grouped[name] = {}
            order.append(name)
        if label in grouped[name]:
            # Two rows for one series and category. Vega-Lite would aggregate
            # them; without a declared aggregate there is no saying which.
            return None
        grouped[name][label] = value

    # An undeclared x whose every value is a number is the quantitative channel
    # Vega-Lite would infer, and it is declined for the reason a declared one is.
    if x_type is None and x_all_numeric:
        return None
    if category_order is not None:
        # Labels the sort names but the data does not hold are dropped: an author
        # listing a full week for four days of rows meant those four days in that
        # order, and Vega-Lite draws no empty category for the fifth either.
        wanted = [label for label in category_order if label in categories]
        if len(wanted) != len(categories):
            # The other direction is not recoverable. Vega-Lite appends the
            # categories a sort leaves out, in an order of its own; choosing one
            # here would be inventing the part of the order the spec withheld.
            return None
        categories = wanted
    if any(len(grouped[name]) != len(categories) for name in order):
        return None

    return _series_chart(
        kind,
        order,
        categories,
        [[grouped[name][label] for label in categories] for name in order],
        x_label=_vega_lite_label(x_channel),
        y_label=_vega_lite_label(y_channel),
    )


def _is_interactive(value: Any) -> bool:
    """True when anything below *value* posts an interaction back to the app.

    One recursive walk rather than a field-by-field schema, because part of what
    is being guarded against is the field this module has not heard of yet. It
    is looking for either of two things at every level:

    * an ``action_id``, wherever it sits. A button, a select, an overflow and an
      accessory all hold one, and nothing this module builds for itself does.
    * a ``type`` naming an interactive block or element. Slack invents an
      ``action_id`` for an element that omits one and delivers the interaction
      regardless, so the field alone would let a bare button through.

    A false positive costs an author their fence and shows them their own source,
    which is the failure worth having on this side of the check.
    """
    if isinstance(value, dict):
        if any(str(key).lower() == INTERACTIVE_FIELD for key in value):
            return True
        kind = value.get("type")
        if isinstance(kind, str) and kind.strip().lower() in INTERACTIVE_TYPES:
            return True
        return any(_is_interactive(item) for item in value.values())
    if isinstance(value, list):
        return any(_is_interactive(item) for item in value)
    return False


def _parse_table(lines: list[str], start: int) -> tuple[_Table | None, int]:
    """Parse the table beginning at ``lines[start]``, if one does.

    Returns ``(None, 0)`` for anything that is not a table, which is what makes a
    malformed one degrade rather than raise: a header row with no delimiter under
    it, a delimiter whose width disagrees with the header, or a stray pipe in a
    sentence all simply stay prose.

    A ragged body row is not malformed enough to throw the table away -- models
    drop and duplicate separators routinely -- so it is padded with empty cells or
    truncated to the header's width. That loses nothing a text rendering would
    have preserved: the row was already misaligned.
    """
    header_line = lines[start]
    if "|" not in header_line or start + 1 >= len(lines):
        return None, 0

    header = _split_row(header_line)
    if not header:
        return None, 0

    delimiter_cells = _split_row(lines[start + 1])
    if len(delimiter_cells) != len(header) or not delimiter_cells:
        return None, 0
    if not all(_DELIMITER_CELL_RE.match(cell) for cell in delimiter_cells):
        return None, 0

    alignments = [_alignment(cell) for cell in delimiter_cells]
    raw_rows = [list(header)]
    rows = [[_plain_text(cell) for cell in header]]
    index = start + 2
    while index < len(lines):
        line = lines[index]
        if "|" not in line or not line.strip():
            break
        cells = _split_row(line)
        if not cells:
            break
        cells = (cells + [""] * len(header))[: len(header)]
        raw_rows.append(list(cells))
        rows.append([_plain_text(cell) for cell in cells])
        index += 1

    return _Table(rows, alignments, raw_rows), index - start


def _split_row(line: str) -> list[str]:
    """Split one table row into cells.

    Scanned rather than split on a regex because three kinds of pipe are not
    separators: the ``\\|`` GFM escapes, the one inside a ``<url|label>`` link --
    which is exactly what the connector's normaliser produces from a Markdown
    link, so a table of links would otherwise gain a phantom column per row --
    and any pipe inside an inline code span.

    The outer pipes GFM allows are optional, so they are stripped before the
    split rather than producing empty leading and trailing cells.
    """
    stripped = line.strip()
    if not stripped or "|" not in stripped:
        return []
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]

    cells: list[str] = []
    current: list[str] = []
    in_link = False
    in_code = False
    index = 0
    while index < len(stripped):
        character = stripped[index]
        if character == "\\" and index + 1 < len(stripped):
            current.append(stripped[index : index + 2])
            index += 2
            continue
        if character == "`":
            in_code = not in_code
        elif not in_code and character == "<":
            in_link = True
        elif not in_code and character == ">":
            in_link = False
        elif character == "|" and not in_link and not in_code:
            cells.append("".join(current))
            current = []
            index += 1
            continue
        current.append(character)
        index += 1
    cells.append("".join(current))
    return [cell.strip() for cell in cells]


def _alignment(delimiter_cell: str) -> str:
    if delimiter_cell.startswith(":") and delimiter_cell.endswith(":"):
        return "center"
    if delimiter_cell.endswith(":"):
        return "right"
    return "left"


def _plain_text(cell: str) -> str:
    """Reduce one cell to what a ``raw_text`` cell can actually show.

    Table cells are the one place the mrkdwn the rest of the message keeps cannot
    be used: ``raw_text`` renders characters, so leaving the markup in would put
    literal asterisks in the grid. Losing bold and code markers is a fair trade
    for that. Losing a link's destination is not, which is why a cell containing
    one is rendered as ``rich_text`` instead -- see ``_cell``.
    """
    text = _LINK_RE.sub(lambda match: match.group("label") or match.group("url"), cell)
    return _reduce_markup(text)


def _reduce_inline(text: str) -> str:
    """Strip the inline markup a cell cannot render, leaving the words.

    Whitespace is left alone: between a text run and the link beside it, it is
    the only thing keeping the two apart.
    """
    text = _CODE_SPAN_RE.sub(lambda match: match.group("body"), text)
    # Twice, so "*_both_*" loses both markers; a third pass buys nothing.
    text = _EMPHASIS_RE.sub(lambda match: match.group("body"), text)
    text = _EMPHASIS_RE.sub(lambda match: match.group("body"), text)
    return text.replace("\\|", "|")


def _reduce_markup(text: str) -> str:
    """``_reduce_inline`` for a value that stands on its own, so also trimmed."""
    return _reduce_inline(text).strip()


def _cell(raw: str, plain: str) -> dict[str, Any]:
    """Render one cell, keeping a link's destination when it has one.

    ``raw_text`` can only hold characters, so a cell holding
    ``<https://…|jiuwenswarm#1426>`` would arrive as the label alone and the
    destination would be gone -- and a table of pull requests, incidents or
    search results is exactly the content that motivates a table in the first
    place. A ``rich_text`` cell can hold the link, so cells that have one use
    it and every other cell stays ``raw_text``.

    Deliberately not the general mrkdwn-to-rich_text parser this module declines
    to write: bold and code inside a cell are still reduced to their words. The
    only thing recovered is the destination, because it is the only thing whose
    loss cannot be seen in the result.
    """
    if not _link_matches(raw):
        return {"type": "raw_text", "text": plain}
    elements = _rich_text_elements(raw)
    if not elements:
        return {"type": "raw_text", "text": plain}
    return {
        "type": "rich_text",
        "elements": [{"type": "rich_text_section", "elements": elements}],
    }


def _link_matches(raw: str) -> list[re.Match[str]]:
    """The ``<url|label>`` tokens in *raw* that are actually links.

    ``_LINK_RE`` also matches the other things Slack wraps in angle brackets --
    ``<@U01ABCDEF>`` mentions, ``<#C01ABCDEF|general>`` channel references,
    ``<!here>`` -- which the normaliser upstream produces and must not be turned
    into hyperlinks to a nonexistent scheme. Requiring a scheme separates them:
    a real link is always ``https:`` or similar, and none of the Slack tokens
    hold a colon in that position.
    """
    return [
        match
        for match in _LINK_RE.finditer(raw)
        if _URL_SCHEME_RE.match(match.group("url") or "")
    ]


def _rich_text_elements(raw: str) -> list[dict[str, Any]]:
    """Split *raw* into alternating text runs and link elements.

    The cell as a whole is trimmed, exactly as ``_plain_text`` trims it, but the
    runs between links keep their interior spacing.
    """
    elements: list[dict[str, Any]] = []
    position = 0
    for match in _link_matches(raw):
        _append_text(elements, raw[position : match.start()])
        url = match.group("url") or ""
        label = _reduce_markup(match.group("label") or "")
        link: dict[str, Any] = {"type": "link", "url": url}
        # Slack shows the url itself when a link holds no text, which is what
        # a bare <url> already meant, so the field is omitted rather than
        # repeating the url back at it.
        if label and label != url:
            link["text"] = label
        elements.append(link)
        position = match.end()
    _append_text(elements, raw[position:])
    return _trim_ends(elements)


def _append_text(elements: list[dict[str, Any]], run: str) -> None:
    """Append one text run, reduced, unless nothing is left of it."""
    text = _reduce_inline(run)
    if text:
        elements.append({"type": "text", "text": text})


def _trim_ends(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim the outer edges of the cell, dropping runs that empty out."""
    if elements and elements[0]["type"] == "text":
        elements[0]["text"] = elements[0]["text"].lstrip()
    if elements and elements[-1]["type"] == "text":
        elements[-1]["text"] = elements[-1]["text"].rstrip()
    return [
        element
        for element in elements
        if element["type"] != "text" or element["text"]
    ]


def _caption_for(prose: str | None, fallback: str = DEFAULT_TABLE_CAPTION) -> str:
    """The caption for a table, or the title for a chart, sitting under *prose*.

    ``data_table`` requires a caption and Markdown has nowhere to write one, so
    it is taken from the heading the table already sits under. Reports put one
    there as a matter of course -- ``*Newly tracked*``, ``*Changed*``,
    ``*Roster (1/3)*`` -- and it is the line a reader would call the table's
    name if asked.

    Only the last non-empty line above the table is considered, and only when it
    is a heading or a line that is nothing but bold. An ordinary sentence
    introducing a table is not its name, and lifting one into the caption would
    read as a mis-rendering rather than as a label. Everything else, including a
    table that opens the message, falls back to the generic caption.
    """
    for line in reversed((prose or "").splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        heading = _HEADING_RE.match(stripped) or _BOLD_LINE_RE.match(stripped)
        if heading is None:
            return fallback
        text = _reduce_markup(heading.group("text")).rstrip(":").strip()
        return _clamp(text, MAX_TABLE_CAPTION_LENGTH) or fallback
    return fallback


def _clamp(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"


def _table_block(
    table: _Table,
    *,
    caption: str = DEFAULT_TABLE_CAPTION,
    force_data_table: bool = False,
) -> dict[str, Any] | None:
    """Build one table block, or ``None`` when it will not fit Slack's shape.

    Which of the two block types it builds is the caller's, since it is the
    operator's ``render_tables`` and an author's marker that decide it and
    neither is visible from here. What is decided here is whether the block can
    be built at all: a table wider than Slack's column ceiling, or -- for the
    plain block, whose budget is half the other one's -- longer than its row
    ceiling, has no rendering, and ``None`` declines the whole message back to
    text rather than sending a table with rows missing from it.
    """
    if not table.rows:
        return None
    if len(table.rows[0]) > MAX_TABLE_COLUMNS:
        return None

    if force_data_table:
        return _data_table_block(table, caption)

    if len(table.rows) > MAX_PLAIN_TABLE_ROWS:
        return None
    block: dict[str, Any] = {"type": "table", "rows": _cells(table)}
    # Omitted entirely when every column is left aligned, which is the default:
    # sending the default back is noise in the payload and in the tests.
    if any(alignment != "left" for alignment in table.alignments):
        block["column_settings"] = [
            {"align": alignment} for alignment in table.alignments
        ]
    return block


def _data_table_block(table: _Table, caption: str) -> dict[str, Any] | None:
    """Build one ``data_table``, which pages, sorts and filters in the client.

    Three differences from ``table`` and each of them is deliberate.

    ``caption`` is required and is a plain string rather than a text object --
    Slack rejects the object form, which is the shape every other label in Block
    Kit takes.

    Numeric columns are emitted as ``raw_number`` so the client sorts them as
    numbers. See ``_numeric_columns`` for why that is decided per column.

    ``column_settings`` is **not** emitted. ``table`` accepts it; whether
    ``data_table`` does is undocumented and unprobed. An unknown field risks
    ``invalid_blocks``, which costs the whole message its rendering, while
    omitting it costs the alignment of the rare generated table that asks for
    one.
    """
    if len(table.rows) > MAX_TABLE_ROWS:
        return None

    numeric = _numeric_columns(table)
    # rich_text is not a valid header cell type, so the header row is raw_text
    # whatever it holds -- a link in a heading loses its destination rather than
    # taking the message's rendering down with it.
    rows: list[list[dict[str, Any]]] = [
        [{"type": "raw_text", "text": plain} for plain in table.rows[0]]
    ]
    for raw_row, row in zip(table.raw_rows[1:], table.rows[1:]):
        rows.append(
            [
                _number_cell(plain) if index in numeric else _cell(raw, plain)
                for index, (raw, plain) in enumerate(zip(raw_row, row))
            ]
        )
    return {
        "type": "data_table",
        "caption": caption,
        "rows": rows,
        "page_size": _page_size(len(table.rows) - 1),
    }


def _page_size(data_rows: int) -> int:
    """How many rows one page shows.

    Never Slack's default of 5, and never more rows than the table has. A
    three-row table gets a page of three and the pager never appears, so the
    block is drawn for its sorting and filtering rather than for paging nobody
    asked for. Only a table longer than ``DATA_TABLE_PAGE_SIZE`` is paged.
    """
    wanted = min(DATA_TABLE_PAGE_SIZE, data_rows)
    return max(MIN_DATA_TABLE_PAGE_SIZE, min(wanted, MAX_DATA_TABLE_PAGE_SIZE))


def _numeric_columns(table: _Table) -> set[int]:
    """The column indexes whose every body cell is a number.

    Decided per column, not per cell, because Slack sorts a column numerically
    only when every cell in it is ``raw_number``. A column with one stray
    ``raw_text`` cell sorts alphabetically, where ``10`` comes before ``9`` and
    the reader has no way to see why -- so a column that is not entirely numeric
    is left entirely textual, which at least sorts the way it looks.

    The header row is excluded: it is the column's name, not one of its values.
    A cell holding a link is excluded too, because the destination is worth
    more than the sort order and only ``rich_text`` can keep it.
    """
    if len(table.rows) < 2:
        return set()
    numeric: set[int] = set()
    for column in range(len(table.rows[0])):
        cells = [
            (row[column], raw[column])
            for row, raw in zip(table.rows[1:], table.raw_rows[1:])
            if column < len(row) and column < len(raw)
        ]
        if cells and all(
            _NUMBER_RE.match(plain) and not _link_matches(raw)
            for plain, raw in cells
        ):
            numeric.add(column)
    return numeric


def _number_cell(plain: str) -> dict[str, Any]:
    """One numeric cell.

    ``text`` is documented as optional and is not: a post holding
    ``raw_number`` cells without it is refused with ``missing required field:
    text``. It is also what keeps the cell readable -- ``value`` is what the
    column sorts on, ``text`` is what the reader sees, and the two differ
    wherever the cell was written with thousands separators.
    """
    return {"type": "raw_number", "value": _number_value(plain), "text": plain}


def _number_value(plain: str) -> int | float:
    bare = plain.replace(",", "")
    return float(bare) if "." in bare else int(bare)


def _cells(table: _Table) -> list[list[dict[str, Any]]]:
    return [
        [_cell(raw, plain) for raw, plain in zip(raw_row, row)]
        for raw_row, row in zip(table.raw_rows, table.rows)
    ]


def data_fallback_text(
    blocks: list[dict[str, Any]], *, already_in: str = ""
) -> str:
    """The data behind *blocks* as plain Markdown, or ``""`` when there is none.

    Used on the one path where a rendering was refused and the reader asked for
    data. What they wanted was the numbers, so the numbers are rebuilt from the
    block's own ``rows`` and ``series`` rather than the payload being printed at
    them: a ``data_table`` full of ``{"type": "raw_number", "value": …}`` says
    everything the reader needs and none of it legibly.

    ``already_in`` is the text the same message will hold. On the reply path
    that text is the chunk the table was parsed *out* of, so it still holds the
    pipe table verbatim and repeating it underneath would show the same rows
    twice. A block whose header cells all appear there is therefore dropped, and
    only a block the reader would otherwise never see is rebuilt -- a message
    whose ``text`` is a stub because the whole content was the chart.

    Blocks that hold no data of their own -- a ``section``, a ``context`` --
    contribute nothing here. Their text is mrkdwn already and is in the message.
    """
    pieces: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in ("table", "data_table"):
            piece = _rows_as_markdown(block)
        elif kind == "data_visualization":
            piece = _chart_as_markdown(block)
        else:
            continue
        if piece and not _already_shown(piece, already_in):
            pieces.append(piece)
    return "\n\n".join(pieces)


def _already_shown(piece: str, already_in: str) -> bool:
    """Whether *already_in* is showing the same data *piece* holds.

    Compared cell by cell rather than as whole strings, because the two are
    never byte-identical: the rebuilt form pads nothing and normalises spacing,
    while the source it came from is however the author typed it. Every
    non-empty cell appearing somewhere in the text is a weak test on purpose --
    the cost of a false positive is a reader losing rows they can see anyway
    higher up the same message, and the cost of a false negative is showing
    them twice.
    """
    if not already_in:
        return False
    cells = [
        cell.strip()
        for line in piece.splitlines()
        for cell in line.strip().strip("|").split("|")
    ]
    wanted = [cell for cell in cells if cell and set(cell) != {"-"}]
    return bool(wanted) and all(cell in already_in for cell in wanted)


def blockkit_fallback_summary(blocks: list[dict[str, Any]]) -> str:
    """A short, human fallback for *blocks*, for the one caller with nothing else.

    Used when a reply is nothing but a fence: there is no prose beside it for
    ``text`` to be, and the raw fence source is not a fallback, it is the
    defect this exists to fix. Slack's own client faces the same gap -- a GIF
    dropped in through the UI carries no author-written text either -- and
    answers it by deriving one from the attachment (``fallback: "shared a
    GIF"``) rather than showing the upload's JSON. This is that, for a
    hand-written fence.

    One source, not a digest of every block: a notification is a line, not
    the reply, and joining several blocks' text back together would rebuild
    the wall of text this function exists to avoid. So it stops at the first
    block that has something, checked in the order a reader would find most
    telling about what is on screen:

    1. an ``image`` block's ``title``
    2. an ``image`` block's ``alt_text`` -- required by Slack's own schema, so
       this is the field an image fence can never leave this with nothing
    3. a ``section`` block's ``text``
    4. a ``header`` block's ``text``

    A table or chart is not read here even though ``data_fallback_text``
    could rebuild one: this function only runs when that would still leave
    the message with no notification-length line to show, and a full pipe
    table is not one.

    ``""`` when none of the above is present -- an interactive-only or
    unrecognised payload -- which tells the caller to use its own generic
    string instead of an empty ``text``, which Slack refuses outright.
    """
    for field in ("title", "alt_text"):
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "image":
                continue
            text = _text_object(block.get(field))
            if text:
                return text
    for kind in ("section", "header"):
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != kind:
                continue
            text = _text_object(block.get("text"))
            if text:
                return text
    return ""


def _text_object(value: Any) -> str:
    """The plain string inside a Block Kit text composition object.

    Handles the shape Slack's schema actually requires (``{"type":
    "plain_text"/"mrkdwn", "text": ...}``) and a bare string, which is not
    valid Block Kit but costs nothing to accept here: the fence this reads
    was written by a model, not validated input, and a string in this
    position is a shape mistake worth reading past rather than one more
    reason to fall back to the generic notice.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("text") or "").strip()
    return ""


def _rows_as_markdown(block: dict[str, Any]) -> str:
    """One ``table`` or ``data_table`` back as a pipe table.

    The caption is kept as a heading above it where there is one: on a
    ``data_table`` it is required and is usually the only thing naming what the
    rows are.
    """
    rows = block.get("rows")
    if not isinstance(rows, list) or not rows:
        return ""
    grid = [
        [_cell_text(cell) for cell in row]
        for row in rows
        if isinstance(row, list)
    ]
    grid = [row for row in grid if row]
    if not grid:
        return ""
    width = max(len(row) for row in grid)
    lines = [
        "| " + " | ".join((row + [""] * width)[:width]) + " |" for row in grid
    ]
    lines.insert(1, "| " + " | ".join(["---"] * width) + " |")
    caption = str(block.get("caption") or "").strip()
    return f"*{caption}*\n" + "\n".join(lines) if caption else "\n".join(lines)


def _cell_text(cell: Any) -> str:
    """What one table cell says, whichever of the three cell shapes it is.

    ``rich_text`` is flattened back to ``<url|label>``, which is the mrkdwn the
    cell was built from and what the rest of the message is written in.
    """
    if not isinstance(cell, dict):
        return ""
    kind = cell.get("type")
    if kind in ("raw_text", "raw_number"):
        return str(cell.get("text") or "").strip()
    if kind == "rich_text":
        return "".join(_rich_text_text(cell.get("elements"))).strip()
    return ""


def _rich_text_text(value: Any) -> list[str]:
    """Every readable run under a ``rich_text`` cell, in order."""
    if isinstance(value, list):
        return [part for item in value for part in _rich_text_text(item)]
    if not isinstance(value, dict):
        return []
    if value.get("type") == "link":
        url = str(value.get("url") or "")
        label = str(value.get("text") or "")
        return [f"<{url}|{label}>" if label else f"<{url}>"]
    if value.get("type") == "text":
        return [str(value.get("text") or "")]
    return _rich_text_text(value.get("elements"))


def _chart_as_markdown(block: dict[str, Any]) -> str:
    """One ``data_visualization`` back as the table it was drawn from.

    A chart's data is a grid -- categories down, series across -- so it comes
    back as one, which is the form the reader can still read the numbers off.
    A pie has a single unnamed series and comes back as two columns.
    """
    chart = block.get("chart")
    if not isinstance(chart, dict):
        return ""
    title = str(block.get("title") or "").strip()
    if chart.get("type") == PIE_CHART:
        rows = [
            (str(item.get("label") or ""), item.get("value"))
            for item in chart.get("segments") or []
            if isinstance(item, dict)
        ]
        if not rows:
            return ""
        lines = ["| Label | Value |", "| --- | --- |"]
        lines += [f"| {label} | {value} |" for label, value in rows]
        return (f"*{title}*\n" if title else "") + "\n".join(lines)

    series = [item for item in chart.get("series") or [] if isinstance(item, dict)]
    if not series:
        return ""
    axis = chart.get("axis_config")
    categories = list((axis or {}).get("categories") or []) if isinstance(axis, dict) else []
    if not categories:
        categories = [
            str(point.get("label") or "")
            for point in series[0].get("data") or []
            if isinstance(point, dict)
        ]
    names = [str(item.get("name") or "") for item in series]
    values = [
        {
            str(point.get("label") or ""): point.get("value")
            for point in item.get("data") or []
            if isinstance(point, dict)
        }
        for item in series
    ]
    lines = [
        "| " + " | ".join([""] + names) + " |",
        "| " + " | ".join(["---"] * (len(names) + 1)) + " |",
    ]
    for category in categories:
        cells = [str(mapping.get(category, "")) for mapping in values]
        lines.append("| " + " | ".join([str(category)] + cells) + " |")
    return (f"*{title}*\n" if title else "") + "\n".join(lines)


def section_blocks(prose: str) -> list[dict[str, Any]]:
    """Prose as ``section`` blocks, for a surface that has no plain-text field.

    A message holds a ``text`` field beside its blocks, so a caller whose text
    the renderer declined to turn into blocks has somewhere to put it and this
    is not needed. A published view -- an App Home tab -- holds blocks and
    nothing else, so prose that reaches it has to be wrapped before it can be
    published at all.

    A public name for the wrapping the renderer already does to every run of
    prose it meets, rather than a second implementation of it. The section text
    ceiling and the rule for where a long run is cut are decided in one place,
    so a paragraph reaching a view is cut exactly where the same paragraph
    reaching a channel would be.
    """
    return _section_blocks(prose)


def _section_blocks(prose: str) -> list[dict[str, Any]]:
    """Wrap a prose run in as many ``section`` blocks as its length requires.

    Split on line boundaries so a paragraph is never cut mid-sentence, and only
    hard-split a single line that is itself over the limit -- at that size there
    is no boundary left to prefer.
    """
    blocks: list[dict[str, Any]] = []
    for piece in _split_to_limit(prose.strip("\n"), MAX_SECTION_TEXT_LENGTH):
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": piece}}
        )
    return blocks


def _split_to_limit(text: str, limit: int) -> list[str]:
    if not text.strip():
        return []
    if len(text) <= limit:
        return [text]

    pieces: list[str] = []
    current: list[str] = []
    length = 0
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                pieces.append("\n".join(current))
                current, length = [], 0
            pieces.append(line[:limit])
            line = line[limit:]
        # +1 for the newline that rejoins this line to the ones before it.
        if current and length + 1 + len(line) > limit:
            pieces.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += (1 if length else 0) + len(line)
    if current:
        pieces.append("\n".join(current))
    return [piece for piece in pieces if piece.strip()]
