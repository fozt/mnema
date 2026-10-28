"""Remote transcriber: URL and message mapping, no real websocket."""

from __future__ import annotations

import pytest

from mnema.live.remote import RemoteStreamingTranscriber, _ws_url


@pytest.mark.parametrize(
    ("base", "ws"),
    [
        ("https://203.0.113.10", "wss://203.0.113.10"),
        ("http://localhost:8000", "ws://localhost:8000"),
    ],
)
def test_ws_url_https_to_wss(base, ws) -> None:
    url = _ws_url(base, {"lang": "ru"})
    assert url == f"{ws}/api/live?lang=ru"  # https → wss keeps TLS
    assert _ws_url(base, {"lang": None}) == f"{ws}/api/live"  # no language, no parameter


def test_ingest_accumulates_then_poll_drains() -> None:
    tr = RemoteStreamingTranscriber(runner="http://x", label="mic")
    tr._ingest({"type": "ready"})
    assert tr._ready.is_set()
    tr._ingest({"committed": [{"start": 0, "end": 1, "text": "раз"}], "draft": "два"})
    tr._ingest({"committed": [{"start": 1, "end": 2, "text": " три"}], "draft": "четыре"})
    c1 = tr.poll()
    assert [s.text for s in c1.committed] == ["раз", " три"]
    assert {s.speaker for s in c1.committed} == {"mic"}  # speaker = source
    assert c1.draft == "четыре"
    c2 = tr.poll()
    assert c2.committed == []  # drained


def test_ingest_server_error_surfaces_and_feed_raises() -> None:
    """A server {'type':'error'} is not swallowed: feed raises a clear MediaError."""
    from mnema.core.base import MediaError

    tr = RemoteStreamingTranscriber(runner="http://x", label="mic")
    tr._ingest({"type": "error", "detail": "whisperlivekit не установлен"})
    assert tr._ready.is_set()  # warmup does not hang
    with pytest.raises(MediaError, match="whisperlivekit не установлен"):
        tr.feed(b"\x00\x01")


def test_send_quiet_swallows_dead_socket() -> None:
    """Sending into a dropped socket does not crash the background task; otherwise asyncio prints
    "Task exception was never retrieved" with a traceback after the session was saved. The drop
    is visible through _error anyway."""
    import asyncio

    tr = RemoteStreamingTranscriber(runner="http://x", label="mic")

    class DeadSocket:
        async def send(self, data):
            raise ConnectionError("no close frame received or sent")

    tr._ws = DeadSocket()
    asyncio.run(tr._send_quiet(b"pcm"))  # does not raise


def test_finish_closes_warming_connection_and_leaves_threads_to_runner(monkeypatch):
    import asyncio
    import threading
    from urllib.parse import parse_qs, urlsplit

    import websockets

    connected, closed = threading.Event(), threading.Event()
    urls = []

    class Connection:
        async def __aenter__(self):
            connected.set()
            return self

        async def __aexit__(self, *args):
            closed.set()

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()  # socket open, no ready yet

    def connect(url, **kwargs):
        urls.append(url)
        return Connection()

    monkeypatch.setenv("MNEMA_ASR_THREADS", "6")
    monkeypatch.setattr(websockets, "connect", connect)
    tr = RemoteStreamingTranscriber(runner="http://localhost")
    tr.start_warmup()
    try:
        assert connected.wait(5)
        tr.finish()
    finally:
        tr.close()
    assert closed.is_set()
    assert tr._thread is not None and not tr._thread.is_alive()
    assert "threads" not in parse_qs(urlsplit(urls[0]).query)


def test_remote_errors_are_english_in_english_ui() -> None:
    from mnema.core.base import MediaError
    from mnema.i18n import using

    tr = RemoteStreamingTranscriber(runner="http://x", label="mic")
    with using("en"):
        tr._ingest({"type": "error"})
        with pytest.raises(MediaError) as err:
            tr.feed(b"\x00")
    assert str(err.value) == "Lost connection to the server: server error"


def test_socket_thread_keeps_the_callers_interface_language(monkeypatch) -> None:
    """The runner closing the session is reported in the language of the thread that connected."""
    import websockets

    from mnema.core.base import MediaError
    from mnema.i18n import using

    class Closed:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    monkeypatch.setattr(websockets, "connect", lambda url, **kw: Closed())
    tr = RemoteStreamingTranscriber(runner="http://localhost")
    with using("en"):
        tr.start_warmup()
    assert tr._thread is not None
    tr._thread.join(5)
    assert isinstance(tr._error, MediaError)
    assert str(tr._error) == "the server closed the connection"
