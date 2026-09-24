"""The Slack SDK logger trees must land where every other log in the process does.

``setup_logger`` attaches handlers to the ``jiuwenswarm`` logger and to nothing
else, so ``slack_bolt`` and ``slack_sdk`` were never quiet -- they were unwired,
with no handler anywhere on either tree. Everything they had to say about an
inbound envelope, including the exceptions bolt catches on a listener behalf
rather than letting them propagate, was discarded before it reached a file.
"""

from __future__ import annotations

import logging

import pytest

from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect



def test_sdk_loggers_are_wired_to_the_handlers_that_already_redact() -> None:
    """The SDK trees must borrow the shared handlers, not grow their own.

    Every handler on the ``jiuwenswarm`` logger holds ``SensitiveDataFilter``,
    and these two trees are the ones that really do log tokens and whole message
    bodies. Re-deriving that filtering here is how a bot token reaches a file.
    """
    shared = logging.getLogger("jiuwenswarm")
    marker = logging.NullHandler()
    shared.addHandler(marker)
    try:
        base = slack_connect.configure_sdk_logging("DEBUG")
        # What AsyncApp(logger=...) is given. Without it bolt pins every logger
        # it builds to the root logger's level and to no handler, so setting the
        # tree's level below would not reach one of them. It holds no handlers
        # itself, because bolt copies a base logger's handlers down and leaves
        # the copies propagating.
        assert base is logging.getLogger(slack_connect.SDK_BASE_LOGGER_NAME)
        assert base.handlers == []
        assert base.level == logging.DEBUG
        for name in slack_connect.SDK_LOGGER_NAMES:
            sdk = logging.getLogger(name)
            assert marker in sdk.handlers
            assert sdk.level == logging.DEBUG
            # Handlers are attached here, so propagating would only add an
            # unfiltered second copy by way of the root logger.
            assert sdk.propagate is False
    finally:
        shared.removeHandler(marker)
        for name in slack_connect.SDK_LOGGER_NAMES:
            sdk = logging.getLogger(name)
            sdk.handlers.clear()
            sdk.setLevel(logging.NOTSET)
            sdk.propagate = True


def test_sdk_logging_is_not_attached_twice_across_restarts() -> None:
    shared = logging.getLogger("jiuwenswarm")
    marker = logging.NullHandler()
    shared.addHandler(marker)
    try:
        slack_connect.configure_sdk_logging("INFO")
        slack_connect.configure_sdk_logging("INFO")
        bolt = logging.getLogger("slack_bolt")
        assert bolt.handlers.count(marker) == 1
    finally:
        shared.removeHandler(marker)
        for name in slack_connect.SDK_LOGGER_NAMES:
            sdk = logging.getLogger(name)
            sdk.handlers.clear()
            sdk.setLevel(logging.NOTSET)
            sdk.propagate = True


class _Counting(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_a_bolt_logger_reaches_the_shared_handlers_exactly_once() -> None:
    """Why the base logger has to be passed to AsyncApp, and why a bare one.

    Bolt builds one logger per internal class. Given no base logger it pins each
    one's level to ``logging.root``'s and attaches no handler, and an explicit
    level on a child beats its parent's -- so configuring the ``slack_bolt``
    tree alone leaves every one of them exactly as silent as before, which is
    the first half of what this pins.

    The second half is the trap on the other side. Bolt also copies a base
    logger's *handlers* onto each logger it builds and leaves those loggers
    propagating, so a base logger holding handlers writes every bolt line
    twice. Once, not none and not two.
    """
    bolt_logger = pytest.importorskip("slack_bolt.logger")

    class _Pretend:
        pass

    shared = logging.getLogger("jiuwenswarm")
    counter = _Counting()
    shared.addHandler(counter)
    try:
        base = slack_connect.configure_sdk_logging("DEBUG")
        built = bolt_logger.get_bolt_logger(_Pretend, base_logger=base)
        assert built.level == logging.DEBUG, "the level must reach bolt's own loggers"
        built.warning("bolt says something about an envelope")
        from_bolt = [r for r in counter.records if r.name.startswith("slack_bolt")]
        assert len(from_bolt) == 1
        assert from_bolt[0].getMessage().endswith("about an envelope")
    finally:
        shared.removeHandler(counter)
        for name in (
            *slack_connect.SDK_LOGGER_NAMES,
            slack_connect.SDK_BASE_LOGGER_NAME,
            "slack_bolt._Pretend",
        ):
            sdk = logging.getLogger(name)
            sdk.handlers.clear()
            sdk.setLevel(logging.NOTSET)
            sdk.propagate = True


@pytest.mark.parametrize(
    "raw, expected",
    [
        ({}, slack_connect.DEFAULT_SDK_LOG_LEVEL),
        ({"sdk_log_level": None}, slack_connect.DEFAULT_SDK_LOG_LEVEL),
        ({"sdk_log_level": "debug"}, "DEBUG"),
        ({"sdk_log_level": " Info "}, "INFO"),
        # A misspelling is not a request for silence.
        ({"sdk_log_level": "verbose"}, slack_connect.DEFAULT_SDK_LOG_LEVEL),
    ],
)
def test_resolve_sdk_log_level(raw, expected) -> None:
    assert slack_connect.resolve_sdk_log_level(raw) == expected
