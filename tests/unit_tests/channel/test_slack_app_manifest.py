"""The tiered Slack app manifests, held to the code that needs them.

The manifests under ``jiuwenswarm/resources/slack/`` are what an operator pastes
into Slack to create the app. They are generated from
``jiuwenswarm/common/slack_scope_policy.py``, and this file is what stops either
half drifting from the code:

* every Slack API call in the Slack sources must be classified by the policy
  module -- an unclassified one fails by name;
* every event listener the connector registers must be classified likewise;
* the committed manifests must be exactly what the generator produces now;
* the top tier must cover every scope the policy module asks for, and ask for
  nothing else;
* the tiers must nest, so choosing a smaller one is always a narrowing.

**What this cannot catch.** Stated plainly, because a check trusted beyond its
reach is worse than no check. The three-way split below is between a scope
written as a literal in source, one assembled at runtime, and one that appears
nowhere a machine can bind it to a call.

* **The scope table itself is a human judgement.** Thirteen of the twenty-five
  scopes appear as literals in source; none are assembled at runtime; the other
  twelve appear nowhere a machine can bind them to a call, seven of them nowhere
  in the package at all, because what needs them is Slack's willingness to
  deliver an event. So nothing here can confirm that ``files.info`` really needs
  ``files:read``. If Slack changes its mind, every test in this file still
  passes. What the file does enforce is that no call goes unclassified.
* **A method name assembled at runtime is invisible.** Discovery is static: an
  attribute on a client object, a literal handed to ``api_call``, or a literal
  that is already a known method name. ``getattr(client, method)`` is covered
  only because every ``method`` reaching it is a literal elsewhere in the same
  file. A name built by concatenation, read from config, or passed in from
  another module would not be seen. The ``slack_sdk`` oracle below narrows this
  gap and does not close it, and it is skipped entirely where the SDK is not
  installed.
* **Code outside the Slack sources is not scanned.** The scan covers files whose
  name or directory says "slack".
* **No install is inspected.** This compares artefacts in the repository. It
  says nothing about what a workspace actually granted, and nothing about
  whether Slack will accept a scope at all -- the ``search:read.*`` family is
  suspected of being plan-gated and that suspicion is unverified.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

import pytest
import yaml

from jiuwenswarm.common.slack_events_policy import EVENT_TYPE_FAMILIES
from jiuwenswarm.common.slack_scope_policy import (
    EVENT_REQUIREMENTS,
    MESSAGE_SUBSCRIPTIONS,
    METHOD_REQUIREMENTS,
    TIERS,
    events_for_tier,
    manifest_filename,
    render_manifest,
    scopes_for_tier,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PACKAGE_ROOT = _REPO_ROOT / "jiuwenswarm"
_MANIFEST_DIR = _PACKAGE_ROOT / "resources" / "slack"
_GENERATOR = _REPO_ROOT / "scripts" / "generate_slack_manifest.py"
_CONNECTOR = (
    _PACKAGE_ROOT
    / "gateway"
    / "channel_manager"
    / "im_platforms"
    / "slack"
    / "slack_connect.py"
)

#: Names an attribute on a client object must have to be a Slack method: every
#: method the SDK exposes is ``family_method``. The underscore is what keeps
#: ``client.stream`` -- an httpx download, not Slack -- out of the set.
_SDK_METHOD_RE = re.compile(r"^[a-z]+_[a-zA-Z0-9_]+$")

#: The expressions this module is willing to call a Slack client.
_CLIENT_NAMES = frozenset({"client", "_client", "web_client", "slack_client"})

#: The generic escape hatch for a method the pinned SDK has no wrapper for. Its
#: first argument names the method.
_API_CALL = "api_call"


def _slack_sources() -> list[Path]:
    """Every source file that is part of the Slack integration.

    Selected by name rather than listed, so a module added beside the existing
    ones is scanned without anybody remembering to add it here.
    """
    found = [
        path
        for path in _PACKAGE_ROOT.rglob("*.py")
        if "slack" in path.name.lower()
        or any(
            "slack" in part.lower()
            for part in path.relative_to(_PACKAGE_ROOT).parts[:-1]
        )
    ]
    assert found, f"no Slack sources found under {_PACKAGE_ROOT}"
    return sorted(found)


def _normalise(method: str) -> str:
    """One spelling for a method this repository writes two ways.

    ``reactions.add`` and ``reactions_add`` are the same call: the SDK spelling
    where a wrapper is called, the dotted spelling where the call goes through
    ``api_call`` because the pinned SDK has no wrapper for it.
    """
    return method.replace(".", "_")


def _sdk_methods() -> frozenset[str]:
    """Method names the installed Slack SDK exposes, or an empty set.

    An oracle, not a requirement. It is what lets discovery notice a *new*
    method handed to a per-module call helper as a literal -- a name the policy
    module has never heard of and so cannot recognise on its own. Where the SDK
    is absent that arm is simply not available, and the tests say so rather than
    pretending the narrower scan was the whole one.
    """
    try:
        from slack_sdk.web.async_client import AsyncWebClient
    except Exception:  # noqa: BLE001 - the SDK is optional to this check
        return frozenset()
    return frozenset(
        name
        for name in dir(AsyncWebClient)
        if not name.startswith("_") and _SDK_METHOD_RE.match(name)
    )


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings, for resolving an argument.

    ``api_call`` is handed a constant rather than a literal at every one of its
    call sites, and the constant is what carries the method name.
    """
    constants: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Constant) or not isinstance(
            node.value.value, str
        ):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                constants[target.id] = node.value.value
    return constants


def _is_client(node: ast.expr) -> bool:
    """Whether an expression is something this repository calls Slack through."""
    if isinstance(node, ast.Name):
        return node.id in _CLIENT_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr in _CLIENT_NAMES
    if isinstance(node, ast.Call):
        func = node.func
        return isinstance(func, ast.Attribute) and func.attr.endswith("get_client")
    return False


def _calls_in(path: Path, oracle: frozenset[str]) -> set[str]:
    """The Slack methods one file calls, normalised.

    Three shapes, all three read:

    * ``client.chat_postMessage(...)`` -- an attribute on a client object.
    * ``client.api_call(_SEARCH_METHOD, ...)`` -- the escape hatch, whose first
      argument names the method as a literal or a module constant.
    * ``self._call("pins_list", ...)`` -- a literal handed to a per-module
      helper that does the ``getattr``. Recognised because the literal is a name
      the policy module already knows, or because the SDK oracle says it is a
      method.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    constants = _module_constants(tree)
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _is_client(node.value):
            if node.attr != _API_CALL and _SDK_METHOD_RE.match(node.attr):
                found.add(node.attr)
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == _API_CALL and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.add(_normalise(first.value))
                elif isinstance(first, ast.Name) and first.id in constants:
                    found.add(_normalise(constants[first.id]))
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            candidate = _normalise(node.value)
            if candidate in METHOD_REQUIREMENTS or candidate in oracle:
                found.add(candidate)

    return found


def _registered_events() -> set[str]:
    """The event types the connector has a listener for.

    Two sources, because the connector registers two ways. The named ones are
    literals passed to ``app.event(...)``. The six non-message ones are
    registered by iterating ``EVENT_TYPE_FAMILIES``, which is imported rather
    than re-parsed: that table is the connector's own declaration of which
    events it handles, so reading it directly cannot disagree with it.

    The catch-all ``app.event(_ANY_EVENT_TYPE)`` contributes nothing. It is a
    compiled pattern rather than a literal, and it subscribes to nothing: it is
    a last-resort bolt listener for an event that arrived with no handler.
    """
    tree = ast.parse(_CONNECTOR.read_text(encoding="utf-8"), filename=str(_CONNECTOR))
    literal: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "event"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                literal.add(arg.value)

    assert literal, "no app.event(...) literals found; the connector has moved"

    events: set[str] = set()
    for name in literal:
        if name == "message":
            events.update(MESSAGE_SUBSCRIPTIONS)
        else:
            events.add(name)
    events.update(EVENT_TYPE_FAMILIES)
    return events


def _connector_registers_actions() -> bool:
    """Whether the connector handles ``block_actions`` payloads."""
    tree = ast.parse(_CONNECTOR.read_text(encoding="utf-8"), filename=str(_CONNECTOR))
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "action"
        for node in ast.walk(tree)
    )


def _generator_outputs() -> dict[Path, str]:
    """What ``scripts/generate_slack_manifest.py`` says each file should hold.

    The script is imported rather than re-implemented, so the path it writes to
    and the names it writes under are covered too. A generator that agrees with
    a test that copied it proves nothing.
    """
    spec = importlib.util.spec_from_file_location(
        "generate_slack_manifest", _GENERATOR
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.outputs()


@pytest.fixture(scope="module")
def slack_calls() -> set[str]:
    oracle = _sdk_methods()
    found: set[str] = set()
    for path in _slack_sources():
        found |= _calls_in(path, oracle)
    return found


@pytest.fixture(scope="module")
def manifests() -> dict[int, dict]:
    return {
        tier: yaml.safe_load(
            (_MANIFEST_DIR / manifest_filename(tier)).read_text(encoding="utf-8")
        )
        for tier in TIERS
    }


def _bot_scopes(manifest: dict) -> set[str]:
    return set(manifest["oauth_config"]["scopes"]["bot"])


def _bot_events(manifest: dict) -> set[str]:
    return set(manifest["settings"]["event_subscriptions"]["bot_events"])


def _all_required_scopes() -> set[str]:
    required: set[str] = set()
    for requirement in (*METHOD_REQUIREMENTS.values(), *EVENT_REQUIREMENTS.values()):
        required |= set(requirement.scopes)
    return required


# ── discovery ───────────────────────────────────────────────────────────────


def test_slack_sources_are_found() -> None:
    """The scan reaches the connector and every tool, not just one of them."""
    names = {path.name for path in _slack_sources()}
    for expected in (
        "slack_connect.py",
        "slack_history.py",
        "slack_pins.py",
        "slack_bookmarks.py",
        "slack_reactions.py",
        "slack_search.py",
    ):
        assert expected in names, f"{expected} is no longer covered by the scan"


def test_discovery_finds_all_three_call_shapes(slack_calls: set[str]) -> None:
    """Discovery still reaches every way this repository names a method.

    Without this the scan could quietly stop finding anything -- a renamed
    client attribute, say -- and every other assertion here would pass on an
    empty set.
    """
    for method, shape in (
        ("chat_postMessage", "an attribute on the connector's client"),
        ("files_upload_v2", "an attribute on the connector's client"),
        ("assistant_search_context", "a constant handed to api_call"),
        ("blocks_validate", "a constant handed to api_call"),
        ("assistant_threads_setStatus", "a constant handed to api_call"),
        ("pins_list", "a literal handed to a per-module call helper"),
        ("chat_startStream", "a literal handed to a per-module call helper"),
    ):
        assert method in slack_calls, f"discovery no longer finds {method} ({shape})"


def test_the_sdk_oracle_is_live() -> None:
    """The arm that can notice a method nobody has ever written down.

    Without the SDK the scan still finds every call made through a client
    attribute or ``api_call``, and still recognises any literal the policy
    module already knows -- but a *new* method handed to a per-module call
    helper as a literal would pass unseen. This test says which of the two
    situations the run is in rather than leaving it unsaid.
    """
    oracle = _sdk_methods()
    if not oracle:
        pytest.skip(
            "slack_sdk is not importable, so discovery cannot recognise a Slack"
            " method the policy module has never heard of"
        )
    assert "conversations_history" in oracle
    assert "stream" not in oracle


def test_every_slack_call_is_classified(slack_calls: set[str]) -> None:
    """A Slack call nobody has decided a scope for fails here, by name.

    This is the assertion the rest of the file rests on. Adding a call to the
    connector or to a tool stops the suite until the policy module says what the
    call needs and which tier it belongs to, and saying so is what puts the
    scope into the generated manifests.
    """
    unknown = sorted(slack_calls - set(METHOD_REQUIREMENTS))
    assert not unknown, (
        "Slack methods called but not classified: "
        + ", ".join(unknown)
        + ". Add each to METHOD_REQUIREMENTS in"
        " jiuwenswarm/common/slack_scope_policy.py with the scopes Slack"
        " requires and the tier it belongs to, then run"
        " scripts/generate_slack_manifest.py."
    )


def test_every_registered_event_is_classified() -> None:
    unknown = sorted(_registered_events() - set(EVENT_REQUIREMENTS))
    assert not unknown, (
        "events the connector listens for but nothing classifies: "
        + ", ".join(unknown)
        + ". Add each to EVENT_REQUIREMENTS, then regenerate."
    )


def test_nothing_is_classified_that_is_not_registered() -> None:
    """The other direction: a subscription with no listener is dropped traffic."""
    extra = sorted(set(EVENT_REQUIREMENTS) - _registered_events())
    assert not extra, (
        "EVENT_REQUIREMENTS names events the connector has no listener for: "
        + ", ".join(extra)
    )


# ── the committed files ─────────────────────────────────────────────────────


def test_manifests_are_up_to_date() -> None:
    """The committed files are exactly what the generator produces now."""
    for path, expected in _generator_outputs().items():
        assert path.exists(), f"{path} is missing; run scripts/generate_slack_manifest.py"
        actual = path.read_text(encoding="utf-8")
        assert actual == expected, (
            f"{path.name} is stale. Run scripts/generate_slack_manifest.py and"
            " commit the result; do not edit a generated manifest by hand."
        )


def test_one_manifest_per_tier() -> None:
    written = {path.name for path in _generator_outputs()}
    on_disk = {path.name for path in _MANIFEST_DIR.glob("*.yaml")}
    assert written == on_disk, (
        "the manifest directory holds files the generator does not own, or is"
        f" missing one it does: generator={sorted(written)} on disk={sorted(on_disk)}"
    )


def test_top_tier_covers_every_scope_the_policy_asks_for(
    manifests: dict[int, dict]
) -> None:
    missing = sorted(_all_required_scopes() - _bot_scopes(manifests[TIERS[-1]]))
    assert not missing, (
        "the top tier does not ask for scopes the policy module requires: "
        + ", ".join(missing)
    )


def test_top_tier_asks_for_nothing_the_policy_does_not(
    manifests: dict[int, dict]
) -> None:
    """A scope nothing needs is a permission every install holds for no reason."""
    extra = sorted(_bot_scopes(manifests[TIERS[-1]]) - _all_required_scopes())
    assert not extra, (
        "the top tier asks for scopes nothing needs: " + ", ".join(extra)
    )


def test_tiers_nest(manifests: dict[int, dict]) -> None:
    """Choosing a lower tier is always a narrowing and never a different app."""
    for lower, higher in zip(TIERS, TIERS[1:]):
        assert _bot_scopes(manifests[lower]) <= _bot_scopes(manifests[higher]), (
            f"tier {lower} asks for a scope tier {higher} does not"
        )
        assert _bot_events(manifests[lower]) <= _bot_events(manifests[higher]), (
            f"tier {lower} subscribes to an event tier {higher} does not"
        )


def test_each_manifest_matches_its_tier(manifests: dict[int, dict]) -> None:
    for tier in TIERS:
        assert _bot_scopes(manifests[tier]) == set(scopes_for_tier(tier))
        assert _bot_events(manifests[tier]) == set(events_for_tier(tier))


def test_every_subscribed_event_carries_its_scope(manifests: dict[int, dict]) -> None:
    """A subscription without its scope is accepted by Slack and never delivered."""
    for tier in TIERS:
        scopes = _bot_scopes(manifests[tier])
        for event in sorted(_bot_events(manifests[tier])):
            missing = sorted(set(EVENT_REQUIREMENTS[event].scopes) - scopes)
            assert not missing, (
                f"tier {tier} subscribes to {event} without {', '.join(missing)}"
            )


# ── settings that are not scopes ────────────────────────────────────────────


def test_interactivity_is_enabled_in_every_tier(manifests: dict[int, dict]) -> None:
    """The question, approval and stop buttons are dead without it.

    No Request URL is asserted and none should be set: with Socket Mode on, a
    ``block_actions`` payload arrives on the same WebSocket as the events.
    """
    assert _connector_registers_actions(), (
        "the connector no longer registers app.action listeners; if the buttons"
        " are gone, interactivity can be turned off in the manifests too"
    )
    for tier in TIERS:
        interactivity = manifests[tier]["settings"]["interactivity"]
        assert interactivity["is_enabled"] is True
        assert "request_url" not in interactivity


def test_socket_mode_and_no_request_url(manifests: dict[int, dict]) -> None:
    for tier in TIERS:
        settings = manifests[tier]["settings"]
        assert settings["socket_mode_enabled"] is True
        assert "request_url" not in settings["event_subscriptions"]


def test_no_manifest_describes_distribution(manifests: dict[int, dict]) -> None:
    """One app per workspace, with tokens pasted into config.

    A manifest creates an app; it is not a route to distributing one. A user
    token scope or a redirect URL appearing here would mean the install model
    changed and the setup guides no longer describe it.
    """
    for tier in TIERS:
        manifest = manifests[tier]
        assert manifest["settings"].get("org_deploy_enabled") is False
        assert manifest["settings"].get("token_rotation_enabled") is False
        oauth = manifest["oauth_config"]
        assert "redirect_urls" not in oauth
        assert "user" not in oauth["scopes"]


def test_render_is_deterministic() -> None:
    """Two renders agree, so a regenerate-and-diff check cannot flap."""
    for tier in TIERS:
        assert render_manifest(tier) == render_manifest(tier)


def test_an_unknown_tier_is_refused() -> None:
    with pytest.raises(ValueError):
        render_manifest(max(TIERS) + 1)
