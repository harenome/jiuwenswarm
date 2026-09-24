"""Render a question's Block Kit input elements and read back what was picked.

An ask-user question is answered here with values a person *entered* rather than
with an option they pressed. Slack has one shape for that: stateful elements --
pickers, select menus, checkboxes, text fields -- posted in the message, plus a
submit button that says the entry is finished. Every element type shares the
same lifecycle afterwards, and this module is where that sameness is written
down once:

* a **spec table** (``ELEMENT_SPECS``) says, per Slack element type, how it is
  built, which key of ``state.values`` holds its selection, and how that
  selection becomes a string. Adding a type is an entry in that table, not new
  control flow anywhere;
* an **input table** (``INPUT_SPECS``) says which declarations a question may
  make and which element(s) each renders. ``datetime`` is the only entry that
  renders more than one element, and it exists because a date and a time are
  answered as one instant.

Nothing here accumulates state. Slack sends a ``block_actions`` payload on every
change, and every one of them holds ``state.values`` -- the complete current
contents of every element in the message. The connector therefore ignores the
intermediate events and calls ``read_state`` once, when submit is pressed, with
the whole thing.

Pure by construction, like ``slack_blocks``: nothing here imports the Slack SDK
or the connector, so it can be tested, reviewed and lifted on its own. The
caller supplies declarations and dictionaries and receives dictionaries back.

Limits are Slack's, verified against slack_sdk 3.43.0's block models and the
Block Kit reference.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

logger = logging.getLogger(__name__)

# Slack's published ceilings, named rather than inlined so a breach in a test
# reads as the limit it broke.
MAX_INPUTS_PER_QUESTION = 10
MAX_LABEL_LENGTH = 150
MAX_HINT_LENGTH = 150
MAX_PLACEHOLDER_LENGTH = 150
MAX_HEADER_LENGTH = 150
MAX_OPTION_TEXT_LENGTH = 75
MAX_OPTION_VALUE_LENGTH = 75
MAX_OPTION_DESCRIPTION_LENGTH = 75
MAX_OPTIONS_PER_ELEMENT = 100
MAX_CONTEXT_ELEMENTS = 10

# A date is ``YYYY-MM-DD`` and a time is ``HH:mm``; both are what Slack promises
# and neither is what a hand-built payload always sends. Matched rather than
# parsed so a malformed value is reported as malformed instead of raising.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


def clamp(text: str, limit: int) -> str:
    """Shorten ``text`` to ``limit`` characters, marking where it was cut.

    A local copy of the connector's helper rather than an import of it: this
    module must stay liftable, and coupling a pure renderer to a 3,000-line
    connector for one four-line function is the wrong trade.
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return text[: limit - 1].rstrip() + "…"


class InputRenderError(ValueError):
    """Raised when a declaration cannot be rendered as Block Kit at all.

    Only for the failures that cannot be clamped away -- an unknown element
    type, an option whose value does not fit in the 75 characters Slack allows,
    a question declaring more inputs than a message can hold. Posting the rest
    would silently drop something the person was meant to be able to answer
    with. The option-button path already refuses that same failure.
    """


# ---------------------------------------------------------------------------
# Reading one element's entry out of ``state.values``
# ---------------------------------------------------------------------------
#
# Every reader takes the state entry Slack sends for one element and returns the
# list of raw values it holds -- empty when nothing was picked. Multi-valued
# elements return several; everything else returns nought or one. Formatting and
# validation happen afterwards, per input, so that a reader stays a description
# of Slack's payload rather than of what the answer should look like.


def _read_scalar(key: str) -> Callable[[Mapping[str, Any]], list[str]]:
    def read(entry: Mapping[str, Any]) -> list[str]:
        raw = entry.get(key)
        value = str(raw).strip() if raw is not None else ""
        return [value] if value else []

    return read


def _read_list(key: str) -> Callable[[Mapping[str, Any]], list[str]]:
    def read(entry: Mapping[str, Any]) -> list[str]:
        raw = entry.get(key)
        if not isinstance(raw, list):
            return []
        return [str(item).strip() for item in raw if str(item or "").strip()]

    return read


def _option_value(option: Any) -> str:
    """Return the value an option stands for, falling back to its label.

    Slack echoes the whole option object back, and the value is what the
    question was built to be answered with. The label is the last resort for the
    same reason it is on the button path: it is what the person actually read.
    """
    if not isinstance(option, Mapping):
        return ""
    value = str(option.get("value") or "").strip()
    if value:
        return value
    text = option.get("text")
    if isinstance(text, Mapping):
        return str(text.get("text") or "").strip()
    return ""


def _read_option(key: str) -> Callable[[Mapping[str, Any]], list[str]]:
    def read(entry: Mapping[str, Any]) -> list[str]:
        value = _option_value(entry.get(key))
        return [value] if value else []

    return read


def _read_options(key: str) -> Callable[[Mapping[str, Any]], list[str]]:
    def read(entry: Mapping[str, Any]) -> list[str]:
        raw = entry.get(key)
        if not isinstance(raw, list):
            return []
        return [value for value in (_option_value(item) for item in raw) if value]

    return read


def _flatten_rich_text(node: Any) -> str:
    """Reduce a ``rich_text`` value to the text it displays.

    A rich-text input answers with a whole ``rich_text`` block -- sections,
    lists, quotes and preformatted runs, each holding styled fragments. The
    answer travels as a string, so the fragments are concatenated and the
    styling is dropped: bold and code become their words, a link becomes its
    URL, a mention becomes the id it points at. This is not a rich-text
    renderer and is not meant to become one.
    """
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_flatten_rich_text(item) for item in node)
    if not isinstance(node, Mapping):
        return ""

    node_type = str(node.get("type") or "")
    if node_type == "text":
        return str(node.get("text") or "")
    if node_type == "link":
        return str(node.get("text") or node.get("url") or "")
    if node_type == "emoji":
        return f":{node.get('name')}:" if node.get("name") else ""
    if node_type in {"user", "usergroup", "channel"}:
        for key in ("user_id", "usergroup_id", "channel_id"):
            if node.get(key):
                return str(node[key])
        return ""
    # A container: rich_text, rich_text_section, rich_text_list,
    # rich_text_quote, rich_text_preformatted. Sections are separated by a
    # newline so a multi-paragraph answer does not run together into one word.
    children = node.get("elements")
    if not isinstance(children, list):
        return ""
    if node_type in {"rich_text", "rich_text_list"}:
        return "\n".join(
            part for part in (_flatten_rich_text(child) for child in children) if part
        )
    return "".join(_flatten_rich_text(child) for child in children)


def _read_rich_text(entry: Mapping[str, Any]) -> list[str]:
    text = _flatten_rich_text(entry.get("rich_text_value")).strip()
    return [text] if text else []


# ---------------------------------------------------------------------------
# Building one element
# ---------------------------------------------------------------------------


def _build_option(option: Mapping[str, Any]) -> dict[str, Any] | None:
    """Render one ``{label, value, description}`` as a Block Kit option.

    ``None`` when the option holds nothing to show. An option whose value does
    not fit is *not* silently shortened: a truncated value answers with
    something the question was not built to receive, so the caller refuses the
    whole rendering instead.
    """
    label = str(option.get("label") or "").strip()
    value = str(option.get("value") or "").strip() or label
    if not label:
        label = value
    if not label or not value:
        return None
    if len(value) > MAX_OPTION_VALUE_LENGTH:
        raise InputRenderError(
            f"option value is {len(value)} characters, over Slack's "
            f"{MAX_OPTION_VALUE_LENGTH}: {value[:40]!r}"
        )
    built: dict[str, Any] = {
        "text": {
            "type": "plain_text",
            "text": clamp(label, MAX_OPTION_TEXT_LENGTH),
            "emoji": True,
        },
        "value": value,
    }
    description = str(option.get("description") or "").strip()
    if description:
        built["description"] = {
            "type": "plain_text",
            "text": clamp(description, MAX_OPTION_DESCRIPTION_LENGTH),
            "emoji": True,
        }
    return built


def _build_options(declaration: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = declaration.get("options")
    if not isinstance(raw, list):
        return []
    options: list[dict[str, Any]] = []
    for entry in raw[:MAX_OPTIONS_PER_ELEMENT]:
        if not isinstance(entry, Mapping):
            continue
        built = _build_option(entry)
        if built is not None:
            options.append(built)
    return options


@dataclass(frozen=True)
class ElementSpec:
    """One Slack element type: how to build it, and where its value lands.

    ``read`` is the whole of the receiving side. ``initial_key`` and
    ``initial_kind`` are the whole of the "start it at this value" side, which
    is the only field whose name differs per element type -- ``initial_date``,
    ``initial_time``, ``initial_user``, ``initial_option`` and the rest.

    ``needs_options`` marks the types that cannot be rendered without a list to
    choose from, which is a declaration error rather than an empty menu.
    """

    element_type: str
    read: Callable[[Mapping[str, Any]], list[str]]
    multi: bool = False
    initial_key: str = ""
    # "text", "int", "option" or "options" -- how the declared initial value is
    # shaped once it reaches Slack.
    initial_kind: str = "text"
    needs_options: bool = False
    supports_placeholder: bool = True
    # Extra fields copied straight from the declaration when present, keyed by
    # the declaration's name and the Slack field's name.
    passthrough: tuple[tuple[str, str], ...] = ()
    # Slack fields the element carries whether or not the declaration named
    # them, with the value sent when it did not. A required field cannot be
    # left to the declaration: Slack refuses the element when one is missing,
    # and the refusal names the block rather than the field.
    required_fields: tuple[tuple[str, Any], ...] = ()
    # Slack fields typed String on an element whose declared counterpart is a
    # JSON number. Named per field, because it is Slack's typing that decides
    # and not the shape of the value that arrives.
    string_typed: frozenset[str] = frozenset()


ELEMENT_SPECS: dict[str, ElementSpec] = {
    # -- pickers ----------------------------------------------------------
    "datepicker": ElementSpec(
        element_type="datepicker",
        read=_read_scalar("selected_date"),
        initial_key="initial_date",
    ),
    "timepicker": ElementSpec(
        element_type="timepicker",
        read=_read_scalar("selected_time"),
        initial_key="initial_time",
        passthrough=(("timezone", "timezone"),),
    ),
    "datetimepicker": ElementSpec(
        element_type="datetimepicker",
        read=_read_scalar("selected_date_time"),
        initial_key="initial_date_time",
        initial_kind="int",
        supports_placeholder=False,
    ),
    # -- select menus -----------------------------------------------------
    "static_select": ElementSpec(
        element_type="static_select",
        read=_read_option("selected_option"),
        initial_key="initial_option",
        initial_kind="option",
        needs_options=True,
    ),
    "multi_static_select": ElementSpec(
        element_type="multi_static_select",
        read=_read_options("selected_options"),
        multi=True,
        initial_key="initial_options",
        initial_kind="options",
        needs_options=True,
        passthrough=(("max_selected", "max_selected_items"),),
    ),
    "users_select": ElementSpec(
        element_type="users_select",
        read=_read_scalar("selected_user"),
        initial_key="initial_user",
    ),
    "multi_users_select": ElementSpec(
        element_type="multi_users_select",
        read=_read_list("selected_users"),
        multi=True,
        initial_key="initial_users",
        initial_kind="options",
        passthrough=(("max_selected", "max_selected_items"),),
    ),
    "conversations_select": ElementSpec(
        element_type="conversations_select",
        read=_read_scalar("selected_conversation"),
        initial_key="initial_conversation",
    ),
    "multi_conversations_select": ElementSpec(
        element_type="multi_conversations_select",
        read=_read_list("selected_conversations"),
        multi=True,
        initial_key="initial_conversations",
        initial_kind="options",
        passthrough=(("max_selected", "max_selected_items"),),
    ),
    "channels_select": ElementSpec(
        element_type="channels_select",
        read=_read_scalar("selected_channel"),
        initial_key="initial_channel",
    ),
    "multi_channels_select": ElementSpec(
        element_type="multi_channels_select",
        read=_read_list("selected_channels"),
        multi=True,
        initial_key="initial_channels",
        initial_kind="options",
        passthrough=(("max_selected", "max_selected_items"),),
    ),
    # -- toggles ----------------------------------------------------------
    "checkboxes": ElementSpec(
        element_type="checkboxes",
        read=_read_options("selected_options"),
        multi=True,
        initial_key="initial_options",
        initial_kind="options",
        needs_options=True,
        supports_placeholder=False,
    ),
    "radio_buttons": ElementSpec(
        element_type="radio_buttons",
        read=_read_option("selected_option"),
        initial_key="initial_option",
        initial_kind="option",
        needs_options=True,
        supports_placeholder=False,
    ),
    # -- text fields ------------------------------------------------------
    "plain_text_input": ElementSpec(
        element_type="plain_text_input",
        read=_read_scalar("value"),
        initial_key="initial_value",
        passthrough=(
            ("multiline", "multiline"),
            ("min_length", "min_length"),
            ("max_length", "max_length"),
        ),
    ),
    "email_text_input": ElementSpec(
        element_type="email_text_input",
        read=_read_scalar("value"),
        initial_key="initial_value",
    ),
    "url_text_input": ElementSpec(
        element_type="url_text_input",
        read=_read_scalar("value"),
        initial_key="initial_value",
    ),
    "number_input": ElementSpec(
        element_type="number_input",
        read=_read_scalar("value"),
        initial_key="initial_value",
        passthrough=(
            ("decimal", "is_decimal_allowed"),
            ("min", "min_value"),
            ("max", "max_value"),
        ),
        # Two fields of this element are not the declaration's to decide, and
        # a live question found out the hard way. ``is_decimal_allowed`` is
        # required, and ``min_value``/``max_value`` are typed String where the
        # declaration carries JSON numbers. Either mistake is refused, and the
        # refusal is the same one: the validator declines the element and then
        # reads the ``input`` block as carrying none at all, answering
        # ``/<index>/element=missing_field``. The message loses every control
        # in it, not only this one.
        #
        # ``True`` is what a question that says nothing about decimals gets. A
        # question narrows a number to whole ones by asking for that; silence
        # is not the request, and the narrower default would turn a question
        # that never mentioned decimals into one refusing 3.5.
        required_fields=(("is_decimal_allowed", True),),
        string_typed=frozenset({"min_value", "max_value"}),
    ),
    "rich_text_input": ElementSpec(
        element_type="rich_text_input",
        read=_read_rich_text,
        initial_key="initial_value",
        initial_kind="rich_text",
    ),
}


def _slack_string(value: Any) -> str:
    """A declared number as the string the Slack field it feeds is typed as.

    ``min_value`` and ``max_value`` are Strings in Slack's reference while the
    declaration carries ``min`` and ``max`` as JSON numbers. Sending the number
    is what gets the element refused.

    A whole number loses the fractional part JSON gives it, so a bound written
    ``1.0`` reaches Slack as ``"1"``. ``"1.0"`` is a decimal bound, and beside
    ``is_decimal_allowed: false`` it is a bound written in a form the element
    accepts no answer in.
    """
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _build_element(
    spec: ElementSpec,
    declaration: Mapping[str, Any],
    *,
    action_id: str,
    initial: Any = None,
) -> dict[str, Any]:
    """Render one element, or raise ``InputRenderError`` if it cannot be.

    Everything variable about an element type is read off its ``ElementSpec``:
    this function has no per-type branch and gains none when a type is added.
    """
    element: dict[str, Any] = {"type": spec.element_type, "action_id": action_id}

    if spec.needs_options:
        options = _build_options(declaration)
        if not options:
            raise InputRenderError(
                f"{spec.element_type} needs a non-empty options list"
            )
        element["options"] = options

    placeholder = str(declaration.get("placeholder") or "").strip()
    if placeholder and spec.supports_placeholder:
        element["placeholder"] = {
            "type": "plain_text",
            "text": clamp(placeholder, MAX_PLACEHOLDER_LENGTH),
            "emoji": True,
        }

    if initial is None:
        initial = declaration.get("initial")
    if initial not in (None, "", [], {}):
        rendered = _render_initial(spec, initial, element.get("options"))
        if rendered is not None:
            element[spec.initial_key] = rendered

    for declared_name, slack_name in spec.passthrough:
        value = declaration.get(declared_name)
        if value is not None:
            element[slack_name] = (
                _slack_string(value) if slack_name in spec.string_typed else value
            )

    # After the declaration, so that what was declared wins, and unconditional,
    # so that what Slack requires is present whether or not it was declared.
    for slack_name, default in spec.required_fields:
        element.setdefault(slack_name, default)

    return element


def _render_initial(
    spec: ElementSpec, initial: Any, options: list[dict[str, Any]] | None
) -> Any:
    """Shape a declared initial value the way this element type expects it.

    An initial *option* has to be one of the rendered options, not a copy of it:
    Slack rejects an ``initial_option`` that does not appear in ``options``. It
    is therefore matched by value rather than rebuilt, and an initial naming a
    value that was not offered is dropped rather than sent.
    """
    if spec.initial_kind == "int":
        try:
            return int(initial)
        except (TypeError, ValueError):
            return None
    if spec.initial_kind == "rich_text":
        return initial if isinstance(initial, Mapping) else None
    if spec.initial_kind == "option":
        wanted = str(initial).strip()
        for option in options or []:
            if option.get("value") == wanted:
                return option
        return None
    if spec.initial_kind == "options":
        wanted_list = initial if isinstance(initial, list) else [initial]
        wanted = {str(item).strip() for item in wanted_list}
        if options is None:
            # users/conversations/channels multi-selects take bare ids.
            return [str(item).strip() for item in wanted_list if str(item).strip()]
        picked = [option for option in options if option.get("value") in wanted]
        return picked or None
    return str(initial)


# ---------------------------------------------------------------------------
# Formatting one input's raw values into the answer's strings
# ---------------------------------------------------------------------------


class InputValueError(ValueError):
    """Raised when what came back for an input is not a value it can answer with."""


def _format_passthrough(
    values: list[str], _entries: list[Mapping[str, Any]], _zone: str
) -> list[str]:
    return values


def _format_date(
    values: list[str], _entries: list[Mapping[str, Any]], _zone: str
) -> list[str]:
    for value in values:
        if not _DATE_RE.match(value):
            raise InputValueError(f"{value!r} is not a YYYY-MM-DD date")
    return values


def _zone_suffix(entries: Sequence[Mapping[str, Any]], declared: str) -> str:
    """Return the IANA zone name Slack reported, or the one the question pinned.

    Slack attaches ``timezone`` to a ``timepicker``'s payload when the client
    knows it. A question may also pin one when it posts the picker, which is
    what makes the zone survive a client that reports none. The payload wins:
    it is what the person was actually looking at.
    """
    for entry in entries:
        reported = str(entry.get("timezone") or "").strip()
        if reported:
            return reported
    return declared.strip()


def _format_time(
    values: list[str], entries: list[Mapping[str, Any]], declared_zone: str
) -> list[str]:
    """Render a bare time as ``HH:MM:SS``, tagged with its zone when there is one.

    No UTC offset, deliberately. An offset is a property of an instant, and a
    time with no date has none: whether Europe/Paris is +01:00 or +02:00 at
    09:30 depends on which day it is. Naming the zone instead reports only what is
    known.
    """
    formatted: list[str] = []
    zone = _zone_suffix(entries, declared_zone)
    for value in values:
        if not _TIME_RE.match(value):
            raise InputValueError(f"{value!r} is not an HH:mm time")
        formatted.append(f"{value}:00[{zone}]" if zone else f"{value}:00")
    return formatted


def _format_epoch(
    values: list[str], _entries: list[Mapping[str, Any]], _zone: str
) -> list[str]:
    """Render a ``datetimepicker``'s Unix timestamp as a UTC instant.

    UTC and no zone name, because that is all the element holds: it answers
    with a number of seconds and Slack does not say which zone the person was
    reading when they picked it. That is precisely why ``datetime`` -- a
    datepicker beside a timepicker -- is the entry to prefer.
    """
    formatted: list[str] = []
    for value in values:
        try:
            seconds = int(value)
        except (TypeError, ValueError) as exc:
            raise InputValueError(f"{value!r} is not a Unix timestamp") from exc
        formatted.append(datetime.fromtimestamp(seconds, timezone.utc).isoformat())
    return formatted


def _format_number(
    values: list[str], _entries: list[Mapping[str, Any]], _zone: str
) -> list[str]:
    """Check that what came back is a number, which ``float`` alone does not.

    ``float`` reads ``inf`` and ``nan``, and neither is an answer: nothing
    downstream compares usefully against either, and the element asking the
    question offers no way to enter one. Checked on the value rather than
    assumed of the element, because what arrives here is a string out of a
    client payload.
    """
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise InputValueError(f"{value!r} is not a number") from exc
        if not math.isfinite(number):
            raise InputValueError(f"{value!r} is not a number")
    return values


def compose_datetime(date_value: str, time_value: str, zone: str) -> str:
    """Join a picked date and time into one ISO 8601 instant.

    The zone is resolved *against that date*, which is the whole reason the two
    are composed here rather than delivered apart: the offset of a wall-clock
    time is a function of the day it falls on, and 09:30 in Europe/Paris is
    +01:00 in January and +02:00 in July.

    The zone's name is kept beside the offset, in RFC 9557's bracket form
    (``2026-08-20T09:30:00+02:00[Europe/Paris]``). An offset places one instant and
    does not describe a recurrence, so "09:30 Paris, every day" cannot be
    written as "09:30+02:00, every day". Everything before the bracket is plain
    ISO 8601, so a consumer that wants only the instant can cut there.

    A zone the host's tz database does not know keeps its name and loses its
    offset, which is the honest rendering of "this is when, and we could not
    work out what that means here".
    """
    if not zone:
        return f"{date_value}T{time_value}:00"
    try:
        from zoneinfo import ZoneInfo

        moment = datetime.fromisoformat(f"{date_value}T{time_value}:00").replace(
            tzinfo=ZoneInfo(zone)
        )
    except Exception:  # noqa: BLE001 - unknown zone, or no tz database installed
        logger.warning(
            "Slack reported a timezone this host cannot resolve, so the answer "
            "carries its name without an offset: %s",
            zone,
        )
        return f"{date_value}T{time_value}:00[{zone}]"
    return f"{moment.isoformat()}[{zone}]"


# ---------------------------------------------------------------------------
# The input table: what a question may declare
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InputSpec:
    """One thing a question can ask for, and the element(s) that ask it.

    ``parts`` names the elements an input renders. All but ``datetime`` render
    one, and the part name is what distinguishes their action ids inside a
    single input.

    ``format`` turns the raw values Slack sent into the strings the answer
    holds. It is where validation lives: raising ``InputValueError`` is how an
    input says the submit is not answerable, which the caller reports to the
    person who pressed it rather than sending on.
    """

    name: str
    parts: tuple[str, ...]
    element_types: tuple[str, ...]
    format: Callable[[list[str], list[Mapping[str, Any]], str], list[str]] = (
        _format_passthrough
    )
    # Set for the one input whose parts are not independent values.
    composite: bool = False
    description: str = ""

    @property
    def multi(self) -> bool:
        return any(ELEMENT_SPECS[name].multi for name in self.element_types)


INPUT_SPECS: dict[str, InputSpec] = {
    "date": InputSpec(
        name="date",
        parts=("",),
        element_types=("datepicker",),
        format=_format_date,
        description="a calendar date, answered as YYYY-MM-DD",
    ),
    "time": InputSpec(
        name="time",
        parts=("",),
        element_types=("timepicker",),
        format=_format_time,
        description="a time of day, answered as HH:MM:SS[Zone]",
    ),
    "datetime": InputSpec(
        name="datetime",
        parts=("date", "time"),
        element_types=("datepicker", "timepicker"),
        composite=True,
        description="a date beside a time, answered as one ISO 8601 instant",
    ),
    "datetime_unix": InputSpec(
        name="datetime_unix",
        parts=("",),
        element_types=("datetimepicker",),
        format=_format_epoch,
        description="Slack's combined picker, answered as a UTC instant",
    ),
    "select": InputSpec(
        name="select",
        parts=("",),
        element_types=("static_select",),
        description="one of a declared list of options",
    ),
    "multi_select": InputSpec(
        name="multi_select",
        parts=("",),
        element_types=("multi_static_select",),
        description="any number of a declared list of options",
    ),
    "user": InputSpec(
        name="user",
        parts=("",),
        element_types=("users_select",),
        description="one workspace member, answered as a Slack user id",
    ),
    "users": InputSpec(
        name="users",
        parts=("",),
        element_types=("multi_users_select",),
        description="any number of workspace members, as Slack user ids",
    ),
    "conversation": InputSpec(
        name="conversation",
        parts=("",),
        element_types=("conversations_select",),
        description="one conversation, answered as a Slack conversation id",
    ),
    "conversations": InputSpec(
        name="conversations",
        parts=("",),
        element_types=("multi_conversations_select",),
        description="any number of conversations, as Slack conversation ids",
    ),
    "channel": InputSpec(
        name="channel",
        parts=("",),
        element_types=("channels_select",),
        description="one public channel, answered as a Slack channel id",
    ),
    "channels": InputSpec(
        name="channels",
        parts=("",),
        element_types=("multi_channels_select",),
        description="any number of public channels, as Slack channel ids",
    ),
    "checkboxes": InputSpec(
        name="checkboxes",
        parts=("",),
        element_types=("checkboxes",),
        description="any number of a declared list of options, as checkboxes",
    ),
    "radio": InputSpec(
        name="radio",
        parts=("",),
        element_types=("radio_buttons",),
        description="one of a declared list of options, as radio buttons",
    ),
    "text": InputSpec(
        name="text",
        parts=("",),
        element_types=("plain_text_input",),
        description="typed text, single line unless multiline is declared",
    ),
    "email": InputSpec(
        name="email",
        parts=("",),
        element_types=("email_text_input",),
        description="a typed email address",
    ),
    "url": InputSpec(
        name="url",
        parts=("",),
        element_types=("url_text_input",),
        description="a typed URL",
    ),
    "number": InputSpec(
        name="number",
        parts=("",),
        element_types=("number_input",),
        format=_format_number,
        description="a typed number",
    ),
    "rich_text": InputSpec(
        name="rich_text",
        parts=("",),
        element_types=("rich_text_input",),
        description="typed rich text, answered as the text it displays",
    ),
}


@dataclass
class QuestionInput:
    """One declared input, resolved against the table and ready to render.

    ``name`` keys the value in a multi-input answer and defaults to the declared
    type, which is what makes a single-input question -- the common case -- need
    no naming at all.
    """

    spec: InputSpec
    name: str
    label: str
    declaration: Mapping[str, Any] = field(default_factory=dict)
    optional: bool = False
    index: int = 0

    def action_id(self, prefix: str, part: str) -> str:
        return action_id_for(prefix, self.index, part)


def action_id_for(prefix: str, index: int, part: str) -> str:
    """The action id one element of one input is posted under.

    Unique within the message, which Slack requires, and parseable back to the
    input it belongs to, which is what lets ``state.values`` be read without
    depending on the block ids Slack generates when none are given.
    """
    return f"{prefix}{index}.{part}" if part else f"{prefix}{index}"


def parse_inputs(question: Mapping[str, Any]) -> list[QuestionInput]:
    """Resolve a question's ``inputs`` declaration, or return ``[]``.

    ``[]`` means the question is not an input question, which is every question
    that declares no ``inputs``: the option-button path is left to handle it,
    unchanged. That is the whole of the backward compatibility contract.

    An entry may be a bare type name -- ``inputs: ["date", "time"]`` -- or a
    mapping holding a label, an initial value and the type's own knobs. The
    bare form exists because the common question declares one input and has
    nothing to say about it beyond what it is.
    """
    raw = question.get("inputs")
    if not isinstance(raw, (list, tuple)) or not raw:
        return []

    inputs: list[QuestionInput] = []
    for entry in raw:
        if isinstance(entry, str):
            entry = {"type": entry}
        if not isinstance(entry, Mapping):
            logger.warning("Slack question input is not a declaration: %r", entry)
            continue
        type_name = str(entry.get("type") or "").strip()
        spec = INPUT_SPECS.get(type_name)
        if spec is None:
            raise InputRenderError(
                f"unknown question input type {type_name!r}; known types are "
                + ", ".join(sorted(INPUT_SPECS))
            )
        name = str(entry.get("name") or "").strip() or spec.name
        # Falls back to the input's name rather than to prose. The question
        # arrives in whatever language it was written in, and a label this
        # module invented would be the one line of the message in another.
        label = str(entry.get("label") or "").strip() or name
        inputs.append(
            QuestionInput(
                spec=spec,
                name=name,
                label=clamp(label, MAX_LABEL_LENGTH),
                declaration=entry,
                optional=bool(entry.get("optional")),
                index=len(inputs),
            )
        )

    if len(inputs) > MAX_INPUTS_PER_QUESTION:
        raise InputRenderError(
            f"a question declares {len(inputs)} inputs, over the "
            f"{MAX_INPUTS_PER_QUESTION} one message carries"
        )
    names = [entry.name for entry in inputs]
    if len(set(names)) != len(names):
        raise InputRenderError(f"question inputs do not have distinct names: {names}")
    return inputs


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def divider_block() -> dict[str, Any]:
    """A horizontal rule. Non-interactive and holds nothing."""
    return {"type": "divider"}


def header_block(text: str) -> dict[str, Any]:
    """A large bold heading. ``plain_text`` only -- markup shows literally."""
    return {
        "type": "header",
        "text": {
            "type": "plain_text",
            "text": clamp(text, MAX_HEADER_LENGTH),
            "emoji": True,
        },
    }


def context_block(*lines: str) -> dict[str, Any]:
    """Small grey text beneath a block, for notes that are not the message."""
    elements = [
        {"type": "mrkdwn", "text": line}
        for line in lines[:MAX_CONTEXT_ELEMENTS]
        if line
    ]
    return {"type": "context", "elements": elements}


def render_input_blocks(
    inputs: Sequence[QuestionInput], *, action_id_prefix: str
) -> list[dict[str, Any]]:
    """Render every declared input as a labelled ``input`` block.

    One block per element, and an ``input`` block for all of them rather than an
    ``actions`` block for some: ``input`` is the only container with a label,
    and an unlabelled row of three pickers does not say which is which. Slack
    permits ``input`` blocks in messages, and a message's ``block_actions``
    payload holds their contents in ``state.values`` exactly as a modal's
    submission does.

    ``optional`` is passed through for what it displays. It is not what enforces
    a required input: a message has no submission Slack validates, so the check
    is ours and happens when submit is pressed.
    """
    blocks: list[dict[str, Any]] = []
    for entry in inputs:
        for part, element_type in zip(entry.spec.parts, entry.spec.element_types):
            spec = ELEMENT_SPECS[element_type]
            element = _build_element(
                spec,
                entry.declaration,
                action_id=entry.action_id(action_id_prefix, part),
                initial=_initial_for_part(entry, part),
            )
            block: dict[str, Any] = {
                "type": "input",
                "block_id": entry.action_id(action_id_prefix, part),
                "label": {
                    "type": "plain_text",
                    "text": clamp(_label_for_part(entry, part), MAX_LABEL_LENGTH),
                    "emoji": True,
                },
                "element": element,
                # What Slack displays, not what it enforces: a message has no
                # submission for Slack to validate, so a required input is
                # checked here when submit is pressed.
                "optional": entry.optional,
            }
            hint = str(entry.declaration.get("hint") or "").strip()
            if hint:
                block["hint"] = {
                    "type": "plain_text",
                    "text": clamp(hint, MAX_HINT_LENGTH),
                    "emoji": True,
                }
            blocks.append(block)
    return blocks


def _label_for_part(entry: QuestionInput, part: str) -> str:
    """The label one element of an input is shown under.

    A composite input renders two elements and therefore needs two labels. The
    second is declared as ``<part>_label``; without one it falls back to the
    part's own name, for the same reason the input's label does.
    """
    if not part:
        return entry.label
    declared = str(entry.declaration.get(f"{part}_label") or "").strip()
    if declared:
        return declared
    if part == entry.spec.parts[0]:
        return entry.label
    return part


def _initial_for_part(entry: QuestionInput, part: str) -> Any:
    if not part:
        return entry.declaration.get("initial")
    return entry.declaration.get(f"initial_{part}")


# ---------------------------------------------------------------------------
# Reading a submit
# ---------------------------------------------------------------------------


@dataclass
class InputResult:
    """What one input's elements held when submit was pressed."""

    name: str
    label: str
    values: list[str]
    error: str = ""

    @property
    def empty(self) -> bool:
        return not self.values


def flatten_state(state_values: Any) -> dict[str, Mapping[str, Any]]:
    """Reduce ``state.values`` to ``{action_id: entry}``.

    Slack keys the outer level by block id, which it generates when the message
    did not supply one. Keying by action id instead is what makes reading
    independent of that: the connector chose the action ids and knows them.
    """
    flattened: dict[str, Mapping[str, Any]] = {}
    if not isinstance(state_values, Mapping):
        return flattened
    for block in state_values.values():
        if not isinstance(block, Mapping):
            continue
        for action_id, entry in block.items():
            if isinstance(entry, Mapping):
                flattened[str(action_id)] = entry
    return flattened


def read_state(
    inputs: Sequence[QuestionInput],
    state_values: Any,
    *,
    action_id_prefix: str,
) -> list[InputResult]:
    """Read every input out of one ``state.values``, in declaration order.

    Called once, on submit, with the whole payload. Slack sends the complete
    current contents of the message on every change event, so there is nothing
    to accumulate and nothing an ignored intermediate event can lose.

    A malformed value is reported on the result rather than raised: one input
    that came back wrong should name itself to the person who pressed submit,
    and the others are still worth reading to say whether they were filled.
    """
    flattened = flatten_state(state_values)
    results: list[InputResult] = []
    for entry in inputs:
        raw: list[str] = []
        entries: list[Mapping[str, Any]] = []
        for part, element_type in zip(entry.spec.parts, entry.spec.element_types):
            state_entry = flattened.get(entry.action_id(action_id_prefix, part), {})
            entries.append(state_entry)
            raw.extend(ELEMENT_SPECS[element_type].read(state_entry))
        results.append(_finish(entry, raw, entries))
    return results


def _finish(
    entry: QuestionInput, raw: list[str], entries: list[Mapping[str, Any]]
) -> InputResult:
    declared_zone = str(entry.declaration.get("timezone") or "").strip()
    if entry.spec.composite:
        return _finish_composite(entry, raw, entries, declared_zone)
    if not raw:
        return InputResult(name=entry.name, label=entry.label, values=[])
    try:
        values = entry.spec.format(raw, entries, declared_zone)
    except InputValueError as exc:
        return InputResult(
            name=entry.name, label=entry.label, values=[], error=str(exc)
        )
    return InputResult(name=entry.name, label=entry.label, values=values)


def _finish_composite(
    entry: QuestionInput,
    raw: list[str],
    entries: list[Mapping[str, Any]],
    declared_zone: str,
) -> InputResult:
    """Join a ``datetime``'s two elements, or say which half is missing.

    Half a datetime is not an answer. Reporting *which* half is what lets the
    person fix it in one go rather than discovering the other on the next try.
    """
    date_values = [value for value in raw if _DATE_RE.match(value)]
    time_values = [value for value in raw if _TIME_RE.match(value)]
    malformed = [
        value for value in raw if value not in date_values and value not in time_values
    ]
    if malformed:
        return InputResult(
            name=entry.name,
            label=entry.label,
            values=[],
            error=f"{malformed[0]!r} is neither a YYYY-MM-DD date nor an HH:mm time",
        )
    if not date_values and not time_values:
        return InputResult(name=entry.name, label=entry.label, values=[])
    if not date_values:
        return InputResult(
            name=entry.name, label=entry.label, values=[], error="no date was picked"
        )
    if not time_values:
        return InputResult(
            name=entry.name, label=entry.label, values=[], error="no time was picked"
        )
    zone = _zone_suffix(entries, declared_zone)
    return InputResult(
        name=entry.name,
        label=entry.label,
        values=[compose_datetime(date_values[0], time_values[0], zone)],
    )


def compose_answer(
    inputs: Sequence[QuestionInput], results: Sequence[InputResult]
) -> list[str]:
    """Turn the read inputs into the ``selected_options`` an answer holds.

    Two shapes, and the rule between them is how many things were asked for.

    A question with **one** input answers with that input's values and nothing
    else -- ``["2026-08-20T09:30:00+02:00[Europe/Paris]"]`` for a datetime,
    ``["red", "blue"]`` for a multi-select. That is byte-identical in shape to
    the answer a pressed button produces, which is the point: the receiving side
    upstream reads one selected option as a value and several as a list, and it
    should not have to learn a second convention to hear about a date.

    A question with **several** answers with one entry holding a compact JSON
    object keyed by input name. Names are the only thing that distinguishes two
    values of the same type, positional order would be lost the moment an
    optional input was left blank, and ``name=value`` pairs cannot be taken
    apart again when a value contains an ``=``. JSON is the one encoding that is
    unambiguous in every case and that a reader -- person or model -- can still
    make sense of at a glance.
    """
    filled = [result for result in results if result.values]
    if len(inputs) <= 1:
        return list(filled[0].values) if filled else []
    payload: dict[str, Any] = {}
    for result, entry in zip(results, inputs):
        if not result.values:
            continue
        payload[result.name] = (
            result.values if entry.spec.multi else result.values[0]
        )
    if not payload:
        return []
    return [json.dumps(payload, separators=(",", ":"), ensure_ascii=False)]


def submit_problem(
    inputs: Sequence[QuestionInput], results: Sequence[InputResult]
) -> str:
    """Say why a submit cannot be answered with, or "" when it can.

    Three cases, all of them explicit because none of them can be assumed away:
    an input that came back malformed, a required input nobody filled, and a
    submit where nothing at all was entered. The last is separate from the
    second because a question whose every input is optional still cannot be
    answered with silence -- there would be nothing to send.
    """
    for result in results:
        if result.error:
            return f"{result.label}: {result.error}"
    for entry, result in zip(inputs, results):
        if result.empty and not entry.optional:
            return f"{result.label}: nothing was selected"
    if all(result.empty for result in results):
        return "nothing was selected"
    return ""
