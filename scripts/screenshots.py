"""Render the README screenshots of the Mac panel, in English and in Russian.

    uv run python -m scripts.screenshots [--out docs/images]

The panel state is built by the real panel model (mnema.bar.panel) from invented recordings, in
each interface language, and drawn by the Mac app's own preview (`Mnema --preview panel`, built
here with `swift build`) with MNEMA_UI_LANG set, so the app's own texts follow the language too.
Writes panel-light.png and panel-dark.png (English), panel-light.ru.png and panel-dark.ru.png
(Russian). macOS only.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mnema.bar.panel import panel_model
from mnema.bar.protocol import StateMsg
from mnema.bar.recordings import RecordingCard
from mnema.bar.settings_model import settings_model
from mnema.bar.state import BarPrefs, BarState, MenuEnv
from mnema.i18n import _, using
from mnema.live.meta import RecordState
from mnema.live.remarks import Step, light
from mnema.llm.errors import ErrorKind
from mnema.llm.notes import Cloud, LightNote

ROOT = Path(__file__).resolve().parent.parent
MACOS = ROOT / "mnema" / "bar" / "macos"
LANGS = ("en", "ru")
NOW = datetime(2026, 10, 21, 18, 0)

# (title, section, start, minutes, gist); an empty gist — no summary, with a remark
_CONTENT = {
    "en": [
        (
            "Release 2.0 planning",
            "Work",
            NOW.replace(hour=11, minute=30),
            42,
            "Agreed to ship 2.0 at the end of November without the export feature.",
        ),
        (
            "Payments incident review",
            "Work",
            NOW.replace(hour=9, minute=15),
            18,
            "Payments failed because of an expired certificate; renewal is now automatic.",
        ),
        (
            "Onboarding call with the designer",
            "Projects",
            NOW.replace(hour=17, minute=40) - timedelta(days=1),
            35,
            "Cut onboarding down to three screens and dropped the mandatory sign-up.",
        ),
        ("Weekly team meeting", "Work", NOW.replace(hour=10, minute=0) - timedelta(days=2), 27, ""),
    ],
    "ru": [
        (
            "Планирование релиза 2.0",
            "Работа",
            NOW.replace(hour=11, minute=30),
            42,
            "Договорились выпустить 2.0 в конце ноября без экспорта.",
        ),
        (
            "Разбор инцидента с оплатами",
            "Работа",
            NOW.replace(hour=9, minute=15),
            18,
            "Платежи падали из-за просроченного сертификата, теперь он продлевается сам.",
        ),
        (
            "Созвон с дизайнером по онбордингу",
            "Проекты",
            NOW.replace(hour=17, minute=40) - timedelta(days=1),
            35,
            "Сократили онбординг до трёх экранов и убрали обязательную регистрацию.",
        ),
        (
            "Еженедельная встреча команды",
            "Работа",
            NOW.replace(hour=10, minute=0) - timedelta(days=2),
            27,
            "",
        ),
    ],
}


def showcase(lang: str) -> StateMsg:
    """The panel at rest with a connected server and a few recordings, in the given language."""
    with using(lang):
        remark = light(
            Step.SUMMARY,
            LightNote(
                engine="none",
                cloud=Cloud.FAILED,
                kind=ErrorKind.DAILY_LIMIT,
                model="gemini/gemini-flash-latest",
            ),
        )
        assert remark is not None
        cards = [
            RecordingCard(
                path=Path("mnema") / section / f"{start:%Y-%m-%d}_{title}",
                id=f"showcase-{i}",
                section=section,
                title=title,
                started=start,
                seconds=minutes * 60,
                gist=gist,
                has_text=True,
                has_summary=bool(gist),
                remarks=[] if gist else [remark.text],
                state=RecordState.PROCESSED,
                languages=[lang],
                finished=True,
            )
            for i, (title, section, start, minutes, gist) in enumerate(_CONTENT[lang])
        ]
        sections = sorted({c.section for c in cards})
        prefs = BarPrefs(record_section=cards[0].section)
        env = MenuEnv(
            server="http://server.example:8000",
            server_ok=True,
            server_name=_("Server"),
            server_llm=True,
            mac=True,
        )
        settings = settings_model(prefs, env)
        panel = panel_model(BarState(), prefs, env, cards, settings, now=NOW, sections=sections)
        return StateMsg(panel=panel)


def strip_png(data: bytes) -> bytes:
    """Only the chunks that draw the image: no text, time or other metadata."""
    keep = {b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND", b"sRGB", b"gAMA", b"cHRM"}
    signature, pos, out = data[:8], 8, [data[:8]]
    if signature != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        end = pos + 12 + length
        if kind in keep:
            out.append(data[pos:end])
        pos = end
    return b"".join(out)


def render(state: Path, out: Path, *, lang: str, dark: bool) -> None:
    binary = MACOS / ".build" / "debug" / "Mnema"
    args = [str(binary), "--preview", "panel", str(state), str(out)]
    if dark:
        args.append("--dark")
    subprocess.run(args, check=True, env=os.environ | {"MNEMA_UI_LANG": lang})
    out.write_bytes(strip_png(out.read_bytes()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the README screenshots of the Mac panel")
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "images")
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        raise SystemExit("screenshots: the Mac app's preview renders only on macOS")
    subprocess.run(["swift", "build"], cwd=MACOS, check=True)
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for lang in LANGS:
            state = Path(tmp) / f"state-{lang}.json"
            msg = showcase(lang)
            state.write_text(json.dumps(msg.model_dump(mode="json"), ensure_ascii=False))
            suffix = "" if lang == "en" else f".{lang}"
            for theme in ("light", "dark"):
                out = args.out / f"panel-{theme}{suffix}.png"
                render(state, out, lang=lang, dark=theme == "dark")
                print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
