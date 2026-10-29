"""One connection string: the token is split off before HTTP and kept intact. Parsing, building
the string and "is this a Mnema server" are shared with the phone:
tests/fixtures/connection-v1.json (ConnectionTest.kt checks it too)."""

import json
from pathlib import Path

import httpx
import pytest

from mnema.client import MnemaClient
from mnema.connection import format_connection, format_invite, parse_connection, version_gap

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/connection-v1.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", FIXTURE["parse"], ids=lambda case: repr(case["text"]))
def test_connection_string_is_read_as_on_the_phone(case):
    if "error" in case:
        with pytest.raises(ValueError) as error:
            parse_connection(case["text"])
        assert str(error.value) == FIXTURE["messages"][case["error"]]  # no secret from the string
        return
    parsed = parse_connection(case["text"])
    assert (parsed.url, parsed.token, parsed.invite) == (case["url"], case["token"], case["invite"])


@pytest.mark.parametrize("case", FIXTURE["format"], ids=lambda case: repr(case["url"]))
def test_connection_string_is_made_as_on_the_phone(case):
    if "error" in case:
        with pytest.raises(ValueError) as error:
            format_connection(case["url"], case["token"])
        assert str(error.value) == FIXTURE["messages"][case["error"]]
        return
    line = format_connection(case["url"], case["token"])
    assert line == case["line"]
    assert parse_connection(line).token == (case["token"] or None)


@pytest.mark.parametrize(
    "case", FIXTURE["capabilities"], ids=lambda case: f"{case['status']} {case['body'][:40]}"
)
def test_mnema_server_is_recognised_as_on_the_phone(case):
    """The client reads an address's answer to GET /api/capabilities as the phone does: a Mnema
    server, not one, or no answer; only a Mnema server counts as working (health)."""
    reply = httpx.Response(case["status"], content=case["body"].encode())
    client = MnemaClient("http://host:8000", transport=httpx.MockTransport(lambda _: reply))
    answer, _ = client.probe()
    assert answer == case["answer"]
    assert (client.health() is not None) is (case["answer"] == "mnema")


@pytest.mark.parametrize(
    "case", FIXTURE["versions"], ids=lambda case: f"{case['server']} vs {case['app']}"
)
def test_versions_differing_in_major_minor_say_which_side_to_update(case):
    """The same rule as the phone: a patch difference is fine, an unknown server version is not
    judged."""
    assert version_gap(case["server"], case["app"]) == case["gap"]


def test_token_goes_only_into_the_bearer_header():
    token = "secret-#-%-&-+-'-\\-$HOME"
    parsed = parse_connection(format_connection("http://host:43117/", token))

    def request(req):
        assert req.headers["Authorization"] == f"Bearer {token}"
        assert req.url.fragment == "" and req.url.query == b""
        assert token not in str(req.url)
        return httpx.Response(200, json={"ok": True, "llm": False, "auth": True})

    client = MnemaClient(parsed.url, token=parsed.token, transport=httpx.MockTransport(request))
    assert client.health()["ok"] is True


def test_invite_string_roundtrips_and_is_hidden_from_repr():
    code = "Ab3-_x9" * 4
    text = format_invite("http://node:43117/", code)
    assert text == f"http://node:43117#invite={code}"
    parsed = parse_connection(text)
    assert (parsed.url, parsed.invite, parsed.token) == ("http://node:43117", code, None)
    assert code not in repr(parsed)
    assert parse_connection(format_connection("http://node", "t")).invite is None


@pytest.mark.parametrize(
    "build, url, secret",
    [
        (format_invite, "http://host#token=a", "code"),
        (format_invite, "http://host#invite=a", "code"),
        (format_invite, "http://host", ""),
        (format_connection, "http://host#invite=code", "token"),
    ],
)
def test_format_invite_needs_plain_address_and_code(build, url, secret):
    with pytest.raises(ValueError):
        build(url, secret)
