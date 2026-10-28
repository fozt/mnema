"""This computer's server address for invitations (mnema/node.py), its token
(install_setup.ensure_server_token), and processing a recording on the server over HTTP.

External boundaries: network interfaces (a stub list), the server's HTTP (ASGITransport or
httpx.MockTransport), litellm.
"""

from __future__ import annotations

import array
import json
import types
import wave
from pathlib import Path

import httpx
import pytest

from mnema.install_setup import ensure_server_token
from mnema.live import meta
from mnema.node import node_address, parse_interfaces

TAILSCALE = [("tailscale0", "100.64.0.7")]


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path) -> Path:
    """~/.config/mnema (server.env) in the test's tmp."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_parse_interfaces_reads_ip_and_ifconfig() -> None:
    ip = (
        "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
        "3: wlan0    inet 192.168.1.20/24 brd 192.168.1.255 scope global wlan0\n"
        "3: wlan0    inet6 fe80::1/64 scope link \\       valid_lft\n"
        "7: veth1@if6    inet 172.18.0.1/16 scope global veth1\n"
    )
    assert parse_interfaces(ip) == [
        ("lo", "127.0.0.1"),
        ("wlan0", "192.168.1.20"),
        ("wlan0", "fe80::1"),
        ("veth1", "172.18.0.1"),
    ]
    ifconfig = (
        "lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384\n"
        "\tinet 127.0.0.1 netmask 0xff000000\n"
        "en0: flags=8863<UP,BROADCAST,SMART,RUNNING> mtu 1500\n"
        "\tether 3c:22:fb:00:00:01\n"
        "\tinet6 fe80::1c2b:aa:bb:cc%en0 prefixlen 64 secured scopeid 0x6\n"
        "\tinet 10.0.1.12 netmask 0xffffff00 broadcast 10.0.1.255\n"
        "utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280\n"
        "\tinet 100.101.102.103 --> 100.101.102.103 netmask 0xffffffff\n"
    )
    assert parse_interfaces(ifconfig) == [
        ("lo0", "127.0.0.1"),
        ("en0", "fe80::1c2b:aa:bb:cc"),
        ("en0", "10.0.1.12"),
        ("utun4", "100.101.102.103"),
    ]


@pytest.mark.parametrize(
    ("found", "address"),
    [
        # Tailscale is reachable from any VPN network, even with a home address listed first
        ([("lo", "127.0.0.1"), ("en0", "192.168.1.5"), ("utun4", "100.101.102.103")],
         "100.101.102.103"),
        # IPv6 Tailscale in brackets, as in a URL; IPv4 Tailscale is preferred
        ([("tailscale0", "fd7a:115c:a1e0::1"), ("en0", "192.168.1.5")], "[fd7a:115c:a1e0::1]"),
        ([("tailscale0", "fd7a:115c:a1e0::1"), ("tailscale0", "100.64.0.7")], "100.64.0.7"),
        # a home network: the first private IPv4, not loopback, link-local or a container bridge
        ([("lo", "127.0.0.1"), ("en0", "169.254.3.4"), ("docker0", "172.17.0.1"),
          ("br-0001", "172.18.0.1"), ("wlan0", "192.168.1.20"), ("eth1", "10.0.0.2")],
         "192.168.1.20"),
        # a VPS without VPN: the phone sees the public IPv4, not internal private addresses
        ([("lo", "127.0.0.1"), ("docker0", "172.17.0.1"), ("eth0", "93.184.216.34"),
          ("eth0", "2a00:1450:4010::65")], "93.184.216.34"),
        ([("eth0", "93.184.216.34"), ("eth1", "10.0.0.2")], "93.184.216.34"),
        # DigitalOcean: anchor 10.x on eth0 before the public one, VPC 10.x on eth1
        ([("eth0", "10.19.0.5"), ("eth0", "164.90.200.11"), ("eth1", "10.114.0.2")],
         "164.90.200.11"),
        # Hetzner with a private network: ens10
        ([("eth0", "49.12.100.7"), ("ens10", "10.0.0.3")], "49.12.100.7"),
        ([("lo", "127.0.0.1"), ("docker0", "172.17.0.1")], None),
        ([], None),
    ],
    ids=["tailscale-first", "tailscale-v6", "tailscale-v4-over-v6", "lan", "public-only",
         "public-over-private", "vps-anchor-and-vpc", "vps-private-network", "bridge-only",
         "none"],
)  # fmt: skip
def test_node_address_prefers_tailscale_then_public_then_lan(found, address) -> None:
    assert node_address(found) == address


def test_identity_creates_a_private_token_once(_home) -> None:
    """Without server.env: a new token in a file for the owner only; afterwards the same token
    (the devices' connections stay valid)."""
    token = ensure_server_token()
    config = _home / ".config/mnema"
    assert (config / "server.env").read_text() == f'MNEMA_TOKEN="{token}"\n'
    assert len(token) >= 32
    assert oct((config / "server.env").stat().st_mode & 0o777) == "0o600"
    assert oct(config.stat().st_mode & 0o777) == "0o700"
    assert ensure_server_token() == token


def test_ensure_server_token_does_not_take_the_client_token(_home) -> None:
    """The client's ~/.config/mnema/.env holds another server's token: this computer's server
    does not take it."""
    config = _home / ".config/mnema"
    config.mkdir(parents=True)
    (config / ".env").write_text('MNEMA_TOKEN="remote-token"\n')
    assert ensure_server_token() != "remote-token"


RU_TEXT = "mic: обсудили релиз, сроки и кто готовит демо.\nsystem: я беру демо к пятнице.\n" * 10
SUMMARY_JSON = (
    '{"title": "Созвон о релизе", "about": "Обсудили релиз.", "summary": ["Демо — к пятнице"]}'
)


def _litellm(monkeypatch, answer) -> list[dict]:
    """litellm at the boundary, for both the node (TestClient in this process) and this machine."""
    import litellm

    from mnema.llm import claude

    calls: list[dict] = []

    def completion(**kw):
        calls.append(kw)
        out = answer(kw)
        if isinstance(out, BaseException):
            raise out
        delta = types.SimpleNamespace(content=out)
        choice = types.SimpleNamespace(delta=delta, finish_reason="stop")
        return iter([types.SimpleNamespace(choices=[choice])])

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(claude.time, "sleep", lambda s: None)
    return calls


class _Recorded(httpx.ASGITransport):
    """A real node (create_app) without network; the client's requests in order."""

    def __init__(self, app) -> None:
        super().__init__(app=app)
        self.requests: list[tuple[str, str]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        return await super().handle_async_request(request)


def _limits(monkeypatch) -> None:
    from mnema.llm import limits, providers

    monkeypatch.setattr(
        providers, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(404, json={}))
    )
    monkeypatch.setattr(limits, "_FETCHED", {})


def _recording(tmp_path: Path, name: str = "2026-10-22_13-05") -> Path:
    folder = tmp_path / "notes" / name
    folder.mkdir(parents=True)
    (folder / "transcript.txt").write_text(RU_TEXT, encoding="utf-8")
    return folder


def _last_run(folder: Path) -> dict:
    return json.loads((folder / ".mnema-meta.json").read_text(encoding="utf-8"))["runs"][-1]


def _processing_client(monkeypatch, app):
    from mnema.client import MnemaClient

    transport = _Recorded(app)
    import shutil

    import mnema.live.recording_client as recording_client
    from mnema import node_settings
    from mnema.core import audio

    monkeypatch.setattr(node_settings, "asr_problem", lambda *a, **k: None)
    # the ffmpeg boundary: CI has none
    monkeypatch.setattr(
        audio, "extract_audio", lambda src, dst, **kw: Path(shutil.copyfile(src, dst))
    )
    monkeypatch.setattr(audio, "mix", lambda inputs, dst, **kw: dst.write_bytes(b"ID3") and dst)

    monkeypatch.setattr(
        recording_client,
        "MnemaClient",
        lambda url, *, token=None: MnemaClient(url, token=token, transport=transport),
    )
    return transport


@pytest.mark.parametrize("model", [True, False])
def test_node_summary_uses_only_the_node_model(monkeypatch, tmp_path, model):
    from mnema.connection import Connection
    from mnema.live.recording_client import summarize_on_node
    from mnema.server import create_app

    _limits(monkeypatch)
    calls = _litellm(monkeypatch, lambda kw: SUMMARY_JSON)
    if not model:
        monkeypatch.delenv("MNEMA_MODEL", raising=False)
    transport = _processing_client(monkeypatch, create_app(tmp_path / "node"))
    folder = _recording(tmp_path)
    if not model:
        from mnema.core.base import MediaError

        with pytest.raises(MediaError, match="^На сервере не настроена модель$"):
            summarize_on_node(folder, Connection(url="http://node:8123"))
        assert calls == []
        # refused before work: no work file with a request, the recording as it was
        assert not (folder / ".mnema-processing").exists()
        assert not (folder / "summary.md").exists() and meta.last(folder) is None
        assert (folder / "transcript.txt").read_text() == RU_TEXT
        return
    result = summarize_on_node(folder, Connection(url="http://node:8123"))
    assert ("POST", "/api/jobs") in transport.requests
    assert (result.base / "transcript.txt").read_text() == RU_TEXT
    run = _last_run(result.base)
    assert run["settings"]["server"] == "node:8123"
    assert len(calls) == 1
    assert result.summary_md and "Демо — к пятнице" in result.summary_md.read_text()
    assert result.base.name == "2026-10-22_Созвон о релизе"
    assert run["summary"]["model"] == "anthropic/claude-sonnet-5"


@pytest.mark.parametrize("refused", [False, True])
def test_node_failure_keeps_transcript_without_local_model(monkeypatch, tmp_path, refused):
    from mnema.client import MnemaClient
    from mnema.connection import Connection
    from mnema.core.base import MediaError
    from mnema.live.recording_client import summarize_on_node

    _litellm(monkeypatch, lambda kw: pytest.fail("client must not call a model"))

    def fail(request):
        if refused:
            return httpx.Response(401, json={"detail": "unauthorized"})
        raise httpx.ConnectError("offline", request=request)

    transport = httpx.MockTransport(fail)
    monkeypatch.setattr(
        MnemaClient,
        "_make_async_client",
        lambda self: httpx.AsyncClient(base_url=self._base_url, transport=transport),
    )
    folder = _recording(tmp_path)
    expected = MediaError if refused else httpx.ConnectError
    reason = "unauthorized" if refused else "offline"
    with pytest.raises(expected, match=reason):
        summarize_on_node(folder, Connection(url="http://node:8123"))
    assert (folder / "transcript.txt").read_text() == RU_TEXT
    assert not (folder / "summary.md").exists()


def test_recording_without_summary_goes_through_node_http(monkeypatch, tmp_path):
    from mnema.connection import Connection
    from mnema.live.recording_client import recover_on_node
    from mnema.live.setup import LiveConfig
    from mnema.server import create_app
    from tests.test_live_setup import _fake_dispatch

    _limits(monkeypatch)
    _litellm(monkeypatch, lambda kw: SUMMARY_JSON)
    transport = _processing_client(monkeypatch, create_app(tmp_path / "node"))
    calls = []
    _fake_dispatch(
        monkeypatch,
        {"text": RU_TEXT, "segments": [{"start": 0, "end": 1, "text": RU_TEXT}]},
        calls=calls,
    )
    folder = tmp_path / "notes" / "2026-10-22_13-05"
    folder.mkdir(parents=True)
    with wave.open(str(folder / "mic.wav"), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(array.array("h", [3000] * 16000).tobytes())
    result, text = recover_on_node(
        folder, LiveConfig(summary=False, mix_audio=False), Connection(url="http://node:8123")
    )
    assert text and result.transcript_txt.is_file()
    assert [c["op"] for c in calls] == ["transcribe"]
    assert ("POST", "/api/jobs") in transport.requests
    assert result.summary_md is None


@pytest.mark.parametrize("summary", [False, True])
def test_node_title_failure_is_about_title(monkeypatch, tmp_path, summary):
    from mnema.connection import Connection
    from mnema.live.recording_client import recover_on_node
    from mnema.live.remarks import Step
    from mnema.live.setup import LiveConfig
    from mnema.server import create_app
    from tests.test_live_setup import _fake_dispatch

    _limits(monkeypatch)
    _litellm(monkeypatch, lambda kw: RuntimeError("Insufficient Balance"))
    _processing_client(monkeypatch, create_app(tmp_path / "node"))
    text = "Коротко обсудили релиз."
    _fake_dispatch(monkeypatch, {"text": text, "segments": [{"start": 0, "end": 1, "text": text}]})
    folder = tmp_path / "notes" / "2026-10-22_13-05"
    folder.mkdir(parents=True)
    with wave.open(str(folder / "mic.wav"), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(array.array("h", [3000] * 16000).tobytes())
    result, full = recover_on_node(
        folder, LiveConfig(summary=summary, mix_audio=False), Connection(url="http://node:8123")
    )
    assert full and result.summary_md is None
    assert result.base == folder
    assert [r.step for r in result.remarks] == [Step.TITLE]
