"""The event vocabulary and the buffer, with no Slack and no connector in sight."""

from __future__ import annotations

import pytest

from jiuwenswarm.common.slack_events_policy import (
    DROP_EXPIRED,
    DROP_OVERFLOW,
    EVENT_ANCHORED_FAMILIES,
    EVENT_BUFFER_LIMITS,
    EVENT_CONTEXT,
    EVENT_DISPOSITION_DEFAULT,
    EVENT_FAMILIES,
    EVENT_FAMILY_APP_HOME,
    EVENT_FAMILY_MEMBER,
    EVENT_FAMILY_PIN,
    EVENT_FAMILY_REACTION,
    EVENT_OFF,
    EVENT_TURN,
    EVENT_TURN_FAMILIES,
    EVENT_TURN_WITHHELD,
    EVENT_TYPE_FAMILIES,
    InboundEventBuffer,
    disposition_holds,
    event_actor_id,
    event_anchor,
    event_chat_id,
    event_disposition,
    event_family,
    family_anchors_on_a_message,
    normalize_event_policy,
    render_event,
    turn_withheld_reason,
)


class _Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _reaction(emoji: str = "eyes", user: str = "U1", ts: str = "1710000000.000100"):
    return {
        "type": "reaction_added",
        "user": user,
        "reaction": emoji,
        "item": {"type": "message", "channel": "C1", "ts": ts},
        "item_user": "U-BOT",
        "event_ts": "1710000100.000200",
    }


def _pin(user: str = "U1"):
    return {
        "type": "pin_added",
        "user": user,
        "channel_id": "C1",
        "item": {
            "type": "message",
            "channel": "C1",
            "message": {"ts": "1710000000.000100", "text": "keep this"},
        },
        "event_ts": "1710000200.000300",
    }


def _member(user: str = "U1", kind: str = "member_joined_channel"):
    return {
        "type": kind,
        "user": user,
        "channel": "C1",
        "channel_type": "C",
        "team": "T1",
        "event_ts": "1710000300.000400",
    }


def _app_home(user: str = "U1", tab: str = "home", *, view: bool = True):
    """One app_home_opened as Slack sends it.

    ``channel`` is the direct message between that person and this app, which
    is what Slack puts on this event and what the Messages tab of the App Home
    already is. ``view`` is the currently published Home tab and is the reason
    the payload can be large; it is present by default here so that every test
    reading a record is reading one rendered from the fuller shape.
    """
    event = {
        "type": "app_home_opened",
        "user": user,
        "channel": "D1",
        "event_ts": "1710000400.000500",
        "tab": tab,
    }
    if view:
        event["view"] = {
            "id": "V1",
            "team_id": "T1",
            "type": "home",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "x" * 400}}
            ],
            "state": {"values": {}},
            "hash": "1710000000.abcdefgh",
            "private_metadata": "",
        }
    return event


# ----------------------------------------------------------------------
# The vocabulary
# ----------------------------------------------------------------------


def test_every_family_covers_both_directions_of_its_state_change() -> None:
    # A room recording additions and dropping removals leaves the model
    # believing a stale picture is current, which is worse than seeing neither.
    #
    # app_home is one event type rather than a pair because Slack publishes
    # one: there is no closing event to drop, so the property this test exists
    # for is not at stake there.
    by_family: dict[str, set[str]] = {family: set() for family in EVENT_FAMILIES}
    for event_type, family in EVENT_TYPE_FAMILIES.items():
        by_family[family].add(event_type)
    assert by_family == {
        EVENT_FAMILY_REACTION: {"reaction_added", "reaction_removed"},
        EVENT_FAMILY_PIN: {"pin_added", "pin_removed"},
        EVENT_FAMILY_MEMBER: {"member_joined_channel", "member_left_channel"},
        EVENT_FAMILY_APP_HOME: {"app_home_opened"},
    }


def test_unset_means_off_for_every_family() -> None:
    for family in EVENT_FAMILIES:
        assert event_disposition(None, family) == EVENT_OFF
        assert event_disposition({}, family) == EVENT_OFF


def test_a_family_set_to_context_is_the_only_one_that_buffers() -> None:
    policy = {EVENT_FAMILY_REACTION: EVENT_CONTEXT}
    assert event_disposition(policy, EVENT_FAMILY_REACTION) == EVENT_CONTEXT
    assert event_disposition(policy, EVENT_FAMILY_PIN) == EVENT_OFF


def test_normalize_settles_case_and_space_and_drops_what_it_does_not_know() -> None:
    assert normalize_event_policy(
        {"  Reaction ": " Context ", "pin": "off", "nonsense": "context",
         "member": "nonsense"}
    ) == {EVENT_FAMILY_REACTION: EVENT_CONTEXT, EVENT_FAMILY_PIN: EVENT_OFF}
    assert normalize_event_policy("context") is None


# ----------------------------------------------------------------------
# turn: the third word, and the family that cannot have it
# ----------------------------------------------------------------------


def test_turn_round_trips_through_the_normaliser_for_the_families_that_take_it() -> None:
    # Case and surrounding space settled here the way they are for the other
    # two words: a value that passed through is canonical.
    assert normalize_event_policy({" Reaction ": " Turn ", "pin": "TURN"}) == {
        EVENT_FAMILY_REACTION: EVENT_TURN,
        EVENT_FAMILY_PIN: EVENT_TURN,
    }
    policy = normalize_event_policy({"reaction": "turn"})
    assert event_disposition(policy, EVENT_FAMILY_REACTION) == EVENT_TURN
    # Naming one family says nothing about the others, which is the property the
    # mapping shape exists for.
    assert event_disposition(policy, EVENT_FAMILY_PIN) == EVENT_OFF


def test_the_three_state_change_families_take_turn() -> None:
    # member used to be refused for having no message to anchor a reply to. It
    # does not need one: a membership change is about the room, and the room's
    # top level is where a turn woken by one answers.
    assert set(EVENT_TURN_FAMILIES) == {
        EVENT_FAMILY_REACTION,
        EVENT_FAMILY_PIN,
        EVENT_FAMILY_MEMBER,
    }
    for family in EVENT_TURN_FAMILIES:
        assert disposition_holds(family, EVENT_TURN)
        assert normalize_event_policy({family: "turn"}) == {family: EVENT_TURN}
        assert event_disposition({family: EVENT_TURN}, family) == EVENT_TURN


def test_app_home_takes_off_and_context_and_nothing_else() -> None:
    # The whole of the vocabulary this family has.
    assert disposition_holds(EVENT_FAMILY_APP_HOME, EVENT_OFF)
    assert disposition_holds(EVENT_FAMILY_APP_HOME, EVENT_CONTEXT)
    assert not disposition_holds(EVENT_FAMILY_APP_HOME, EVENT_TURN)

    assert normalize_event_policy({"app_home": "context"}) == {
        EVENT_FAMILY_APP_HOME: EVENT_CONTEXT
    }
    assert normalize_event_policy({"app_home": "off"}) == {
        EVENT_FAMILY_APP_HOME: EVENT_OFF
    }
    assert (
        event_disposition({EVENT_FAMILY_APP_HOME: EVENT_CONTEXT}, EVENT_FAMILY_APP_HOME)
        == EVENT_CONTEXT
    )

    # turn is dropped by the normaliser and floors at off at the read gate,
    # rather than falling back to the next word down. A mapping that reached
    # either of those with turn on this family was not vetted by the loader,
    # and guessing at what the operator meant is not the answer to that.
    assert normalize_event_policy({"app_home": "turn"}) == {}
    assert (
        event_disposition({EVENT_FAMILY_APP_HOME: EVENT_TURN}, EVENT_FAMILY_APP_HOME)
        == EVENT_OFF
    )


def test_the_reason_turn_is_withheld_names_the_tab_and_the_payload_field() -> None:
    # The refusal an operator reads has to say why this family and not families
    # in general, because the two answers lead to different next edits.
    reason = turn_withheld_reason(EVENT_FAMILY_APP_HOME)
    assert reason == EVENT_TURN_WITHHELD[EVENT_FAMILY_APP_HOME]
    assert "no message to answer" in reason
    assert "splits on a payload field" in reason
    assert "tab" in reason

    # A family nobody wrote a reason for is still refused, and is told that the
    # reason is what is missing rather than given an invented one.
    assert turn_withheld_reason("star") != reason
    assert "nothing says where a turn woken by one of these would post" in (
        turn_withheld_reason("star")
    )


def test_the_word_still_needs_a_family_with_a_destination() -> None:
    # EVENT_TURN_FAMILIES is written out rather than derived, so a family
    # arriving with nobody having decided where a turn woken by it would answer
    # is refused. app_home is the declared family on that side of the gate; an
    # undeclared name is refused by the same predicate.
    assert not disposition_holds("star", EVENT_TURN)
    # And the floor, not the next word down, for a mapping nobody vetted.
    assert event_disposition({"star": EVENT_TURN}, "star") == EVENT_OFF


def test_only_the_message_families_anchor_on_a_message() -> None:
    assert set(EVENT_ANCHORED_FAMILIES) == {EVENT_FAMILY_REACTION, EVENT_FAMILY_PIN}
    assert family_anchors_on_a_message(EVENT_FAMILY_REACTION)
    assert family_anchors_on_a_message(EVENT_FAMILY_PIN)
    # Not a gap: the room is the destination, settled by what the event is
    # about rather than by what the payload happens to carry.
    assert not family_anchors_on_a_message(EVENT_FAMILY_MEMBER)
    # app_home is unanchored too, and unlike member it has no destination of
    # its own either, which is why it is outside EVENT_TURN_FAMILIES.
    assert not family_anchors_on_a_message(EVENT_FAMILY_APP_HOME)
    assert EVENT_FAMILY_APP_HOME not in EVENT_TURN_FAMILIES


def test_unset_is_byte_identical_to_off_for_every_family_and_every_word() -> None:
    # The whole of the promise made to a deployment that never writes the key.
    for family in EVENT_FAMILIES:
        assert event_disposition(None, family) == EVENT_DISPOSITION_DEFAULT
        assert event_disposition({}, family) == EVENT_DISPOSITION_DEFAULT
        assert EVENT_DISPOSITION_DEFAULT == EVENT_OFF


def test_a_reaction_anchors_on_the_message_it_names() -> None:
    ts, thread_ts = event_anchor(_reaction(), family=EVENT_FAMILY_REACTION)
    assert ts == "1710000000.000100"
    # The payload says nothing about a thread, so the anchor is its own root.
    assert thread_ts == ts


def test_a_pin_anchors_on_the_message_nested_inside_its_item() -> None:
    ts, thread_ts = event_anchor(_pin(), family=EVENT_FAMILY_PIN)
    assert ts == "1710000000.000100"
    assert thread_ts == ts


def test_an_anchor_inside_a_thread_keeps_that_thread() -> None:
    event = _pin()
    event["item"]["message"]["thread_ts"] = "1709000000.000001"
    ts, thread_ts = event_anchor(event, family=EVENT_FAMILY_PIN)
    assert (ts, thread_ts) == ("1710000000.000100", "1709000000.000001")


def test_an_event_naming_no_message_has_no_anchor() -> None:
    # Two empty strings, never one of them, and it means two things: for a
    # reaction on a *file* it means this event lost the message its family
    # promised; for member it is the permanent and correct answer.
    on_a_file = {
        "type": "reaction_added",
        "user": "U1",
        "reaction": "eyes",
        "item": {"type": "file", "channel": "C1", "file": "F1"},
    }
    assert event_anchor(on_a_file, family=EVENT_FAMILY_REACTION) == ("", "")
    assert event_anchor(_member(), family=EVENT_FAMILY_MEMBER) == ("", "")
    # And for app_home, which names no message and never will: nothing in the
    # payload is standing in for one.
    assert event_anchor(_app_home(), family=EVENT_FAMILY_APP_HOME) == ("", "")


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------


def test_a_reaction_renders_with_the_settled_field_names() -> None:
    record = render_event(_reaction(), family=EVENT_FAMILY_REACTION)
    assert record == {
        "family": "reaction",
        "event_type": "reaction_added",
        "event_ts_iso_utc": "2024-03-09T16:01:40.000200Z",
        "author_user_id": "U1",
        "author_name": "U1",
        "emoji_name": "eyes",
        "ts": "1710000000.000100",
        "ts_iso_utc": "2024-03-09T16:00:00.000100Z",
    }


def test_author_fields_name_the_actor_not_the_author_of_the_item() -> None:
    # item_user is the author of the message reacted to. Reading that one would
    # be filtering and labelling the wrong person.
    record = render_event(_reaction(user="U-REACTOR"), family=EVENT_FAMILY_REACTION)
    assert record["author_user_id"] == "U-REACTOR"
    assert event_actor_id(_reaction(user="U-REACTOR")) == "U-REACTOR"


def test_a_pin_finds_the_message_ts_one_level_deeper() -> None:
    record = render_event(_pin(), family=EVENT_FAMILY_PIN)
    assert record["ts"] == "1710000000.000100"
    assert record["event_type"] == "pin_added"
    assert "emoji_name" not in record


def test_a_membership_change_carries_no_message_identifier() -> None:
    record = render_event(_member(), family=EVENT_FAMILY_MEMBER)
    assert "ts" not in record
    assert "ts_iso_utc" not in record
    assert record["author_user_id"] == "U1"


def test_an_app_home_opening_renders_with_the_tab_it_was_opened_on() -> None:
    # Both values, because the two are different situations and a record that
    # reported them as one thing would be the defect this family's field exists
    # to avoid: messages is the direct message conversation itself, home is the
    # published view beside it.
    for tab in ("home", "messages"):
        record = render_event(_app_home(tab=tab), family=EVENT_FAMILY_APP_HOME)
        assert record == {
            "family": "app_home",
            "event_type": "app_home_opened",
            "event_ts_iso_utc": "2024-03-09T16:06:40.000500Z",
            "author_user_id": "U1",
            "author_name": "U1",
            "tab": tab,
        }


def test_the_published_view_never_reaches_the_record() -> None:
    # The largest thing any event here could fold in, repeated identically on
    # every entry, in the family that fires on every entry -- and it describes
    # this app's own output rather than anything the person did. Nor is a
    # cheaper summary of it written under some other name: this module does not
    # own the Home tab surface, so a field named here would fix a vocabulary
    # for it from the outside.
    event = _app_home(view=True)
    assert "view" in event
    record = render_event(event, family=EVENT_FAMILY_APP_HOME)
    assert "view" not in record
    assert not any("view" in key for key in record)
    # Byte-identical to the record for the same opening with no view published,
    # which is what "the view is not part of this record" means.
    assert record == render_event(_app_home(view=False), family=EVENT_FAMILY_APP_HOME)


def test_an_app_home_opening_carries_no_message_identifier() -> None:
    record = render_event(_app_home(), family=EVENT_FAMILY_APP_HOME)
    assert "ts" not in record
    assert "ts_iso_utc" not in record
    assert "emoji_name" not in record


def test_a_tab_the_payload_did_not_name_is_absent_rather_than_guessed() -> None:
    bare = {k: v for k, v in _app_home().items() if k != "tab"}
    record = render_event(bare, family=EVENT_FAMILY_APP_HOME)
    assert "tab" not in record
    assert record["event_type"] == "app_home_opened"


def test_is_author_bot_is_written_only_when_the_payload_states_it() -> None:
    assert "is_author_bot" not in render_event(
        _reaction(), family=EVENT_FAMILY_REACTION
    )
    marked = render_event(
        {**_reaction(), "bot_id": "B1"}, family=EVENT_FAMILY_REACTION
    )
    assert marked["is_author_bot"] is True


def test_the_conversation_is_read_from_whichever_spelling_slack_used() -> None:
    assert event_chat_id(_reaction()) == "C1"
    assert event_chat_id(_pin()) == "C1"
    assert event_chat_id(_member()) == "C1"
    # The direct message between that person and this app, which is what the
    # App Home's Messages tab is. Filed there, the record lands in the
    # conversation the person is standing in rather than in a space of its own.
    assert event_chat_id(_app_home()) == "D1"


def test_event_family_names_only_the_types_this_connector_reads() -> None:
    assert event_family(_reaction()) == EVENT_FAMILY_REACTION
    assert event_family(_app_home()) == EVENT_FAMILY_APP_HOME
    assert event_family({"type": "star_added"}) is None
    # There is no closing event, and nothing invents one.
    assert event_family({"type": "app_home_closed"}) is None


# ----------------------------------------------------------------------
# The buffer
# ----------------------------------------------------------------------


def test_nothing_held_folds_to_nothing() -> None:
    assert InboundEventBuffer().fold("C1") is None


def test_a_fold_reports_the_conversation_and_every_record_oldest_first() -> None:
    clock = _Clock()
    buffer = InboundEventBuffer(now=clock)
    buffer.record("C1", EVENT_FAMILY_REACTION, {"family": "reaction", "n": 1})
    clock.advance(1)
    buffer.record("C1", EVENT_FAMILY_MEMBER, {"family": "member", "n": 2})
    clock.advance(1)
    buffer.record("C1", EVENT_FAMILY_REACTION, {"family": "reaction", "n": 3})

    payload = buffer.fold("C1")

    assert payload == {
        "chat_id": "C1",
        "events": [
            {"family": "reaction", "n": 1},
            {"family": "member", "n": 2},
            {"family": "reaction", "n": 3},
        ],
    }
    # Folded is forgotten: the next turn must not carry the last turn's events.
    assert buffer.fold("C1") is None


def test_the_buffer_is_per_conversation() -> None:
    buffer = InboundEventBuffer()
    buffer.record("C1", EVENT_FAMILY_REACTION, {"n": 1})
    buffer.record("C2", EVENT_FAMILY_REACTION, {"n": 2})
    assert buffer.fold("C1") == {"chat_id": "C1", "events": [{"n": 1}]}
    assert buffer.fold("C2") == {"chat_id": "C2", "events": [{"n": 2}]}


def test_a_reaction_flood_cannot_evict_a_membership_change() -> None:
    # Per-family bounds, not one shared cap: a shared queue would let the noisy
    # family starve the quiet one, and the join explaining who the new person is
    # is the record most worth keeping.
    buffer = InboundEventBuffer(
        limits={EVENT_FAMILY_REACTION: 3, EVENT_FAMILY_PIN: 3, EVENT_FAMILY_MEMBER: 3}
    )
    buffer.record("C1", EVENT_FAMILY_MEMBER, {"family": "member", "n": "join"})
    for index in range(50):
        buffer.record("C1", EVENT_FAMILY_REACTION, {"family": "reaction", "n": index})

    payload = buffer.fold("C1")

    assert {"family": "member", "n": "join"} in payload["events"]
    reactions = [e for e in payload["events"] if e["family"] == "reaction"]
    assert len(reactions) == 3


def test_overflow_drops_the_oldest_and_says_how_many() -> None:
    # Oldest, never newest: these are state changes, so the recent records are
    # the ones describing the room as it now stands. And the cut is reported --
    # a fold that quietly holds half a burst is a lie about the room.
    buffer = InboundEventBuffer(limits={EVENT_FAMILY_REACTION: 2})
    for index in range(5):
        buffer.record("C1", EVENT_FAMILY_REACTION, {"n": index})

    payload = buffer.fold("C1")

    assert payload["events"] == [{"n": 3}, {"n": 4}]
    assert payload["dropped"] == {EVENT_FAMILY_REACTION: {DROP_OVERFLOW: 3}}


def test_an_event_older_than_the_ttl_is_dropped_and_counted() -> None:
    clock = _Clock()
    buffer = InboundEventBuffer(ttl_seconds=3600.0, now=clock)
    buffer.record("C1", EVENT_FAMILY_REACTION, {"n": "stale"})
    clock.advance(3601)
    buffer.record("C1", EVENT_FAMILY_REACTION, {"n": "fresh"})

    payload = buffer.fold("C1")

    assert payload["events"] == [{"n": "fresh"}]
    assert payload["dropped"] == {EVENT_FAMILY_REACTION: {DROP_EXPIRED: 1}}


def test_a_quiet_room_expires_at_the_fold_as_well_as_on_arrival() -> None:
    # The count bound never fires here, so the deadline is the only thing
    # keeping a two-day-old reaction out of tomorrow's first turn.
    clock = _Clock()
    buffer = InboundEventBuffer(ttl_seconds=3600.0, now=clock)
    buffer.record("C1", EVENT_FAMILY_PIN, {"n": "stale"})
    clock.advance(172800)

    payload = buffer.fold("C1")

    assert payload == {
        "chat_id": "C1",
        "events": [],
        "dropped": {EVENT_FAMILY_PIN: {DROP_EXPIRED: 1}},
    }


def test_a_drop_count_is_reported_once_and_then_cleared() -> None:
    buffer = InboundEventBuffer(limits={EVENT_FAMILY_REACTION: 1})
    buffer.record("C1", EVENT_FAMILY_REACTION, {"n": 0})
    buffer.record("C1", EVENT_FAMILY_REACTION, {"n": 1})

    assert buffer.fold("C1")["dropped"] == {EVENT_FAMILY_REACTION: {DROP_OVERFLOW: 1}}
    assert buffer.fold("C1") is None


def test_records_are_never_coalesced() -> None:
    # Twenty reactions must stay twenty records. "five people reacted" is a
    # derived representation, and the derivation is the connector deciding what
    # mattered.
    buffer = InboundEventBuffer(limits={EVENT_FAMILY_REACTION: 30})
    for index in range(20):
        buffer.record(
            "C1", EVENT_FAMILY_REACTION, {"emoji_name": "eyes", "author_user_id": "U1"}
        )
    assert len(buffer.fold("C1")["events"]) == 20


def test_forget_drops_one_conversation_or_all_of_them() -> None:
    buffer = InboundEventBuffer()
    buffer.record("C1", EVENT_FAMILY_REACTION, {"n": 1})
    buffer.record("C2", EVENT_FAMILY_REACTION, {"n": 2})
    buffer.forget("C1")
    assert buffer.fold("C1") is None
    assert buffer.fold("C2") is not None

    buffer.record("C1", EVENT_FAMILY_REACTION, {"n": 1})
    buffer.record("C2", EVENT_FAMILY_REACTION, {"n": 2})
    buffer.forget()
    assert buffer.fold("C1") is None
    assert buffer.fold("C2") is None


@pytest.mark.parametrize("family", list(EVENT_FAMILIES))
def test_the_shipped_bound_is_positive_for_every_family(family: str) -> None:
    assert EVENT_BUFFER_LIMITS[family] > 0


def test_the_app_home_bound_is_the_tightest_and_holds_the_recent_entries() -> None:
    """The bound chosen for the highest-rate family, behaving as it is written.

    Five, and the smallest of the four, which is not a contradiction: this is
    the only event nobody did *to* anything, every record of it in one
    conversation names the same person, and the only field that varies is the
    tab. What is kept is the most recent five and what is reported is how many
    entries there were, which says more than a sixth identical record would.
    """
    assert EVENT_BUFFER_LIMITS[EVENT_FAMILY_APP_HOME] == 5
    assert EVENT_BUFFER_LIMITS[EVENT_FAMILY_APP_HOME] == min(
        EVENT_BUFFER_LIMITS.values()
    )

    buffer = InboundEventBuffer()
    for index in range(9):
        buffer.record(
            "D1",
            EVENT_FAMILY_APP_HOME,
            {
                **render_event(
                    _app_home(tab="home" if index % 2 else "messages"),
                    family=EVENT_FAMILY_APP_HOME,
                ),
                "n": index,
            },
        )

    folded = buffer.fold("D1")
    assert folded is not None
    assert [record["n"] for record in folded["events"]] == [4, 5, 6, 7, 8]
    # The oldest go, never the newest: the recent entries are the ones that
    # describe where the person is now.
    assert folded["dropped"] == {EVENT_FAMILY_APP_HOME: {DROP_OVERFLOW: 4}}
    # And the tab survives the fold, which is the field the family exists for.
    assert {record["tab"] for record in folded["events"]} == {"home", "messages"}


def test_a_burst_of_app_home_entries_cannot_evict_another_family() -> None:
    # The per-family cap, checked against the family most able to run away with
    # a shared one: an entry to App Home costs nothing and can be repeated as
    # fast as somebody clicks.
    buffer = InboundEventBuffer()
    buffer.record("D1", EVENT_FAMILY_REACTION, {"kept": True})
    for _ in range(50):
        buffer.record(
            "D1",
            EVENT_FAMILY_APP_HOME,
            render_event(_app_home(), family=EVENT_FAMILY_APP_HOME),
        )

    folded = buffer.fold("D1")
    assert folded is not None
    assert {"kept": True} in folded["events"]
    assert len(folded["events"]) == 1 + EVENT_BUFFER_LIMITS[EVENT_FAMILY_APP_HOME]
