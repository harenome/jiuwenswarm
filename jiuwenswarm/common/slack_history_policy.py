# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""One word for how far a Slack conversation may read, and the names it travels under.

Five words, each strictly wider than the one above it:

``disabled``
    No history tool at all.
``origin``
    This conversation only: the tool card declares no target, and the
    conversation comes from trusted request metadata.
``members``
    Another conversation may be named, and every named one is gated at read
    time on ``members(S) subset-of members(T)``.
``visible``
    As ``members``, except that for a *public* target the question asked about
    each person in ``S`` is whether they could read ``T`` themselves rather
    than whether they are in it. A full member of the workspace can, because
    joining a public channel is self-serve; a guest cannot, and neither can
    anybody Slack does not report as a full member of this workspace.
``open``
    As ``members``, except that a *public* target skips the rule entirely.

The rule is about the room, not about the asker: the answer is posted into
``S`` and everybody there sees it, so a rule keyed on the asker alone would
summarise ``T`` into a room full of people who are not in ``T``. It subsumes the
DM case rather than special-casing it -- in a DM ``members(S)`` is the user and
the bot, so it reduces to *the asking user and the bot are both in T*.

**Membership is the wrong question for a public target, and saying so is the
whole of what ``visible`` adds.** Under ``members`` a public channel is refused
to a room whose people are merely not in it, on the stated ground that they
cannot read it themselves -- which for a full member of the workspace is simply
false, since joining is self-serve and needs nobody's permission. ``visible``
asks the true question instead. It is a fifth word rather than a repair of
``members``, because a deployment sitting on ``members`` wrote down a membership
rule and has to go on getting one: an upgrade must not widen a gate nobody
widened.

**What separates ``visible`` from ``open``, and why both earn their place.**
``open`` relaxes on the target's publicness alone and reads nothing about
anybody, so it costs no member list and no directory lookup. It is the cheap
word, and the right one for a workspace with no guests in it. ``visible`` pays
for those calls in order to keep a guest from being shown a channel nobody
invited them to.

**Three things the rule reads, and one it must not.** Whether ``T`` is public,
because only a public channel is joinable at will. What kind of conversation
each side is, because a direct message and a group direct message are joinable
by nobody, however their flags read. And what each person in ``S`` is -- a full
member, a guest, or an application -- because only a full member can act on a
public channel's self-serve membership. What it must not read is anything about
``S`` itself, for the reason in the next paragraph.

Publicness is asymmetric and only targets relax. As a *target* a public ``T`` is
safe: membership there is self-serve, so the content was already reachable by
anybody who cared to join. As a *source* a public ``S`` is the dangerous side --
the check holds at the instant it is evaluated, and somebody may join ``S``
afterwards and read ``T`` out of the scrollback. There is therefore no value
that reads the source's privacy at all.

Three processes need the same five words and the same resolution: the Slack
connector, which settles the word and stamps it; the cron scheduler, which
settles it again per run for a job's conversation; and the runtime's history
toolkit, which acts on the stamp. Neither of the last two may import the
connector -- it pulls in ``slack_bolt`` -- and the runtime must not read
connector config at all, so the mapping-in, word-out resolution lives here.

Reading ``channels.slack`` at all is this module's other job, through
:func:`slack_config`, and once that key can hold a list of installs the reading
has to say *which* install. A bot token is issued per installation and so is
every id it can see: a user id, a conversation id and a message ts name
something in one workspace and nothing in another. Selecting the block for one
team therefore belongs beside the policy word rather than in each of the five
model-facing tools, which would otherwise hold five readings of one shape. See
:func:`select_slack_workspace`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

#: The five words, in widening order, so that "is this at least X" is an index
#: comparison.
HISTORY_DISABLED = "disabled"
HISTORY_ORIGIN = "origin"
HISTORY_MEMBERS = "members"
HISTORY_VISIBLE = "visible"
HISTORY_OPEN = "open"
HISTORY_POLICY_VALUES: tuple[str, ...] = (
    HISTORY_DISABLED,
    HISTORY_ORIGIN,
    HISTORY_MEMBERS,
    HISTORY_VISIBLE,
    HISTORY_OPEN,
)
#: What a deployment that has written none of this gets. ``disabled`` rather
#: than ``origin`` because the shipped ``history_digest_channel_ids`` was ``[]``,
#: which read as "no history", and an upgrade must not turn a feature on.
HISTORY_POLICY_DEFAULT = HISTORY_DISABLED

#: The words that let a request name a conversation other than its own.
HISTORY_POLICY_NAMES_A_TARGET: frozenset[str] = frozenset(
    {HISTORY_MEMBERS, HISTORY_VISIBLE, HISTORY_OPEN}
)

#: Layer-0 keys, under ``channels.slack``.
KEY_HISTORY = "history"
KEY_HISTORY_NEVER_READ = "history_never_read"
KEY_HISTORY_EXEMPT_MEMBERS = "history_exempt_members"
#: The key ``history`` retires. Still read, still shipped in the template: a key
#: removed from the shipped template is deleted from the operator's file on
#: upgrade, so dropping it outright would silently take history away from a
#: deployment that had configured it, with nothing in the diff to see.
LEGACY_KEY_HISTORY_CHANNEL_IDS = "history_digest_channel_ids"

#: What the connector stamps onto request metadata and the runtime toolkit
#: reads. Literals rather than a shared enum on the wire, because request
#: metadata crosses a process boundary as JSON; a test pins the connector, the
#: cron path and the toolkit to these three names.
METADATA_POLICY_KEY = "slack_history_policy"
METADATA_NEVER_READ_KEY = "slack_history_never_read"
METADATA_EXEMPT_MEMBERS_KEY = "slack_history_exempt_members"
#: Who is asking. Already on every inbound Slack request; named here because the
#: gate reads it, and a cron run legitimately has none.
METADATA_ASKER_KEY = "slack_user_id"
#: Which path built the metadata. A cron run says so, and the gate reads the
#: marker rather than inferring one from a missing sender: "no asker, and that
#: is expected" and "no asker, and something is wrong" are opposite answers. The
#: connector never stamps this, so an inbound request cannot claim to be a cron
#: run. Held here rather than in the cron module so that the runtime's history
#: toolkit can read it without importing the gateway.
METADATA_ORIGIN_KEY = "slack_history_origin"
ORIGIN_CRON_JOB = "cron_job"
#: Which Slack installation the request came from. The connector stamps it on
#: every inbound request from the event body, and every Slack session id opens
#: with it. One installation issues one token pair and one namespace of user,
#: conversation and message ids, so with more than one configured this is the
#: only thing on a request that says which token may serve it and which
#: namespace its ids are to be read in.
METADATA_TEAM_KEY = "slack_team_id"

#: The list of installs under ``channels.slack``, and the one key inside a
#: block a model-facing tool reads. The connector owns the shape and its
#: ``normalize_slack_conf`` is the reference reading; these two names are the
#: whole of what the runtime needs from it, spelled here rather than imported
#: because importing the connector pulls in ``slack_bolt``. A test pins this
#: module's reading to that function's.
KEY_WORKSPACES = "workspaces"
KEY_BOT_TOKEN = "bot_token"

#: The keys that belong to one install rather than to the connector. Selecting
#: a workspace overlays exactly these onto the connector-wide mapping, so that
#: every other key -- the bounds, the policy word, the search switch -- keeps
#: reading the same whichever install a request is served by.
SLACK_WORKSPACE_KEYS: tuple[str, ...] = (
    "bot_token",
    "app_token",
    "enabled",
    "default_channel_id",
)

#: The prefix every Slack session id carries, and the separator after the team.
#: ``slack_{team}_{conversation}``: a Slack team id is ``T`` followed by
#: alphanumerics and never holds an underscore, so the second field is the whole
#: of it. The connector has the same parser; this one exists because a harness
#: tool may not import the connector to reach it.
_SLACK_SESSION_PREFIX = "slack_"


class SlackWorkspaceUnresolved(LookupError):
    """No configured Slack install may serve this request.

    Raised rather than answered with a fallback, because the fallback is the
    failure: with two installs configured, serving a request from the first
    block that happens to hold a token reads or writes in a workspace nobody
    asked about. Carries a ``code`` for a tool to report in its own refusal
    shape and a ``detail`` saying what could not be settled, following the
    ``missing_scope`` treatment the Slack tools already give a Slack refusal.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def slack_team_id_from_session(session_id: Any) -> str:
    """The workspace a Slack session id was built for, or the empty string.

    The team survives on anything carrying a session, including a scheduled run
    rebuilt hours after the event that created the job is gone. It is the only
    workspace a cron run has: the cron path stamps a conversation and a policy
    onto its request metadata and no team.

    Stdlib string work and no import of the connector, whose
    ``slack_session_team_id`` is the same reading of the same three forms.
    """
    raw = str(session_id or "").strip()
    if not raw.startswith(_SLACK_SESSION_PREFIX):
        return ""
    parts = raw.split("_", 2)
    if len(parts) < 3:
        # ``slack_T…`` with no conversation after it is not one of the forms;
        # it was assembled out of something missing.
        return ""
    return parts[1].strip()


def slack_workspace_blocks(
    slack_conf: Mapping[str, Any] | None,
) -> "tuple[Mapping[str, Any], ...]":
    """The configured Slack installs, in the order the operator wrote them.

    Both shapes, read the way ``normalize_slack_conf`` reads them:

    * a ``workspaces`` list yields its mapping entries, malformed ones dropped;
    * anything else yields the ``channels.slack`` mapping itself as the single
      block, which is what every Slack config written before ``workspaces``
      existed is.

    **An empty list is read as no list at all**, and it has to be: the shipped
    template carries ``workspaces: []`` -- a key absent from the template is
    deleted from an operator's file on the next upgrade, so it cannot be left
    out -- and every existing config gains it the moment it is upgraded. Read
    literally, that key would take the token away from a working
    single-workspace deployment on upgrade.

    The single block is the ``channels.slack`` mapping *itself* rather than a
    copy of it, which is what lets :func:`select_slack_workspace` answer the
    single-workspace case with the very mapping it was handed.

    A ``workspaces`` key that is neither a list nor empty yields no blocks at
    all. It is the one shape that must not fall back to the top-level pair: an
    operator who wrote a list wrote it to stop one token serving everything.
    """
    if not isinstance(slack_conf, Mapping):
        return ()
    raw_blocks = slack_conf.get(KEY_WORKSPACES)
    if not raw_blocks:
        return (slack_conf,)
    if not isinstance(raw_blocks, (list, tuple)):
        logger.warning(
            "channels.slack.%s is %s rather than a list; no Slack workspace"
            " can be selected for a tool call",
            KEY_WORKSPACES,
            type(raw_blocks).__name__,
        )
        return ()
    return tuple(entry for entry in raw_blocks if isinstance(entry, Mapping))


def _overlay_workspace(
    slack_conf: Mapping[str, Any], block: Mapping[str, Any]
) -> Mapping[str, Any]:
    """The connector-wide mapping with one install's own keys written over it."""
    if block is slack_conf:
        # The single-mapping form: the block and the mapping are one object, so
        # there is nothing to write over and the caller gets back exactly what
        # it would have got before workspaces existed.
        return slack_conf
    merged = dict(slack_conf)
    merged.update({key: block[key] for key in SLACK_WORKSPACE_KEYS if key in block})
    return merged


def select_slack_workspace(
    slack_conf: Mapping[str, Any] | None,
    team_id: str | None = None,
    *,
    team_of_token: "Callable[[str], str | None] | None" = None,
) -> Mapping[str, Any]:
    """``channels.slack`` as one install's own settings, or a refusal.

    The config holds no team id anywhere -- a block holds the token pair an
    install was issued and nothing naming the install -- so which block answers
    to ``team_id`` is learned from Slack rather than read. ``team_of_token`` is
    that knowledge, as a lookup from a bot token to the team it is installed in;
    it answers ``None`` for a token nobody has identified yet. Keeping the
    lookup out of here is what leaves this function pure: no network, no cache,
    no event loop, and a unit test that needs neither.

    **One configured install is answered without asking anything.** The mapping
    comes back unchanged and ``team_id`` is not compared against it, which is
    both what keeps the single-workspace deployment byte-identical and the
    correct answer: one install means one Socket Mode connection, so the only
    workspace whose events can arrive is the one configured.

    **More than one, and a request that names no workspace is refused.** Which
    install the request belongs to is then unknowable, and every fallback is a
    guess at which of several workspaces to read or write in.

    **More than one, and a team no block answers to is refused.** Falling back
    to the first block is the failure this selection exists to prevent.
    """
    conf = slack_conf if isinstance(slack_conf, Mapping) else {}
    blocks = slack_workspace_blocks(conf)
    if len(blocks) == 1:
        return _overlay_workspace(conf, blocks[0])
    team = str(team_id or "").strip()
    if not blocks:
        raise SlackWorkspaceUnresolved(
            "slack_workspace_unconfigured",
            "channels.slack.workspaces is written but holds no readable"
            " install, so there is no token any Slack tool may use",
        )
    if not team:
        raise SlackWorkspaceUnresolved(
            "slack_request_names_no_workspace",
            f"{len(blocks)} Slack workspaces are configured and this request"
            f" carries no {METADATA_TEAM_KEY}, so which of them it belongs to"
            f" cannot be established; serving it from any of them would be a"
            f" guess",
        )
    lookup = team_of_token if callable(team_of_token) else lambda _token: None
    for block in blocks:
        token = str(block.get(KEY_BOT_TOKEN) or "").strip()
        if not token:
            continue
        if str(lookup(token) or "").strip() == team:
            return _overlay_workspace(conf, block)
    raise SlackWorkspaceUnresolved(
        "slack_workspace_unknown",
        f"no configured Slack workspace is installed in {team}, so this"
        f" request cannot be served; it is a configuration fact rather than a"
        f" permission one",
    )


#: Said once per distinct translation rather than once per call: the cron path
#: resolves on every run, and a per-run deprecation line gets filtered out.
#: Keyed by what was translated, so an operator who changes the legacy value
#: hears about the new one.
_WARNED_LEGACY_TRANSLATIONS: set[tuple[str, str]] = set()


def slack_config(
    config: Mapping[str, Any] | None = None,
    *,
    team_id: str | None = None,
    team_of_token: "Callable[[str], str | None] | None" = None,
) -> Mapping[str, Any]:
    """The ``channels.slack`` block, or an empty mapping.

    Four callers need it and none may import the connector that writes it: the
    history toolkit's settings, the search tool's settings and its enablement
    flag, and the cron scheduler's per-run policy read. ``channels`` and
    ``channels.slack`` are operator-written, so either can be absent or be
    something other than a mapping.

    ``config`` is for a caller that already holds one. With nothing passed the
    live config is read on every call rather than captured at start-up, so an
    operator can edit a key and be obeyed without a restart.

    An unreadable config answers ``{}`` rather than raising. Every caller reads
    an absent block as the feature being unconfigured, and for the two that gate
    a read that means refusing.

    ``team_id`` names the install the caller is serving, and with it the answer
    is that install's own settings: the connector-wide keys unchanged, and the
    ``bot_token`` of the block installed in that team written over whatever the
    top of ``channels.slack`` says. See :func:`select_slack_workspace` for the
    three cases and for what ``team_of_token`` supplies.

    ``None`` and ``""`` are different arguments here, and the difference is the
    point. ``None`` -- the default -- is *this caller reads connector-wide keys
    only*: the cron scheduler's policy read and the search tool's enablement
    flag, neither of which is about an install. ``""`` is *this caller is
    serving a request and the request named no workspace*, which one configured
    install answers and several refuse. A tool must therefore pass the team it
    read off the request even when that read came back empty.

    A deployment with one install configured answers the same mapping either
    way, which is what keeps the live single-workspace deployment reading
    exactly as it did before workspaces were expressible.
    """
    if config is None:
        from jiuwenswarm.common.config import get_config

        try:
            config = get_config() or {}
        except Exception as exc:  # noqa: BLE001 - a read gate must fail closed.
            logger.warning(
                "channels.slack is unreadable; every caller reads it as unset,"
                " which for a read gate means refusing: %s",
                exc,
            )
            return {}
    channels = config.get("channels") if isinstance(config, Mapping) else None
    slack = channels.get("slack") if isinstance(channels, Mapping) else None
    resolved = slack if isinstance(slack, Mapping) else {}
    if team_id is None and team_of_token is None:
        return resolved
    return select_slack_workspace(
        resolved, team_id, team_of_token=team_of_token
    )


def conversation_is_public(info: Mapping[str, Any] | None) -> bool:
    """Whether a ``conversations.info`` record is a room anybody may join.

    Three conditions and all of them explicit. ``is_private`` must be present
    and false -- an absent flag is not read as public, because every gate that
    consults this does so in order to relax or to tighten on publicness having
    been *established* -- and a direct message or a group direct message is
    never public however its private flag reads.

    Here beside the words it relaxes: for a read a public target is the safe
    case and ``visible`` and ``open`` relax the membership rule on it. The
    write ladder does not read it; a write is decided on membership alone.
    """
    if not isinstance(info, Mapping):
        return False
    if info.get("is_private") is not False:
        return False
    if info.get("is_im") or info.get("is_mpim"):
        return False
    return bool(info.get("is_channel"))


def normalize_history_policy(raw: Any) -> str | None:
    """One written value as a policy word, or ``None`` if it is not one.

    ``None`` and ``""`` are not errors and are not words: they are a key nobody
    wrote, or wrote with nothing after the colon, which is how the templates
    ship a key an upgrade must not delete. Both mean *no value here*, leaving
    whatever is below to answer.

    A boolean gets no special reading, which is why the narrowest word is
    ``disabled`` and not ``off``. YAML 1.1 -- what ``safe_load`` implements --
    resolves a bare ``off`` to the boolean false, so ``off`` would be the one
    word of the four an operator could not write unquoted. Both booleans fail
    here as what they are: an unrecognised value, warned about and resolved to
    the shipped default.
    """
    if raw is None:
        return None
    word = str(raw).strip().lower()
    if not word:
        return None
    return word if word in HISTORY_POLICY_VALUES else ""


def id_list(raw: Any) -> tuple[str, ...]:
    """A configured or stamped list of Slack ids, cleaned and de-duplicated.

    Order is preserved rather than sorted, so a warning that quotes the list
    quotes it as the operator wrote it. A bare string is read as the single id
    it plainly is: iterating it instead would silently yield a list of letters.
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        entry = raw.strip()
        return (entry,) if entry else ()
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return ()
    seen: list[str] = []
    for item in raw:
        entry = str(item or "").strip()
        if entry and entry not in seen:
            seen.append(entry)
    return tuple(seen)


def history_policy_from_legacy_channel_ids(entries: Iterable[Any]) -> tuple[str, str]:
    """``history_digest_channel_ids`` as a policy word, plus what was lost.

    Three rows. The third loses information:

    ==========================  ============  ==============================
    ``history_digest_channel_ids``  word      note
    ==========================  ============  ==============================
    ``[]`` or absent            ``disabled``  --
    ``["*"]``                   ``origin``    --
    ``["C0A", "C0B"]``          ``disabled``  per-conversation, not expressible
    ==========================  ============  ==============================

    ``["*"]`` becomes ``origin`` rather than ``members``: the legacy key never
    let a request name another conversation, so translating it to a word that
    does would widen a deployment on upgrade.

    A list of ids becomes ``disabled`` and says so. ``history`` is one word for
    the whole Slack platform, so "these three conversations and no others" has
    no form here, and ``disabled`` is the safe direction. The note is returned
    rather than logged, because only the caller knows whether this resolution is
    the one in force.

    ``"*"`` alongside ids is read as ``"*"``, matching the legacy reader, whose
    test was ``"*" in entries or channel in entries``.
    """
    listed = id_list(entries)
    if not listed:
        return HISTORY_DISABLED, ""
    if "*" in listed:
        return HISTORY_ORIGIN, ""
    return (
        HISTORY_DISABLED,
        f"it named {len(listed)} conversation(s) and per-conversation history"
        f" is no longer expressible: {KEY_HISTORY} is one word for the whole"
        f" Slack connector. History is disabled; set {KEY_HISTORY}:"
        f" {HISTORY_ORIGIN} to restore it for every conversation",
    )


def resolve_history_policy(
    slack_conf: Mapping[str, Any] | None,
    *,
    warn: Any = None,
) -> str:
    """The layer-0 word for ``channels.slack``, legacy key included.

    ``history`` decides on its own whenever it names a known word, so a config
    holding both keys is answered by the new one and the legacy list is not read
    at all -- the same precedence ``acknowledge_mode`` takes over
    ``acknowledge_requests``. An unrecognised word is a misspelling rather than
    a request to fall back: it resolves to the shipped default without reaching
    the legacy key, so that a typo cannot silently buy whatever that list says.

    Fails closed everywhere. Anything that is not a mapping, and any word that
    is not one of the five, lands on ``disabled``.
    """
    emit = warn if callable(warn) else logger.warning
    if not isinstance(slack_conf, Mapping):
        return HISTORY_POLICY_DEFAULT

    word = normalize_history_policy(slack_conf.get(KEY_HISTORY))
    if word:
        return word
    if word == "":
        emit(
            "channels.slack.%s=%r is not one of %s; reading no Slack history"
            " (%s)",
            KEY_HISTORY,
            slack_conf.get(KEY_HISTORY),
            "/".join(HISTORY_POLICY_VALUES),
            HISTORY_POLICY_DEFAULT,
        )
        return HISTORY_POLICY_DEFAULT

    legacy_raw = slack_conf.get(LEGACY_KEY_HISTORY_CHANNEL_IDS)
    if legacy_raw is None:
        return HISTORY_POLICY_DEFAULT
    resolved, note = history_policy_from_legacy_channel_ids(legacy_raw)
    marker = (repr(id_list(legacy_raw)), resolved)
    if marker not in _WARNED_LEGACY_TRANSLATIONS:
        _WARNED_LEGACY_TRANSLATIONS.add(marker)
        emit(
            "channels.slack.%s is deprecated; set %s: %s instead%s",
            LEGACY_KEY_HISTORY_CHANNEL_IDS,
            KEY_HISTORY,
            resolved,
            f". Note: {note}" if note else "",
        )
    return resolved


def resolve_history_never_read(
    slack_conf: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    """Conversations that may never be a target, whoever asks.

    A property of the workspace rather than of one conversation: if a channel
    must never travel, that is true whichever room asks. Absent and ``[]`` both
    mean *nothing is denied*, which is what a deny-list's empty state has to
    mean.
    """
    if not isinstance(slack_conf, Mapping):
        return ()
    return id_list(slack_conf.get(KEY_HISTORY_NEVER_READ))


def resolve_history_exempt_members(
    slack_conf: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    """Members whose presence in the source does not block the subset test.

    A list of ids and not an ``exclude_bots`` boolean. A boolean asserts
    something the operator does not know, cannot be audited, and silently covers
    every integration added later; a list names what was decided and can be read
    back.

    **This is also the whole of what an application gets under ``visible``.**
    That word relaxes a public target for anybody who could reach it on their
    own, and for a person Slack says so: ``users.info`` reports whether they are
    a full member or a guest. For an application it says nothing of the kind.
    Whether an app can read a public channel it has not been added to is decided
    by the OAuth scopes it was installed with, which no call here can see, so
    "this app could have read T anyway" is not a fact the gate can establish --
    and a gate that cannot establish something refuses. An application therefore
    blocks a read exactly as it did before, and an operator who knows better
    names it here, where the decision is one line somebody can read back.
    """
    if not isinstance(slack_conf, Mapping):
        return ()
    return id_list(slack_conf.get(KEY_HISTORY_EXEMPT_MEMBERS))


def history_policy_metadata(
    policy: str,
    *,
    never_read: Iterable[Any] = (),
    exempt_members: Iterable[Any] = (),
) -> dict[str, Any]:
    """The three keys a request holds, always all three.

    Stamped as values rather than left absent even when they are the default.
    An absent policy word means *no Slack connector settled this request*, which
    the runtime gate reads as a refusal; a connector that settled it to
    ``disabled`` is a different thing.

    The lists are stamped rather than read from config on the far side, because
    the runtime must not read connector config: a deployment where the two
    disagree would be a gate evaluated against a policy nobody wrote.
    """
    word = normalize_history_policy(policy) or HISTORY_POLICY_DEFAULT
    return {
        METADATA_POLICY_KEY: word,
        METADATA_NEVER_READ_KEY: list(id_list(never_read)),
        METADATA_EXEMPT_MEMBERS_KEY: list(id_list(exempt_members)),
    }


__all__ = [
    "HISTORY_DISABLED",
    "HISTORY_MEMBERS",
    "HISTORY_OPEN",
    "HISTORY_ORIGIN",
    "HISTORY_POLICY_DEFAULT",
    "HISTORY_POLICY_NAMES_A_TARGET",
    "HISTORY_POLICY_VALUES",
    "HISTORY_VISIBLE",
    "KEY_BOT_TOKEN",
    "KEY_HISTORY",
    "KEY_HISTORY_EXEMPT_MEMBERS",
    "KEY_HISTORY_NEVER_READ",
    "KEY_WORKSPACES",
    "LEGACY_KEY_HISTORY_CHANNEL_IDS",
    "METADATA_ASKER_KEY",
    "METADATA_EXEMPT_MEMBERS_KEY",
    "METADATA_NEVER_READ_KEY",
    "METADATA_ORIGIN_KEY",
    "METADATA_POLICY_KEY",
    "METADATA_TEAM_KEY",
    "ORIGIN_CRON_JOB",
    "SLACK_WORKSPACE_KEYS",
    "SlackWorkspaceUnresolved",
    "conversation_is_public",
    "history_policy_from_legacy_channel_ids",
    "history_policy_metadata",
    "id_list",
    "normalize_history_policy",
    "resolve_history_exempt_members",
    "resolve_history_never_read",
    "resolve_history_policy",
    "select_slack_workspace",
    "slack_config",
    "slack_team_id_from_session",
    "slack_workspace_blocks",
]
