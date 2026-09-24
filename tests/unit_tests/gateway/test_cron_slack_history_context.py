# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Which cron jobs may read Slack history, and which may only claim they can.

``CronJob.session_id`` is accepted verbatim by the gateway cron RPCs. While it
only chooses where a result is delivered that is a routing question; the moment
it also chooses what may be *read* it becomes an access-control question, and
these tests pin the answer: a job reads a Slack channel only when the request
that created it was itself in that channel.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from jiuwenswarm.gateway.cron.controller import CronController
from jiuwenswarm.gateway.cron.models import CronJob
from jiuwenswarm.common.slack_routing import (
    parse_slack_cron_session,
    slack_cron_session_is_trusted,
    slack_history_metadata_for_cron_job,
)
from jiuwenswarm.gateway.cron.store import CronJobStore

# The conversation a Slack turn happens in, and the session id the connector
# derives from it (``slack_{team}_{channel}`` plus an optional target field).
_SLACK_CHANNEL = "C-ONE"
_SLACK_DM_CHANNEL = "D-ONE"
_SLACK_SESSION = f"slack_T-TEAM_{_SLACK_CHANNEL}_U-ONE"
_SLACK_THREAD_SESSION = f"slack_T-TEAM_{_SLACK_CHANNEL}_1712345678.000100"
# The target-less form: a session belonging to the channel itself, with nothing
# narrower than the channel to be delivered into or read from.
_SLACK_CHANNEL_SESSION = f"slack_T-TEAM_{_SLACK_CHANNEL}"
# The third target form: an opaque discriminator rather than a user id or a
# thread ts. This is the form a job that posts on its own schedule takes,
# there being no user and no thread for it to be named after.
_SLACK_DIGEST_SESSION = f"slack_T-TEAM_{_SLACK_CHANNEL}_nightly-digest"
# A session id of the same shape naming a channel the creator was never in.
_FORGED_SESSION = "slack_T-TEAM_C-PRIVATE_U-ATTACKER"
_FORGED_CHANNEL_SESSION = "slack_T-TEAM_C-PRIVATE"


class _StubScheduler:
    """Scheduler stand-in: reload, plus the project fence create_job consults."""

    def __init__(self) -> None:
        self.reloads = 0

    async def reload(self) -> None:
        self.reloads += 1

    async def project_execution_allowed(self, project_id: str, user_id: str) -> bool:
        return True


def _job(**overrides: Any) -> CronJob:
    defaults: dict[str, Any] = {
        "id": "job-1",
        "name": "digest",
        "enabled": True,
        "cron_expr": "0 0 9 * * ? *",
        "timezone": "Asia/Shanghai",
        "description": "summarise the channel",
        "targets": "slack",
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    defaults.update(overrides)
    return CronJob(**defaults)


def _create_params(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "name": "digest",
        "cron_expr": "0 0 9 * * ? *",
        "timezone": "Asia/Shanghai",
        "description": "summarise the channel",
        "targets": "slack",
    }
    params.update(overrides)
    return params


@pytest.fixture
def controller(tmp_path: Any) -> CronController:
    return CronController(
        store=CronJobStore(path=tmp_path / "cron_jobs.json"),
        scheduler=_StubScheduler(),
    )


@pytest.fixture
def allow_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live deployment's setting: every conversation reads its own history."""
    import jiuwenswarm.common.config as config_module

    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {"channels": {"slack": {"history": "origin"}}},
    )


# ── The session id, parsed ───────────────────────────────────────────────────


def test_slack_cron_session_names_its_channel_and_thread() -> None:
    assert parse_slack_cron_session(_SLACK_SESSION) == (_SLACK_CHANNEL, "")
    assert parse_slack_cron_session(_SLACK_THREAD_SESSION) == (
        _SLACK_CHANNEL,
        "1712345678.000100",
    )


def test_slack_cron_session_with_a_discriminator_names_only_a_channel() -> None:
    assert parse_slack_cron_session(_SLACK_DIGEST_SESSION) == (_SLACK_CHANNEL, "")


def test_a_session_named_by_channel_alone_still_names_its_channel() -> None:
    """No target field is a form of the id, not a broken one.

    The channel is spelled out in the same position it occupies in every other
    form, and the empty thread ts is the answer the dot rule already gives every
    target that is not a ts: deliver to, and read from, the channel root.
    """
    assert parse_slack_cron_session(_SLACK_CHANNEL_SESSION) == (_SLACK_CHANNEL, "")


@pytest.mark.parametrize(
    "session_id",
    [
        "",
        None,
        "web_1234",
        "slack_",
        "slack_T-TEAM",
        "slack_T-TEAM_",
        "slack__U-ONE",
        "dingtalk::c::s::1",
    ],
)
def test_non_slack_sessions_name_nothing(session_id: Any) -> None:
    assert parse_slack_cron_session(session_id) == ("", "")


# ── Trust, established at creation ───────────────────────────────────────────


def test_a_slack_turn_vouches_for_its_own_session() -> None:
    assert slack_cron_session_is_trusted(
        request_channel_id="slack",
        request_session_id=_SLACK_SESSION,
        job_session_id=_SLACK_SESSION,
    )


def test_a_slack_turn_does_not_vouch_for_another_channels_session() -> None:
    """The security case, at the level of the rule itself.

    An agent turn running in ``C-ONE`` asks for a job bound to a session naming
    a channel it is not in. The shape is right and the request really is from
    Slack; it is still not this request's session, so it is not proven.
    """
    assert not slack_cron_session_is_trusted(
        request_channel_id="slack",
        request_session_id=_SLACK_SESSION,
        job_session_id=_FORGED_SESSION,
    )


@pytest.mark.parametrize("request_channel_id", ["web", "tui", "__cron__", "", None])
def test_only_a_slack_request_can_vouch_for_a_slack_session(
    request_channel_id: Any,
) -> None:
    assert not slack_cron_session_is_trusted(
        request_channel_id=request_channel_id,
        request_session_id=_SLACK_SESSION,
        job_session_id=_SLACK_SESSION,
    )


def test_a_channel_session_is_vouched_for_on_the_same_terms_as_any_other() -> None:
    """Reading the target-less form buys a job no trust its provenance lacks.

    Trust rests on the job's session id being the creating request's own, so a
    session named by channel alone is proven when it is that request's session
    and refused when it names some other channel -- exactly as a session with a
    target is.
    """
    assert slack_cron_session_is_trusted(
        request_channel_id="slack",
        request_session_id=_SLACK_CHANNEL_SESSION,
        job_session_id=_SLACK_CHANNEL_SESSION,
    )
    assert not slack_cron_session_is_trusted(
        request_channel_id="slack",
        request_session_id=_SLACK_CHANNEL_SESSION,
        job_session_id=_FORGED_CHANNEL_SESSION,
    )
    assert not slack_cron_session_is_trusted(
        request_channel_id="web",
        request_session_id="web_42",
        job_session_id=_SLACK_CHANNEL_SESSION,
    )


# ── Trust, recorded on the job ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_job_created_on_a_slack_turn_is_recorded_as_trusted(
    controller: CronController,
) -> None:
    created = await controller.create_job(
        _create_params(session_id=_SLACK_SESSION),
        request_channel_id="slack",
        request_session_id=_SLACK_SESSION,
    )

    assert created["session_id"] == _SLACK_SESSION
    assert created["slack_session_trusted"] is True


@pytest.mark.asyncio
async def test_hand_written_slack_session_is_created_but_never_trusted(
    controller: CronController,
) -> None:
    """The security case, end to end.

    Anyone who can reach the cron RPC can type a session id naming any channel
    the bot is in. The job is still created and will still deliver there -- that
    is long-standing behaviour and not this change's to alter -- but it is not
    recorded as having a Slack context, so it reads nothing.
    """
    created = await controller.create_job(
        _create_params(session_id=_FORGED_SESSION),
        request_channel_id="web",
        request_session_id="web_42",
    )

    assert created["session_id"] == _FORGED_SESSION
    assert "slack_session_trusted" not in created
    assert slack_history_metadata_for_cron_job(_job(**{
        "session_id": _FORGED_SESSION,
        "slack_session_trusted": False,
    })) == {}


@pytest.mark.asyncio
async def test_caller_supplied_trust_flag_is_ignored(
    controller: CronController,
) -> None:
    """A create call cannot vouch for itself by setting the flag."""
    created = await controller.create_job(
        _create_params(session_id=_FORGED_SESSION, slack_session_trusted=True),
        request_channel_id="web",
        request_session_id="web_42",
    )

    assert "slack_session_trusted" not in created


@pytest.mark.asyncio
async def test_repointing_the_session_from_elsewhere_revokes_trust(
    controller: CronController,
) -> None:
    created = await controller.create_job(
        _create_params(session_id=_SLACK_SESSION),
        request_channel_id="slack",
        request_session_id=_SLACK_SESSION,
    )

    patched = await controller.update_job(
        created["id"],
        {"session_id": _FORGED_SESSION, "slack_session_trusted": True},
        request_channel_id="web",
        request_session_id="web_42",
    )

    assert patched["session_id"] == _FORGED_SESSION
    assert "slack_session_trusted" not in patched


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch",
    [
        {"enabled": False},
        # A targets edit rewrites session_id with the value it already had; that
        # is not a session change and must not revoke anything.
        {"targets": "web"},
    ],
    ids=["unrelated-field", "targets-only"],
)
async def test_an_unrelated_edit_keeps_a_proven_session_trusted(
    controller: CronController,
    patch: dict[str, Any],
) -> None:
    created = await controller.create_job(
        _create_params(session_id=_SLACK_SESSION),
        request_channel_id="slack",
        request_session_id=_SLACK_SESSION,
    )

    patched = await controller.update_job(created["id"], patch)

    assert patched["session_id"] == _SLACK_SESSION
    assert patched["slack_session_trusted"] is True


# ── Trust, persisted ─────────────────────────────────────────────────────────


def test_trust_survives_the_dict_round_trip() -> None:
    job = _job(session_id=_SLACK_SESSION, slack_session_trusted=True)

    restored = CronJob.from_dict(job.to_dict())

    assert restored.slack_session_trusted is True
    assert restored.to_dict()["slack_session_trusted"] is True


def test_a_record_written_before_this_field_existed_loads_untrusted() -> None:
    legacy = _job(session_id=_SLACK_SESSION).to_dict()
    legacy.pop("slack_session_trusted", None)

    restored = CronJob.from_dict(legacy)

    assert restored.slack_session_trusted is False
    assert slack_history_metadata_for_cron_job(restored) == {}


# ── The metadata a run presents ──────────────────────────────────────────────


@pytest.mark.usefixtures("allow_all")
def test_a_trusted_job_presents_its_channel_and_thread() -> None:
    metadata = slack_history_metadata_for_cron_job(
        _job(session_id=_SLACK_THREAD_SESSION, slack_session_trusted=True)
    )

    assert metadata == {
        "slack_channel_id": _SLACK_CHANNEL,
        "slack_channel_type": "channel",
        "slack_history_origin": "cron_job",
        "slack_history_policy": "origin",
        "slack_history_never_read": [],
        "slack_history_exempt_members": [],
        # The posting ladder, settled per run beside the reading one so that a
        # deployment which has since narrowed it stops posting on the next run,
        # and stamped even when it says "disabled": the runtime reads an absent
        # word as "nobody with the configuration settled this request".
        "slack_write_policy": "disabled",
        "slack_thread_ts": "1712345678.000100",
    }


@pytest.mark.usefixtures("allow_all")
def test_a_trusted_channel_job_presents_its_channel_and_no_thread() -> None:
    metadata = slack_history_metadata_for_cron_job(
        _job(session_id=_SLACK_CHANNEL_SESSION, slack_session_trusted=True)
    )

    assert metadata["slack_channel_id"] == _SLACK_CHANNEL
    assert metadata["slack_channel_type"] == "channel"
    assert "slack_thread_ts" not in metadata


@pytest.mark.usefixtures("allow_all")
def test_a_trusted_dm_job_presents_the_direct_message_type() -> None:
    metadata = slack_history_metadata_for_cron_job(
        _job(
            session_id=f"slack_T-TEAM_{_SLACK_DM_CHANNEL}_U-ONE",
            slack_session_trusted=True,
        )
    )

    assert metadata["slack_channel_id"] == _SLACK_DM_CHANNEL
    assert metadata["slack_channel_type"] == "im"


def test_the_policy_is_consulted_on_every_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Narrowing ``channels.slack.history`` narrows cron, with no rewrite."""
    import jiuwenswarm.common.config as config_module

    job = _job(session_id=_SLACK_SESSION, slack_session_trusted=True)

    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {"channels": {"slack": {"history": "members"}}},
    )
    assert (
        slack_history_metadata_for_cron_job(job)["slack_history_policy"] == "members"
    )

    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {"channels": {"slack": {"history": "disabled"}}},
    )
    narrowed = slack_history_metadata_for_cron_job(job)
    assert narrowed["slack_channel_id"] == _SLACK_CHANNEL
    assert narrowed["slack_history_policy"] == "disabled"


@pytest.mark.usefixtures("allow_all")
@pytest.mark.parametrize(
    "job",
    [
        # A web-created job: no session at all.
        _job(targets="web", session_id=None),
        # A TUI-created job whose session is not a Slack one.
        _job(targets="tui", session_id="tui_1234"),
        # A Slack-shaped session whose provenance was never established.
        _job(session_id=_FORGED_SESSION),
    ],
    ids=["web", "tui", "unproven-slack-session"],
)
def test_a_job_without_a_proven_slack_session_presents_nothing(job: CronJob) -> None:
    assert slack_history_metadata_for_cron_job(job) == {}


# ── The policy a run is held to ──────────────────────────────────────────────
#
# A cron run is not held to a weaker or a stronger rule than a live message. It
# has a conversation and no sender, and the guarantee the membership rule makes
# is about the audience: nobody in the conversation the answer lands in learns
# anything they could not already learn. That is checkable without knowing who
# scheduled the job. What the scheduler owes the gate is therefore the same
# three values a connector stamps, settled from the same resolver.


def test_a_run_carries_the_same_three_keys_a_live_message_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One resolver, so the two paths cannot disagree about one conversation."""
    import jiuwenswarm.common.config as config_module

    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {
            "channels": {
                "slack": {
                    "history": "open",
                    "history_never_read": ["C-SECRET"],
                    "history_exempt_members": ["B-PAGERDUTY"],
                }
            }
        },
    )
    metadata = slack_history_metadata_for_cron_job(
        _job(session_id=_SLACK_SESSION, slack_session_trusted=True)
    )

    assert metadata["slack_history_policy"] == "open"
    assert metadata["slack_history_never_read"] == ["C-SECRET"]
    assert metadata["slack_history_exempt_members"] == ["B-PAGERDUTY"]
    # And no sender, because there is not one. The gate reads the origin marker
    # beside it and applies the subset rule without an asker term.
    assert "slack_user_id" not in metadata
    assert metadata["slack_history_origin"] == "cron_job"


def test_the_deprecated_key_still_drives_a_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The migration reaches cron because both paths call the same resolver.

    A deployment that has not yet written ``history`` keeps the runs it has --
    which is the whole reason the deprecated key is still read, and still
    shipped.
    """
    import jiuwenswarm.common.config as config_module

    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {"channels": {"slack": {"history_digest_channel_ids": ["*"]}}},
    )
    metadata = slack_history_metadata_for_cron_job(
        _job(session_id=_SLACK_SESSION, slack_session_trusted=True)
    )
    assert metadata["slack_history_policy"] == "origin"


def test_a_config_that_cannot_be_read_reads_no_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read gate fails closed, including when the gate's own input is gone."""
    import jiuwenswarm.common.config as config_module

    def _explode() -> dict[str, Any]:
        raise RuntimeError("config file went away")

    monkeypatch.setattr(config_module, "get_config", _explode)
    metadata = slack_history_metadata_for_cron_job(
        _job(session_id=_SLACK_SESSION, slack_session_trusted=True)
    )
    assert metadata["slack_history_policy"] == "disabled"


@pytest.mark.usefixtures("allow_all")
def test_a_trusted_job_whose_session_no_longer_names_a_channel_presents_nothing() -> (
    None
):
    """No delivery conversation is not an empty one.

    With no room the answer lands in, there is no audience for the subset rule
    to check the disclosure against -- so there is nothing to allow, and the
    tool is not mounted at all rather than mounted and stamped ``disabled``.
    """
    assert (
        slack_history_metadata_for_cron_job(
            _job(session_id="web_session_42", slack_session_trusted=True)
        )
        == {}
    )


def test_the_origin_marker_has_one_declaration() -> None:
    """The scheduler and the gate read one name, declared once.

    It is a literal on the wire -- request metadata is JSON -- and the way a
    literal on both sides of a process boundary stays in step is that only one
    side owns it. This module keeps its own long-standing names as aliases.
    """
    from jiuwenswarm.common import slack_history_policy
    from jiuwenswarm.common import slack_routing

    assert slack_routing.SLACK_HISTORY_ORIGIN_KEY == (
        slack_history_policy.METADATA_ORIGIN_KEY
    )
    assert slack_routing.SLACK_HISTORY_ORIGIN_CRON_JOB == (
        slack_history_policy.ORIGIN_CRON_JOB
    )
