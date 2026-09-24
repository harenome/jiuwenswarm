# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""One host and three limits, for pulling a file's bytes out of Slack.

Two subsystems fetch files from Slack with the bot token attached: the connector
downloads what somebody attached to an inbound message, and the runtime's
history toolkit opens a file the model named by id. They share nothing else, but
they must answer the same four questions the same way -- which host may receive
the credential, how many bytes are worth pulling down, how long any one network
operation may take, and which characters may reach a path.

The values live here rather than in either caller because the toolkit must not
import the connector: ``slack_connect`` pulls in ``slack_bolt``, so importing it
from a harness tool would drag the gateway's dependency tree into the runtime.
``SLACK_FILE_HOST`` is the reason a shared definition matters rather than a
documented pair of copies: it decides which host may be sent a live workspace
credential, and a security boundary with two definitions can drift.

Beside the values sits the transport both callers fetch through, and the loop
that follows a redirect. The allow-list decides *which name* may be sent the
token; :func:`slack_file_transport` decides *which address* that name is allowed
to answer with, and dials the address it validated rather than re-asking a
resolver that may answer differently the second time; and
:func:`stream_slack_file` makes every redirect target face both of those,
because a target out of a ``Location`` header is a URL nobody has checked. All
three have to be answered before a bearer header goes out; see
:func:`resolve_validated_addresses` and :func:`stream_slack_file`.

``_safe_path_component`` stays copied in both callers -- a small pure function
rather than a value the two must agree on. ``_https_host`` is copied in both
callers *and* answered a third time here, by :func:`https_file_host`, because
this module runs the redirect loop and so has to ask the hop the same question
the callers ask of the first URL. The three are meant to answer identically.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

#: The only file host Slack serves its own uploads from. ``url_private`` on a
#: hosted file is ``https://files.slack.com/files-pri/<team>-<file>/<name>``,
#: and ``url_private_download`` is the same with ``/download/`` inserted.
#:
#: Not the whole allow-list: each caller also accepts the workspace's own host
#: (``auth.test``'s ``url``), where ``permalink`` lives. Two exact entries and no
#: wildcard -- a suffix match on ``.slack.com`` would be a rule about a string
#: rather than about a host Slack itself named.
#:
#: For an *external* file (``is_external: true``, ``mode: "external"``) Slack
#: fills ``url_private`` in with the URL whoever registered the file supplied,
#: so it can name any host at all. Attaching the bot token to that as a bearer
#: header hands the live credential to whoever chose the URL, and anybody who
#: can share an external file into a channel the bot is in can choose it.
SLACK_FILE_HOST = "files.slack.com"

#: Matches ``media_attachments``' ceiling for browser uploads. Slack itself
#: allows 1 GB, which is not something to pull into a session directory
#: unprompted.
MAX_FILE_BYTES = 30 * 1024 * 1024

#: Passed to httpx as a single value, which sets connect, read, write and pool
#: alike. It bounds each individual operation and nothing more: the read timeout
#: is measured per chunk, so a transfer that keeps trickling bytes in never trips
#: it however long it runs. A caller that needs the *whole* wait bounded has to
#: impose its own budget, and both do -- the connector across every attachment on
#: one message, the toolkit over the single transfer it was asked for.
FILE_TRANSFER_TIMEOUT_SECONDS = 60.0

#: Path components are built from Slack-supplied names, which are user input and
#: arrive with directory separators, spaces and non-ASCII intact, so everything
#: outside this set is replaced rather than trusted.
UNSAFE_PATH_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")


# ── refusal codes ────────────────────────────────────────────────────────────
#
# One code per check, so a log line says which of several unrelated things
# happened. ``slack_file_host_not_allowed`` means a URL or a redirect named
# somewhere the token is not going, which is a file or a hop to look at.
# ``slack_file_address_refused`` means a host on the allow-list answered with an
# address that is not on the public internet, which is a resolver or a network
# to look at. An ordinary connection failure is neither of these and still
# arrives as the httpx error it always was.

#: A URL -- the first one, or a redirect's target -- named a host that is not on
#: the caller's allow-list, or named one over plain http.
REFUSAL_HOST_NOT_ALLOWED = "slack_file_host_not_allowed"

#: An allow-listed host did not resolve, or resolved to nothing.
REFUSAL_HOST_UNRESOLVED = "slack_file_host_did_not_resolve"

#: An allow-listed host resolved to an address this bot will not dial.
REFUSAL_ADDRESS_REFUSED = "slack_file_address_refused"

#: The redirect chain ran past :data:`MAX_FILE_REDIRECTS`.
REFUSAL_TOO_MANY_REDIRECTS = "slack_file_too_many_redirects"

#: How many redirects one download may follow. Slack's own answer is one hop at
#: most in practice, and a chain longer than this is a loop or a
#: misconfiguration rather than a file. Every hop is checked in full, so the
#: bound is about cost and termination rather than about safety.
MAX_FILE_REDIRECTS = 3

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: How many of a hostname's addresses are worth dialling when the first refuses
#: the connection. *Every* answer is validated before any of them is dialled, so
#: this caps work and not scrutiny: a name that answers with one bad address is
#: refused whether or not the bad one falls inside this many.
MAX_DIALLED_ADDRESSES = 4

#: Checked in this order so that a refusal names the most specific thing that is
#: true of the address. ``127.0.0.1`` is loopback *and* private, ``0.0.0.0`` is
#: unspecified *and* private, ``169.254.169.254`` is link-local *and* private;
#: reporting "a private address" for any of those would send someone looking at
#: RFC 1918 when the answer is the host's own stack, its own link, or a cloud
#: metadata service.
_ADDRESS_REFUSALS: tuple[tuple[str, str], ...] = (
    ("is_unspecified", "the unspecified address"),
    ("is_loopback", "a loopback address"),
    ("is_link_local", "a link-local address"),
    ("is_multicast", "a multicast address"),
    ("is_reserved", "a reserved address"),
    ("is_private", "a private address"),
)


class SlackFileTransferRefused(RuntimeError):
    """A file transfer this module stopped, and which check stopped it.

    Deliberately not an :class:`httpx.HTTPError`. Both callers already map every
    httpx error onto "the download failed", which is the right answer for a
    connection that was refused or a name that timed out and the wrong one for a
    transfer that was never going to be attempted at all. Keeping this class
    outside that hierarchy means a caller that has not added a clause for it
    raises loudly rather than filing a refusal as a network blip.

    ``code`` is a stable string a test and a log filter can match on; ``detail``
    is the sentence a person reads.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def https_file_host(url: Any) -> str:
    """The lowercase host of *url*, or ``""`` unless it is a plain ``https`` URL.

    The third instance of a rule the Slack connector and the history toolkit
    each hold their own copy of, and it is here because this module follows the
    redirects: a hop has to be checked by the same rule the caller checked the
    first URL by, and this module can import neither caller.

    Everything that is not an ``https`` URL with a host answers the empty
    string, which no allow-list contains, so a malformed value, a ``file://``
    path and an unparseable one are all refused by the same comparison rather
    than by three special cases. A trailing dot is stripped: to a resolver,
    ``files.slack.com.`` and ``files.slack.com`` name the same host, and they
    must not differ to an allow-list either.
    """
    try:
        parsed = urlparse(str(url or ""))
    except ValueError:
        return ""
    if parsed.scheme != "https":
        return ""
    return (parsed.hostname or "").strip().lower().rstrip(".")


def normalised_hosts(hosts: Iterable[str]) -> frozenset[str]:
    """An allow-list in the form :func:`https_file_host` answers in."""
    return frozenset(
        host.strip().lower().rstrip(".") for host in hosts if host and host.strip()
    )


def address_refusal_reason(address: Any) -> str | None:
    """Why *address* may not be dialled, or ``None`` when it may.

    An IPv4-mapped IPv6 address (``::ffff:10.0.0.1``) is classified by the IPv4
    address inside it. Without that step the same private network is refused
    when a resolver answers ``A`` and admitted when it answers ``AAAA``, which
    is a difference an attacker picks rather than one that means anything.

    The classes refused are the ones that are not on the public internet:
    unspecified, loopback, link-local, multicast, reserved and private. Nothing
    here encodes anything about Slack's own addresses. Slack has not published
    them, a rule built on a guess about them would break the day they changed,
    and the question being asked is only whether this address is somewhere a
    file host could legitimately be.
    """
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    for attribute, phrase in _ADDRESS_REFUSALS:
        if getattr(address, attribute, False):
            return phrase
    return None


def _getaddrinfo(hostname: str, port: int) -> list[Any]:
    """The one resolver call this module makes, named so a test can replace it.

    Nothing else here touches :mod:`socket`, so a test that rebinds this
    function has faked every lookup the transport will do, and no test has to
    reach a real resolver in order to exercise a refusal.
    """
    return socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)


def resolve_validated_addresses(host: str, port: int) -> tuple[str, ...]:
    """Resolve *host* once, refuse the answer unless every address is dialable.

    This is the half of the fix a hostname allow-list cannot do. Checking that a
    URL names ``files.slack.com`` and then handing the *name* to a client are
    two operations against two lookups: whoever controls the answers can return
    a public address to the check and a private one to the connection, and the
    bearer header goes wherever the second answer pointed. Resolving here and
    dialling the result closes that, by making the answer that was checked and
    the answer that is dialled the same bytes.

    Every address in the answer is validated, not merely the first. A name that
    answers with one public and one private address is refused outright, because
    nothing here chooses which of them a connection would have used -- the
    ordering is the resolver's, and the resolver is the thing under suspicion.

    The returned tuple is capped at :data:`MAX_DIALLED_ADDRESSES` so a name with
    many records cannot turn one refused connection into a long series of
    connection timeouts. The cap is applied after validation, never before.

    Raises :class:`SlackFileTransferRefused`. The hostname appears in the detail
    and the URL never does: by the time this runs the hostname has already
    passed the allow-list, so it is one of the two hosts Slack named rather than
    attacker-chosen text, while the path and the query are neither checked nor
    safe to repeat.
    """
    hostname = (host or "").strip().lower().rstrip(".")
    if not hostname:
        raise SlackFileTransferRefused(
            REFUSAL_HOST_UNRESOLVED,
            "the download named no hostname to resolve",
        )
    try:
        answers = _getaddrinfo(hostname, port)
    except OSError as exc:
        raise SlackFileTransferRefused(
            REFUSAL_HOST_UNRESOLVED,
            f"{hostname} did not resolve ({type(exc).__name__}); that is a"
            f" resolver or a network to look at rather than anything about the"
            f" file",
        ) from None

    addresses: list[str] = []
    for answer in answers:
        # A getaddrinfo answer is (family, type, proto, canonname, sockaddr),
        # and sockaddr[0] is the address. An IPv6 scope id (``fe80::1%eth0``) is
        # cut off before parsing; it names an interface, not an address.
        raw = str(answer[4][0]).split("%")[0]
        try:
            parsed = ipaddress.ip_address(raw)
        except ValueError:
            raise SlackFileTransferRefused(
                REFUSAL_ADDRESS_REFUSED,
                f"{hostname} resolved to {raw!r}, which is not an IP address at"
                f" all; an answer that cannot be classified is refused rather"
                f" than dialled",
            ) from None
        reason = address_refusal_reason(parsed)
        if reason is not None:
            raise SlackFileTransferRefused(
                REFUSAL_ADDRESS_REFUSED,
                f"{hostname} resolved to {raw}, which is {reason}; a Slack file"
                f" host answering with an address that is not on the public"
                f" internet is refused whether or not it is the address a"
                f" connection would have picked",
            )
        if raw not in addresses:
            addresses.append(raw)

    if not addresses:
        raise SlackFileTransferRefused(
            REFUSAL_HOST_UNRESOLVED,
            f"{hostname} resolved to no addresses at all",
        )
    return tuple(addresses[:MAX_DIALLED_ADDRESSES])


class _PinnedBackend:
    """An httpcore network backend that dials a validated address, by address.

    Wraps whichever backend the connection pool was built with rather than
    importing one, so this holds no opinion about which httpcore backend is in
    use and gains whatever the installed one does.

    ``connect_tcp`` is the only method whose behaviour changes, and changing
    only it is what keeps the rest of the request honest. httpcore asks the
    backend for a socket and *separately* derives the TLS ``server_hostname``
    and the ``Host`` header from the request's own origin, so substituting the
    address here moves the connection without touching either. Certificate
    verification therefore still checks the certificate against
    ``files.slack.com`` and not against the address dialled, and nothing in this
    module weakens, relaxes or reaches into TLS verification.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        # ``sleep``, and anything a future httpcore adds. Only the two methods
        # below are decisions; the rest is the wrapped backend's business.
        return getattr(self._inner, name)

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        addresses = await asyncio.to_thread(resolve_validated_addresses, host, port)
        last: BaseException | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:  # noqa: BLE001
                # Every address reaching this loop passed validation, so a
                # failure here is an ordinary network failure and the next
                # validated address is worth trying. The last one is re-raised
                # unchanged, so the caller still sees the real httpx error
                # rather than a refusal wearing its clothes.
                last = exc
        if last is None:
            raise SlackFileTransferRefused(
                REFUSAL_HOST_UNRESOLVED,
                f"{host} produced no address to dial",
            )
        raise last

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> Any:
        raise SlackFileTransferRefused(
            REFUSAL_ADDRESS_REFUSED,
            "a Slack file download tried to connect to a unix socket; there is"
            " no address to validate and no file host at the other end of one",
        )


class _SlackFileTransport(httpx.AsyncHTTPTransport):
    """The transport a token-bearing Slack download goes out through.

    Two things hold here that do not hold of a default client. Every request the
    transport is asked to make -- the first one and any redirect that reaches it
    -- must name an ``https`` host on the allow-list; and every connection it
    opens goes to an address validated in this process, rather than to a name a
    resolver gets to answer a second time.

    The allow-list is checked here as well as in :func:`stream_slack_file`,
    which refuses a bad hop before issuing it. The loop is a procedure a future
    caller can forget to run; this is a property of the object the token is
    handed to, and it holds however the request got here.

    Built with httpx's own defaults for TLS, which means verification against
    the certifi bundle. Nothing is passed here that would turn it off.
    """

    def __init__(self, allowed_hosts: Iterable[str]) -> None:
        super().__init__()
        self._allowed_hosts = normalised_hosts(allowed_hosts)
        pool = getattr(self, "_pool", None)
        backend = getattr(pool, "_network_backend", None)
        if pool is None or backend is None:
            # Fails closed. A future httpx that reshapes the pool would
            # otherwise leave this transport looking pinned while dialling
            # names, which is the one outcome worse than not having it.
            raise SlackFileTransferRefused(
                REFUSAL_ADDRESS_REFUSED,
                "this httpx build does not expose the connection pool's network"
                " backend, so the download cannot be pinned to an address that"
                " was validated; refusing rather than fetching unpinned",
            )
        pool._network_backend = _PinnedBackend(backend)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = (request.url.host or "").strip().lower().rstrip(".")
        if request.url.scheme != "https" or host not in self._allowed_hosts:
            # The host is left out of the message on purpose, as it is at every
            # other refusal on this path: it is text out of a file record or out
            # of a ``Location`` header, and naming it in a log adds nothing an
            # operator can act on that the file's name and permalink do not.
            raise SlackFileTransferRefused(
                REFUSAL_HOST_NOT_ALLOWED,
                "a request on the Slack file path named a host this bot does"
                " not send its token to, or named one over plain http; the"
                " transport refused it before opening a connection",
            )
        return await super().handle_async_request(request)


def slack_file_transport(allowed_hosts: Iterable[str]) -> httpx.AsyncBaseTransport:
    """The transport both Slack file downloads are made through.

    Pass it to :class:`httpx.AsyncClient` as ``transport=``. Doing so also stops
    httpx reading ``HTTPS_PROXY`` and friends for that client, which is
    deliberate: a proxy resolves the name itself and connects on the client's
    behalf, so a pinned address would be advice rather than a guarantee. A
    download that silently stopped being pinned because an environment variable
    was set would be the worse of the two outcomes.
    """
    return _SlackFileTransport(allowed_hosts)


@asynccontextmanager
async def stream_slack_file(
    client: Any,
    url: str,
    *,
    headers: dict[str, str],
    allowed_hosts: Iterable[str],
) -> AsyncIterator[Any]:
    """Stream a Slack file, putting every redirect through the first URL's checks.

    Redirects are followed here rather than by httpx, and the reason is that a
    redirect target is a URL nobody checked. ``follow_redirects=True`` takes a
    host out of a ``Location`` header and dials it; the allow-list that decided
    the first URL could be sent a credential never sees it. httpx does drop
    ``Authorization`` when a redirect leaves the origin, so on that library at
    that version the token itself does not travel -- but the request still does.
    An unchecked hop is a fetch this bot makes to somewhere somebody else chose,
    and the answer is written into a session directory and handed to a model. A
    boundary that holds only because a dependency happens to behave is not a
    boundary this path should be resting on.

    So: ``follow_redirects=False`` on the client, and one hop at a time here,
    each target facing exactly what the first URL faced -- the same ``https``
    rule, the same allow-list, and, through :func:`slack_file_transport`, the
    same resolve-and-validate before a connection opens. At most
    :data:`MAX_FILE_REDIRECTS` of them, so a loop terminates.

    Each redirect response is closed before the next request goes out, so a
    redirect carrying a large body costs nothing. Letting httpx follow would
    read that body into memory first, outside the byte ceiling both callers
    enforce per chunk on the body they do want.

    Yields the first response that is not a redirect -- still open, still
    unread -- for the caller to check the status on and stream the body from. A
    redirect carrying no usable ``Location`` is yielded as it is, so that the
    caller's own status handling reports it rather than this loop inventing a
    second answer for the same failure.
    """
    hosts = normalised_hosts(allowed_hosts)
    target = str(url)
    for hop in range(MAX_FILE_REDIRECTS + 1):
        if https_file_host(target) not in hosts:
            where = "the download URL" if hop == 0 else f"redirect hop {hop}"
            raise SlackFileTransferRefused(
                REFUSAL_HOST_NOT_ALLOWED,
                f"{where} named a host this bot will not send its token to, or"
                f" named one over plain http; nothing was fetched from it",
            )
        context = client.stream("GET", target, headers=headers)
        response = await context.__aenter__()
        try:
            location = (
                str(response.headers.get("location") or "").strip()
                if response.status_code in _REDIRECT_STATUSES
                else ""
            )
        except BaseException:
            await context.__aexit__(None, None, None)
            raise
        if not location:
            try:
                yield response
            finally:
                await context.__aexit__(None, None, None)
            return
        await context.__aexit__(None, None, None)
        # A relative ``Location`` is resolved against the hop that sent it,
        # which is what any client would do, and the result is then checked like
        # every other target rather than trusted for having come from one.
        target = urljoin(target, location)

    raise SlackFileTransferRefused(
        REFUSAL_TOO_MANY_REDIRECTS,
        f"the download redirected more than {MAX_FILE_REDIRECTS} times and was"
        f" abandoned; a chain that long is a loop or a misconfiguration rather"
        f" than a file",
    )


__all__ = [
    "FILE_TRANSFER_TIMEOUT_SECONDS",
    "MAX_DIALLED_ADDRESSES",
    "MAX_FILE_BYTES",
    "MAX_FILE_REDIRECTS",
    "REFUSAL_ADDRESS_REFUSED",
    "REFUSAL_HOST_NOT_ALLOWED",
    "REFUSAL_HOST_UNRESOLVED",
    "REFUSAL_TOO_MANY_REDIRECTS",
    "SLACK_FILE_HOST",
    "UNSAFE_PATH_CHARS_RE",
    "SlackFileTransferRefused",
    "address_refusal_reason",
    "https_file_host",
    "normalised_hosts",
    "resolve_validated_addresses",
    "slack_file_transport",
    "stream_slack_file",
]
