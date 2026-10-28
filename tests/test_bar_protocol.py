"""The macOS app ↔ engine protocol (mnema/bar/protocol.py): messages both ways, the Swift samples
are current, the Swift code builds without Xcode."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mnema.bar import protocol as p
from mnema.i18n import using


def test_every_sample_roundtrips_through_its_direction() -> None:
    for name, msg in p.samples().items():
        line = p.encode(msg)
        assert line.endswith("\n") and "\n" not in line[:-1], name
        adapter = p.OUTGOING if name.startswith("out-") else p.INCOMING
        assert adapter.validate_json(line) == msg, name


def test_incoming_decodes_what_the_app_sends_and_rejects_garbage() -> None:
    assert p.decode_incoming('{"type":"action","key":"start"}') == p.ActionMsg(key="start")
    answer = p.decode_incoming('{"type":"answer","id":4,"value":["http://srv","t"]}')
    assert isinstance(answer, p.AnswerMsg) and answer.value == ["http://srv", "t"]
    assert p.decode_incoming('{"type":"panel","open":false}') == p.PanelMsg(open=False)
    for bad in ("", "не json", '{"type":"nope"}', '{"type":"action"}'):
        with pytest.raises(ValueError):
            p.decode_incoming(bad)


def test_fixtures_match_the_models(tmp_path) -> None:
    """Swift tests and snapshots read Fixtures: regenerate them after changing the models
    (`python -m mnema.bar.protocol`)."""
    fresh = {f.name: f.read_text(encoding="utf-8") for f in p.write_fixtures(tmp_path)}
    stored = {f.name: f.read_text(encoding="utf-8") for f in p.FIXTURES.glob("*.json")}
    assert stored == fresh


def test_swift_sources_avoid_state_macro() -> None:
    """In the macOS 27 SDK @State is a macro whose plugin ships only with Xcode: with Command Line
    Tools alone (as `mnema-bar install` builds) a file with @State does not build. View state lives
    in Store or @StateObject."""
    sources = Path(p.__file__).parent / "macos" / "Sources"
    used = [
        f"{f.name}:{n}"
        for f in sources.rglob("*.swift")
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"@State\b(?!Object)", line.split("//")[0])
    ]
    assert not used, used


def test_form_event_keeps_typed_key_out_of_repr() -> None:
    """The model window event carries the key as typed: repr keeps it out of logs and tracebacks."""
    line = '{"type":"form_event","id":8,"rev":2,"op":"submit","values":{"key":"sk-or-v1-secret"}}'
    msg = p.decode_incoming(line)
    assert isinstance(msg, p.FormEventMsg) and msg.values == {"key": "sk-or-v1-secret"}
    assert "sk-or-v1-secret" not in repr(msg) and "sk-or-v1-secret" not in str(msg)


def test_window_defaults_follow_the_interface_language() -> None:
    """Defaults are translated when a model is built, never frozen at import."""
    with using("en"):
        windows = [
            p.TitleAsk(),
            p.new_section_ask(),
            p.ConfirmAsk(title="Discard?", message="", ok="Discard"),
            p.FolderAsk(start="/"),
            p.RangeAsk(length=60.0),
            p.FormAsk(title="Connect to server", fields=[]),
        ]
        settings = p.SettingsModel(tabs=[])
    texts = [v for w in windows for v in w.model_dump(exclude={"langs"}).values()]
    texts = [t for t in texts if isinstance(t, str)] + [settings.title]
    assert not [t for t in texts if re.search(r"[А-Яа-яЁё]", t)], texts
    assert (windows[1].title, windows[1].ok, windows[0].ok) == ("New section", "Create", "Save")
    assert p.TitleAsk().title == "Название записи"  # conftest pins Russian
