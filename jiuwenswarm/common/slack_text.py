# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Markdown as Slack mrkdwn, and one answer cut into messages Slack will take.

Two passes that every outbound Slack message goes through before Block Kit is
even considered, lifted out of the connector so that they are reachable from
both processes that write to Slack.

The connector is one of those processes and was the only one. The other is the
agent runtime, where ``post_message`` lets a model write into a conversation it
names; the promise that tool makes is that the text goes through *the same*
renderer as an ordinary reply, so that a fenced mermaid diagram, a Markdown
table and a bold heading behave identically inside and outside the tool. A
second implementation of either pass would make that promise false within a
release or two, in the direction that is hardest to notice: the same text
rendering two ways depending on which path posted it.

The runtime cannot import the connector to reach them. ``slack_connect``
imports ``slack_bolt`` and the whole channel manager, and the runtime has
neither, which is the same reason :mod:`jiuwenswarm.common.slack_blocks` sits
beside this module rather than beside the connector. Nothing here imports
anything outside the standard library.

The order the two passes run in is fixed and is not an implementation detail.
:func:`normalize_slack_mrkdwn` converts a narrow Markdown subset -- headings,
bold, bullets, links -- into Slack's mrkdwn. :func:`split_text` then cuts the
result into pieces Slack will store. Normalising second would cut a ``<url|
label>`` token in half, and rendering blocks before splitting would build a
table that no longer fits the message it lands in.
"""

from __future__ import annotations

import re

# Slack accepts roughly 40,000 characters per chat.postMessage text field.
# Leave headroom rather than fragmenting long answers ten times over.
MAX_SLACK_TEXT_LENGTH = 38000
# An edit is held to a tenth of what a post is, and the two fail differently:
# chat.postMessage truncates past its ceiling, while chat.update rejects the
# whole call with msg_too_long -- "Message text is too long. The text field
# cannot exceed 4,000 characters" (docs.slack.dev/reference/methods/chat.update).
# Every write made through chat.update is bounded by this rather than by the
# posting limit above.
MAX_SLACK_UPDATE_TEXT_LENGTH = 4000

# Inline code and Slack's own angle-bracket tokens, which are held out of the
# line rewriter: a ``*`` inside a code span is a literal asterisk, and a
# ``<url|label>`` already is mrkdwn and must not be read as Markdown again.
_INLINE_CODE_OR_SLACK_TOKEN_RE = re.compile(
    r"(?P<code>(?P<ticks>`+)[^\n]*?(?P=ticks))|(?P<slack><[^>\n]+>)"
)
_MARKDOWN_BOLD_RE = re.compile(r"(?<!\*)\*\*(?=\S)(.+?)(?<=\S)\*\*(?!\*)")
_MARKDOWN_HEADING_RE = re.compile(r"^(?P<indent>[ \t]{0,3})#{1,6}[ \t]+(?P<title>.+?)$")
_MARKDOWN_CLOSING_HASHES_RE = re.compile(r"[ \t]+#+[ \t]*$")
_MARKDOWN_LABELED_BULLET_RE = re.compile(
    r"^(?P<indent>[ \t]*)[-+*][ \t]+\*"
    r"(?P<label>Fact|Inference(?:\s*\([^)]*\))?|"
    r"Recommendation|Proposal|Action Item)\*:[ \t]*(?P<body>.*)$",
    re.IGNORECASE,
)
_MARKDOWN_BULLET_RE = re.compile(r"^(?P<indent>[ \t]*)[-+*][ \t]+(?P<body>.*)$")
# Public, unlike the rest: the connector scans for fenced regions in a second
# place, to decide whether a chunk is nothing but a fence, and that scan has to
# agree with the one the normaliser runs. Two copies of a fence pattern would
# eventually disagree about a fence of four backticks.
MARKDOWN_FENCE_OPEN_RE = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
MARKDOWN_FENCE_CLOSE_RE = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})[ \t]*$")


def convert_markdown_links(content: str) -> str:
    """``[label](url)`` as Slack's ``<url|label>``, and everything else intact.

    Hand-written rather than a regular expression because a URL may hold
    balanced parentheses, which is what the depth counter below tracks. A link
    that fails any of the checks is left exactly as it was written: a malformed
    link reaching a reader as the text somebody typed is legible, while one
    half-converted is neither a link nor prose.
    """
    converted: list[str] = []
    index = 0
    while index < len(content):
        if content[index] != "[":
            converted.append(content[index])
            index += 1
            continue

        label_end = content.find("]", index + 1)
        if label_end < 0:
            converted.append(content[index:])
            break
        if label_end + 1 >= len(content) or content[label_end + 1] != "(":
            converted.append(content[index])
            index += 1
            continue

        label = content[index + 1 : label_end]
        url_start = label_end + 2
        remaining = content[url_start:].lower()
        invalid_label = not label or any(character in label for character in "[]<>|")
        if invalid_label or not remaining.startswith(("http://", "https://")):
            converted.append(content[index])
            index += 1
            continue

        depth = 0
        cursor = url_start
        link_end = -1
        invalid = False
        while cursor < len(content):
            character = content[cursor]
            if character.isspace() or character in "<>|":
                invalid = True
                break
            if character == "(":
                depth += 1
            elif character == ")":
                if depth == 0:
                    link_end = cursor
                    break
                depth -= 1
            cursor += 1

        if invalid or link_end < 0:
            converted.append(content[index])
            index += 1
            continue

        url = content[url_start:link_end]
        converted.append(f"<{url}|{label}>")
        index = link_end + 1

    return "".join(converted)


def normalize_markdown_line(content: str) -> str:
    """One line of Markdown as mrkdwn, with code spans and tokens held out.

    Inline code and ``<…>`` tokens are lifted into placeholders before any rule
    runs and put back afterwards, so nothing inside them is rewritten. The
    placeholder holds a NUL, which no reply can contain.
    """
    protected: list[tuple[str, str]] = []

    def preserve(match: "re.Match[str]") -> str:
        placeholder = f"\x00JWS{len(protected)}\x00"
        protected.append((placeholder, match.group(0)))
        return placeholder

    working = _INLINE_CODE_OR_SLACK_TOKEN_RE.sub(preserve, content)
    heading = _MARKDOWN_HEADING_RE.match(working)
    if heading:
        title = _MARKDOWN_CLOSING_HASHES_RE.sub("", heading.group("title")).strip()
        title = convert_markdown_links(title)
        title = _MARKDOWN_BOLD_RE.sub(r"\1", title)
        working = f"{heading.group('indent')}*{title}*"
    else:
        working = convert_markdown_links(working)
        working = _MARKDOWN_BOLD_RE.sub(r"*\1*", working)
        labeled_bullet = _MARKDOWN_LABELED_BULLET_RE.match(working)
        if labeled_bullet:
            indent = labeled_bullet.group("indent")
            label = labeled_bullet.group("label")
            message = labeled_bullet.group("body")
            working = f"{indent}• *{label}:* {message}"
        else:
            bullet = _MARKDOWN_BULLET_RE.match(working)
            if bullet:
                working = f"{bullet.group('indent')}• {bullet.group('body')}"

    for placeholder, value in protected:
        working = working.replace(placeholder, value)
    return working


def normalize_slack_mrkdwn(content: str) -> str:
    """Convert a narrow Markdown subset to Slack mrkdwn safely.

    A fenced region is copied out untouched, opening fence and closing fence
    included. Inside a fence every character is content: a ``#`` is a comment,
    a ``*`` is multiplication, and rewriting either would corrupt code a reader
    is about to copy.
    """
    if not content:
        return ""

    normalized_lines: list[str] = []
    fence_char = ""
    fence_length = 0
    for line in content.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body) :]

        if fence_char:
            normalized_lines.append(line)
            closing_fence = MARKDOWN_FENCE_CLOSE_RE.match(body)
            if closing_fence:
                marker = closing_fence.group("fence")
                if marker[0] == fence_char and len(marker) >= fence_length:
                    fence_char = ""
                    fence_length = 0
            continue

        opening_fence = MARKDOWN_FENCE_OPEN_RE.match(body)
        if opening_fence:
            marker = opening_fence.group("fence")
            fence_char = marker[0]
            fence_length = len(marker)
            normalized_lines.append(line)
            continue

        normalized_lines.append(normalize_markdown_line(body) + ending)
    return "".join(normalized_lines)


def preferred_split_index(content: str, limit: int) -> int:
    """Where to cut *content* so the cut lands somewhere a reader would accept.

    A paragraph break is looked for first, then a line break, then a space, each
    searched backwards from the limit and no further back than half of it -- a
    cut at a tenth of the limit would fragment an answer into far more messages
    than it needs. With none of the three available the limit itself is the
    answer, which cuts mid-word rather than losing the tail.

    A cut inside a ``<…>`` token is moved to before the token opens. Half of
    ``<https://example.com|the report>`` is not a link in either message, and
    the halves do not rejoin: they are two separate Slack messages.
    """
    lower_bound = max(1, limit // 2)
    for separator in ("\n\n", "\n", " "):
        split_at = content.rfind(separator, lower_bound, limit + 1)
        if split_at < 0:
            continue

        last_open = content.rfind("<", 0, split_at)
        last_close = content.rfind(">", 0, split_at)
        if last_open > last_close and last_open >= lower_bound:
            split_at = last_open
        if split_at > 0:
            return split_at
    return limit


def split_text(content: str, first_limit: int = 0) -> list[str]:
    """Split *content* into chunks Slack will accept.

    ``first_limit`` lowers the ceiling for the first chunk only, for the one
    caller whose first chunk is written with chat.update rather than
    chat.postMessage. The two methods do not share a limit, so a reply that
    closes a stream has to be cut where the edit will take it. Left at zero
    every chunk uses the posting limit, which is the behaviour every
    non-streaming caller wants.
    """
    remaining = content.strip()
    chunks: list[str] = []
    while True:
        limit = first_limit if first_limit and not chunks else MAX_SLACK_TEXT_LENGTH
        if len(remaining) <= limit:
            break
        split_at = preferred_split_index(remaining, limit)
        chunk = remaining[:split_at].rstrip()
        if not chunk:
            split_at = limit
            chunk = remaining[:split_at]
        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


__all__ = [
    "MARKDOWN_FENCE_CLOSE_RE",
    "MARKDOWN_FENCE_OPEN_RE",
    "MAX_SLACK_TEXT_LENGTH",
    "MAX_SLACK_UPDATE_TEXT_LENGTH",
    "convert_markdown_links",
    "normalize_markdown_line",
    "normalize_slack_mrkdwn",
    "preferred_split_index",
    "split_text",
]
