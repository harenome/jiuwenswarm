# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""The address a Slack file download is allowed to be dialled at.

Every lookup in this file is faked. The point of the code under test is that a
name is resolved once and the answer is dialled, so a test that reached a real
resolver would be asserting against whatever the network said today; and a
rebinding test in particular needs a resolver that answers differently on
demand, which is exactly what a real one will not do.

The socket layer is faked too. ``_FakeBackend`` stands in for httpcore's network
backend and serves a canned HTTP/1.1 response over a stream that records what it
was asked to do, which is how the dialled address, the TLS ``server_hostname``
and the ``Host`` header can all be asserted on without a listening port.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
from typing import Any

import httpx
import pytest

from jiuwenswarm.common import slack_file_transfer
from jiuwenswarm.common.slack_file_transfer import (
    MAX_DIALLED_ADDRESSES,
    MAX_FILE_REDIRECTS,
    REFUSAL_ADDRESS_REFUSED,
    REFUSAL_HOST_NOT_ALLOWED,
    REFUSAL_HOST_UNRESOLVED,
    REFUSAL_TOO_MANY_REDIRECTS,
    SlackFileTransferRefused,
    address_refusal_reason,
    resolve_validated_addresses,
    slack_file_transport,
    stream_slack_file,
)

_HOST = "files.slack.com"
_WORKSPACE_HOST = "acme.slack.com"
_HOSTS = frozenset({_HOST, _WORKSPACE_HOST})
_URL = f"https://{_HOST}/files-pri/T1-F1/notes.txt"
_PUBLIC = "93.184.216.34"
_PUBLIC_V6 = "2606:4700:4700::1111"
_TOKEN = {"Authorization": "Bearer xoxb-secret"}


def _answers(*addresses: str) -> list[tuple[Any, ...]]:
    """getaddrinfo's answer shape, with only the fields the code reads filled."""
    out = []
    for address in addresses:
        family = (
            socket.AF_INET6 if ":" in address else socket.AF_INET
        )
        sockaddr: tuple[Any, ...] = (
            (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
        )
        out.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
    return out


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """A resolver whose answers a test sets, keyed by hostname.

    Returns the mapping, so a test can rebind a name between the validating
    lookup and any later one and prove that no later one happens.
    """
    table: dict[str, list[str]] = {_HOST: [_PUBLIC]}
    calls: list[str] = []
    table["_calls"] = calls  # type: ignore[assignment]

    def fake(hostname: str, port: int) -> list[tuple[Any, ...]]:
        calls.append(hostname)
        if hostname not in table:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return _answers(*table[hostname])

    monkeypatch.setattr(slack_file_transfer, "_getaddrinfo", fake)
    return table


# ── which addresses are refused ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("address", "phrase"),
    [
        ("10.0.0.7", "a private address"),
        ("192.168.1.1", "a private address"),
        ("172.16.0.1", "a private address"),
        ("127.0.0.1", "a loopback address"),
        ("::1", "a loopback address"),
        ("169.254.169.254", "a link-local address"),
        ("fe80::1", "a link-local address"),
        ("224.0.0.1", "a multicast address"),
        ("ff02::1", "a multicast address"),
        ("240.0.0.1", "a reserved address"),
        ("0.0.0.0", "the unspecified address"),
        ("::", "the unspecified address"),
        ("fd00::1", "a private address"),
        # The IPv4-mapped forms of the same networks. Without the mapping step
        # a resolver could answer AAAA and reach what answering A cannot.
        ("::ffff:10.0.0.7", "a private address"),
        ("::ffff:127.0.0.1", "a loopback address"),
        ("::ffff:169.254.169.254", "a link-local address"),
    ],
)
def test_an_address_off_the_public_internet_is_named_and_refused(
    address: str, phrase: str
) -> None:
    assert address_refusal_reason(ipaddress.ip_address(address)) == phrase


@pytest.mark.parametrize("address", [_PUBLIC, _PUBLIC_V6, "1.1.1.1", "2001:4860::1"])
def test_a_public_address_is_not_refused(address: str) -> None:
    assert address_refusal_reason(ipaddress.ip_address(address)) is None


def test_a_name_that_resolves_to_a_private_address_is_refused(
    resolver: dict[str, list[str]]
) -> None:
    resolver[_HOST] = ["10.1.2.3"]
    with pytest.raises(SlackFileTransferRefused) as caught:
        resolve_validated_addresses(_HOST, 443)
    assert caught.value.code == REFUSAL_ADDRESS_REFUSED
    assert "10.1.2.3" in caught.value.detail
    assert "a private address" in caught.value.detail


def test_one_bad_answer_among_good_ones_refuses_the_whole_name(
    resolver: dict[str, list[str]]
) -> None:
    """Nothing here picks which answer a connection would have used.

    The ordering is the resolver's, and the resolver is the thing under
    suspicion, so a name that offers a private address at all is refused --
    including when the private one is not first.
    """
    resolver[_HOST] = [_PUBLIC, "192.168.0.5"]
    with pytest.raises(SlackFileTransferRefused) as caught:
        resolve_validated_addresses(_HOST, 443)
    assert caught.value.code == REFUSAL_ADDRESS_REFUSED
    assert "192.168.0.5" in caught.value.detail


def test_an_ipv4_mapped_private_address_is_refused(
    resolver: dict[str, list[str]]
) -> None:
    resolver[_HOST] = ["::ffff:10.0.0.7"]
    with pytest.raises(SlackFileTransferRefused) as caught:
        resolve_validated_addresses(_HOST, 443)
    assert caught.value.code == REFUSAL_ADDRESS_REFUSED
    assert "a private address" in caught.value.detail


def test_a_name_that_does_not_resolve_says_so_rather_than_failing_to_connect(
    resolver: dict[str, list[str]]
) -> None:
    with pytest.raises(SlackFileTransferRefused) as caught:
        resolve_validated_addresses("nowhere.slack.com", 443)
    assert caught.value.code == REFUSAL_HOST_UNRESOLVED
    assert "gaierror" in caught.value.detail


def test_public_answers_come_back_in_order_and_deduplicated(
    resolver: dict[str, list[str]]
) -> None:
    resolver[_HOST] = [_PUBLIC, _PUBLIC, _PUBLIC_V6]
    assert resolve_validated_addresses(_HOST, 443) == (_PUBLIC, _PUBLIC_V6)


def test_the_dial_list_is_capped_but_every_answer_is_still_validated(
    resolver: dict[str, list[str]]
) -> None:
    """The cap is about how many connections are worth attempting.

    It is applied after validation, so a private address past the cap still
    refuses the name rather than being skipped along with the rest.
    """
    good = [f"93.184.216.{n}" for n in range(1, MAX_DIALLED_ADDRESSES + 1)]
    resolver[_HOST] = good
    assert resolve_validated_addresses(_HOST, 443) == tuple(good)

    resolver[_HOST] = [*good, "10.9.9.9"]
    with pytest.raises(SlackFileTransferRefused) as caught:
        resolve_validated_addresses(_HOST, 443)
    assert "10.9.9.9" in caught.value.detail


def test_an_answer_that_is_not_an_address_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        slack_file_transfer,
        "_getaddrinfo",
        lambda hostname, port: [(socket.AF_INET, 1, 6, "", ("not-an-ip", 443))],
    )
    with pytest.raises(SlackFileTransferRefused) as caught:
        resolve_validated_addresses(_HOST, 443)
    assert caught.value.code == REFUSAL_ADDRESS_REFUSED


# ── what the transport actually dials ────────────────────────────────────────


class _FakeStream:
    """Enough of httpcore's async network stream to answer one GET.

    Records the TLS handshake it was asked for and the request bytes written to
    it, which is where the ``Host`` header and the ``server_hostname`` come
    from in the assertions below.
    """

    def __init__(self, record: dict[str, Any]) -> None:
        self._record = record
        self._pending = b""

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> _FakeStream:
        self._record["server_hostname"] = server_hostname
        self._record["ssl_context"] = ssl_context
        return self

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._record.setdefault("written", bytearray()).extend(buffer)
        body = self._record["body"]
        self._pending = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n"
            b"\r\n" + body
        )

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        chunk, self._pending = self._pending[:max_bytes], self._pending[max_bytes:]
        return chunk

    async def aclose(self) -> None:
        self._record["closed"] = True

    def get_extra_info(self, info: str) -> Any:
        # ``None`` for ``ssl_object`` keeps httpcore on HTTP/1.1 rather than
        # asking a fake handshake what ALPN negotiated.
        return None


class _FakeBackend:
    """httpcore's network backend, recording the address it was told to dial."""

    def __init__(self, record: dict[str, Any]) -> None:
        self._record = record

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> _FakeStream:
        self._record.setdefault("dialled", []).append((host, port))
        return _FakeStream(self._record)

    async def sleep(self, seconds: float) -> None:  # pragma: no cover - unused
        return None


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A transport whose socket layer is the fake above, and its record."""
    record: dict[str, Any] = {"body": b"file body\n"}

    transport = slack_file_transport(_HOSTS)
    inner = transport._pool._network_backend
    # The pinning wrapper stays; only the real sockets underneath it go away, so
    # the resolve-and-dial decision under test is the production one.
    inner._inner = _FakeBackend(record)
    record["transport"] = transport
    return record


async def test_the_connection_goes_to_the_address_that_was_validated(
    resolver: dict[str, list[str]], wire: dict[str, Any]
) -> None:
    """The whole fix in one assertion.

    The client is given a hostname. What reaches the socket layer is the address
    the validating lookup returned, so there is no second lookup for anybody to
    answer differently.
    """
    async with httpx.AsyncClient(transport=wire["transport"]) as client:
        response = await client.get(_URL)

    assert response.status_code == 200
    assert response.content == b"file body\n"
    assert wire["dialled"] == [(_PUBLIC, 443)]
    # One lookup, made by the transport. Nothing re-asked the name.
    assert resolver["_calls"] == [_HOST]


async def test_the_host_header_and_the_sni_stay_on_the_hostname(
    resolver: dict[str, list[str]], wire: dict[str, Any]
) -> None:
    """Dialling by address must not turn into requesting by address.

    If the URL were rewritten to the address instead, the ``Host`` header would
    name the address and TLS would be asked to verify a certificate for it --
    which is why this pins the dial and nothing else.
    """
    async with httpx.AsyncClient(transport=wire["transport"]) as client:
        await client.get(_URL)

    assert wire["server_hostname"] == _HOST
    written = bytes(wire["written"]).decode("ascii", "replace")
    assert f"Host: {_HOST}\r\n" in written
    assert _PUBLIC not in written


async def test_certificate_verification_is_still_on(
    resolver: dict[str, list[str]], wire: dict[str, Any]
) -> None:
    """Pinning that needed verification turned off would be the wrong pinning."""
    async with httpx.AsyncClient(transport=wire["transport"]) as client:
        await client.get(_URL)

    context = wire["ssl_context"]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


async def test_a_private_answer_refuses_before_any_socket_is_opened(
    resolver: dict[str, list[str]], wire: dict[str, Any]
) -> None:
    resolver[_HOST] = ["127.0.0.1"]
    async with httpx.AsyncClient(transport=wire["transport"]) as client:
        with pytest.raises(SlackFileTransferRefused) as caught:
            await client.get(_URL)

    assert caught.value.code == REFUSAL_ADDRESS_REFUSED
    assert "dialled" not in wire


async def test_a_refusal_is_not_an_httpx_error(
    resolver: dict[str, list[str]], wire: dict[str, Any]
) -> None:
    """Both callers map every httpx error onto "the download failed".

    A refusal that arrived as one would be filed as a network blip, so the class
    deliberately sits outside that hierarchy and this pins it.
    """
    resolver[_HOST] = ["10.0.0.1"]
    async with httpx.AsyncClient(transport=wire["transport"]) as client:
        with pytest.raises(SlackFileTransferRefused) as caught:
            await client.get(_URL)

    assert not isinstance(caught.value, httpx.HTTPError)


async def test_a_second_validated_address_is_tried_when_the_first_refuses(
    resolver: dict[str, list[str]], wire: dict[str, Any]
) -> None:
    """A connection failure is not a refusal, and the next answer is still good."""
    resolver[_HOST] = ["93.184.216.34", "93.184.216.35"]
    attempts: list[str] = []
    backend = wire["transport"]._pool._network_backend._inner

    original = backend.connect_tcp

    async def flaky(host: str, port: int, **kwargs: Any) -> Any:
        attempts.append(host)
        if host == "93.184.216.34":
            raise httpx.ConnectError("refused")
        return await original(host, port, **kwargs)

    backend.connect_tcp = flaky
    async with httpx.AsyncClient(transport=wire["transport"]) as client:
        response = await client.get(_URL)

    assert response.status_code == 200
    assert attempts == ["93.184.216.34", "93.184.216.35"]


async def test_a_unix_socket_connection_is_refused(wire: dict[str, Any]) -> None:
    backend = wire["transport"]._pool._network_backend
    with pytest.raises(SlackFileTransferRefused) as caught:
        await backend.connect_unix_socket("/tmp/anything.sock")
    assert caught.value.code == REFUSAL_ADDRESS_REFUSED


async def test_the_transport_refuses_a_host_off_the_allow_list_on_its_own(
    resolver: dict[str, list[str]], wire: dict[str, Any]
) -> None:
    """The loop below is a procedure; this is a property of the object.

    A future caller that builds a request some other way, or an httpx that
    followed a redirect itself, still cannot get a request to a host outside the
    allow-list out of this transport.
    """
    resolver["elsewhere.example"] = [_PUBLIC]
    async with httpx.AsyncClient(transport=wire["transport"]) as client:
        with pytest.raises(SlackFileTransferRefused) as caught:
            await client.get("https://elsewhere.example/x", headers=_TOKEN)

    assert caught.value.code == REFUSAL_HOST_NOT_ALLOWED
    assert "dialled" not in wire
    # The refusal does not repeat the host it refused: on this path that string
    # came out of a file record or a Location header.
    assert "elsewhere.example" not in caught.value.detail


async def test_the_transport_refuses_plain_http_even_on_an_allowed_host(
    resolver: dict[str, list[str]], wire: dict[str, Any]
) -> None:
    async with httpx.AsyncClient(transport=wire["transport"]) as client:
        with pytest.raises(SlackFileTransferRefused) as caught:
            await client.get(f"http://{_HOST}/files-pri/T1-F1/notes.txt")
    assert caught.value.code == REFUSAL_HOST_NOT_ALLOWED


# ── which redirects are followed ─────────────────────────────────────────────


class _Response:
    def __init__(self, status_code: int, headers: dict[str, str]) -> None:
        self.status_code = status_code
        self.headers = headers
        self.closed = False


class _StreamContext:
    def __init__(self, response: _Response) -> None:
        self._response = response

    async def __aenter__(self) -> _Response:
        return self._response

    async def __aexit__(self, *_exc: object) -> bool:
        self._response.closed = True
        return False


class _Client:
    """A client that answers from a table, recording every request made.

    ``stream`` has the signature the two production callers use, so the loop
    under test drives this exactly as it drives ``httpx.AsyncClient``.
    """

    def __init__(self, table: dict[str, _Response]) -> None:
        self.table = table
        self.requests: list[tuple[str, dict[str, str]]] = []

    def stream(
        self, method: str, url: str, headers: dict[str, str] | None = None
    ) -> _StreamContext:
        assert method == "GET"
        self.requests.append((url, dict(headers or {})))
        return _StreamContext(self.table.get(url) or _Response(200, {}))


def _redirect(location: str) -> _Response:
    return _Response(302, {"location": location})


async def test_an_ordinary_download_is_handed_straight_back(
) -> None:
    client = _Client({})
    async with stream_slack_file(
        client, _URL, headers=_TOKEN, allowed_hosts=_HOSTS
    ) as response:
        assert response.status_code == 200
    assert client.requests == [(_URL, _TOKEN)]
    assert response.closed is True


async def test_a_redirect_to_a_host_off_the_allow_list_is_never_requested() -> None:
    """The one that matters: the token is not sent, because nothing is sent.

    httpx would have dropped the ``Authorization`` header on the way to another
    origin, but it would still have made the request and handed the answer back
    to be written to disk. Here the hop is refused before a request exists.
    """
    client = _Client({_URL: _redirect("https://elsewhere.example/steal")})
    with pytest.raises(SlackFileTransferRefused) as caught:
        async with stream_slack_file(
            client, _URL, headers=_TOKEN, allowed_hosts=_HOSTS
        ):
            pass  # pragma: no cover - the refusal happens before the body

    assert caught.value.code == REFUSAL_HOST_NOT_ALLOWED
    assert "redirect hop 1" in caught.value.detail
    assert [url for url, _ in client.requests] == [_URL]
    assert client.table[_URL].closed is True


async def test_a_redirect_downgrading_to_http_is_refused() -> None:
    """Even back to an allow-listed host. ``https`` is part of the rule."""
    client = _Client({_URL: _redirect(f"http://{_HOST}/files-pri/T1-F1/notes.txt")})
    with pytest.raises(SlackFileTransferRefused) as caught:
        async with stream_slack_file(
            client, _URL, headers=_TOKEN, allowed_hosts=_HOSTS
        ):
            pass  # pragma: no cover
    assert caught.value.code == REFUSAL_HOST_NOT_ALLOWED


async def test_a_redirect_to_the_workspace_host_is_followed_with_the_token() -> None:
    """The case that has to keep working: Enterprise Grid's second hop."""
    target = f"https://{_WORKSPACE_HOST}/files-pri/T1-F1/notes.txt"
    client = _Client({_URL: _redirect(target)})
    async with stream_slack_file(
        client, _URL, headers=_TOKEN, allowed_hosts=_HOSTS
    ) as response:
        assert response.status_code == 200
    assert [url for url, _ in client.requests] == [_URL, target]
    assert client.requests[1][1] == _TOKEN


async def test_a_relative_location_is_resolved_against_the_hop_that_sent_it(
) -> None:
    client = _Client({_URL: _redirect("/files-pri/T1-F1/download/notes.txt")})
    async with stream_slack_file(client, _URL, headers=_TOKEN, allowed_hosts=_HOSTS):
        pass
    assert client.requests[1][0] == (
        f"https://{_HOST}/files-pri/T1-F1/download/notes.txt"
    )


async def test_each_redirect_is_closed_before_the_next_request_goes_out() -> None:
    """A redirect body is never read, so a large one costs nothing."""
    second = f"https://{_HOST}/two"
    client = _Client({_URL: _redirect(second), second: _redirect(f"https://{_HOST}/3")})
    async with stream_slack_file(client, _URL, headers=_TOKEN, allowed_hosts=_HOSTS):
        pass
    assert client.table[_URL].closed is True
    assert client.table[second].closed is True


async def test_a_chain_longer_than_the_bound_is_abandoned() -> None:
    table = {
        f"https://{_HOST}/{n}": _redirect(f"https://{_HOST}/{n + 1}")
        for n in range(MAX_FILE_REDIRECTS + 2)
    }
    client = _Client(table)
    with pytest.raises(SlackFileTransferRefused) as caught:
        async with stream_slack_file(
            client, f"https://{_HOST}/0", headers=_TOKEN, allowed_hosts=_HOSTS
        ):
            pass  # pragma: no cover
    assert caught.value.code == REFUSAL_TOO_MANY_REDIRECTS
    assert len(client.requests) == MAX_FILE_REDIRECTS + 1


async def test_a_redirect_without_a_location_is_handed_to_the_caller() -> None:
    """Not a second answer invented here; the caller's status handling reports it."""
    client = _Client({_URL: _Response(302, {})})
    async with stream_slack_file(
        client, _URL, headers=_TOKEN, allowed_hosts=_HOSTS
    ) as response:
        assert response.status_code == 302
    assert len(client.requests) == 1


async def test_the_first_url_faces_the_allow_list_too() -> None:
    client = _Client({})
    with pytest.raises(SlackFileTransferRefused) as caught:
        async with stream_slack_file(
            client,
            "https://docs.google.com/document/d/x",
            headers=_TOKEN,
            allowed_hosts=_HOSTS,
        ):
            pass  # pragma: no cover
    assert caught.value.code == REFUSAL_HOST_NOT_ALLOWED
    assert "the download URL" in caught.value.detail
    assert client.requests == []
