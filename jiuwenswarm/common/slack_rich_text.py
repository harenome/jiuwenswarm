"""The text a nested Slack payload holds, for the two readers that need it.

Slack puts reader-facing content in three shapes beside a message's ``text``:
Block Kit ``blocks``, the legacy ``attachments`` array, and the blocks an
attachment nests inside one of its own entries. The inbound connector and the
history tool both have to read them, and a second copy of this walk would drift
from the first -- which shows up as one Slack message reading as empty on the
path that dispatches turns and full on the path that reports history.

Placed here rather than beside either reader. ``slack_connect`` pulls
``slack_bolt`` into anything that imports it, so the history tool cannot import
from the connector; and the connector should not reach into an agent tool for a
pure function.
"""

from __future__ import annotations

from typing import Any, Mapping

# How far into a Block Kit payload the text walk goes, and how many strings it
# takes. Slack nests sections inside elements inside blocks, and an attachment
# may hold blocks of its own, so the depth is generous; both bounds are there so
# that a payload built to be enormous costs a truncated read rather than the
# scan.
MAX_RICH_TEXT_DEPTH = 12
MAX_RICH_TEXT_STRINGS = 200

# The keys a Block Kit payload puts reader-facing text under. An allow-list
# rather than a deny-list: the same payload holds block ids, action ids and the
# vocabulary Slack renders with -- "mrkdwn", "section", "button" -- and a walk
# that took every string would file all of it as though somebody had written
# it. ``url`` earns a place because a link element keeps its target there and
# nowhere else; ``image_url`` does not, because an image is not text.
RICH_TEXT_KEYS = frozenset(
    {
        "text",
        "title",
        "pretext",
        "footer",
        "label",
        "placeholder",
        "alt_text",
        "fallback",
        "name",
        "url",
    }
)


def walk_strings_bounded(
    value: Any, keys: "frozenset[str] | None"
) -> "tuple[list[str], bool]":
    """The strings a nested Slack payload holds, and whether a bound cut them.

    ``keys`` names which mapping keys hold text; ``None`` takes every string.
    The walk itself is deliberately structure-blind. Slack adds block and
    element types faster than any list of them here would track, and one this
    module has not heard of would otherwise contribute nothing at all.

    Repeats are dropped. Slack's own payloads restate the same sentence
    routinely -- an attachment's ``fallback`` is usually its ``text``, and a
    button's label is often its accessible name -- and a record that said
    everything twice would spend the reader's budget saying it.

    The second value is what the plain ``walk_strings`` cannot report. Both
    bounds cut the list of strings rather than any one string, so there is
    nowhere in the answer itself for an ellipsis or a marker to go, and a
    caller that showed only the list would present part of a payload as the
    whole of it. It says a bound stopped the walk before it had looked at
    everything, which is a claim about the walk and not a count of what was
    missed: what an unvisited branch would have contributed is exactly what is
    not known here.
    """
    found: list[str] = []
    seen: set[str] = set()
    bounded = False

    def keep(text: str) -> None:
        stripped = text.strip()
        if stripped and stripped not in seen:
            seen.add(stripped)
            found.append(stripped)

    def walk(node: Any, depth: int) -> None:
        nonlocal bounded
        if depth > MAX_RICH_TEXT_DEPTH:
            # An empty container below the depth bound holds nothing to lose,
            # so the bound biting is only worth reporting where there was
            # something under it.
            if isinstance(node, str) or (
                isinstance(node, (Mapping, list, tuple)) and len(node) > 0
            ):
                bounded = True
            return
        if isinstance(node, Mapping):
            for key, item in node.items():
                if len(found) >= MAX_RICH_TEXT_STRINGS:
                    bounded = True
                    return
                if isinstance(item, str):
                    if keys is None or str(key) in keys:
                        keep(item)
                else:
                    walk(item, depth + 1)
        elif isinstance(node, (list, tuple)):
            for item in node:
                if len(found) >= MAX_RICH_TEXT_STRINGS:
                    bounded = True
                    return
                walk(item, depth + 1)
        elif isinstance(node, str) and keys is None:
            keep(node)

    walk(value, 0)
    return found, bounded


def walk_strings(value: Any, keys: "frozenset[str] | None") -> list[str]:
    """``walk_strings_bounded`` for a caller with nowhere to put the second value.

    The inbound connector writes the strings into a description of the message
    that it clamps to a character count of its own, so a bound reached here
    reaches the reader as the clamp it already states. Kept as a function of
    its own rather than by widening the signature: the two readers of this
    module are switched on independently and one of them has no use for the
    flag.
    """
    found, _ = walk_strings_bounded(value, keys)
    return found
