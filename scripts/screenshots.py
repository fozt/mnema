"""Render the README screenshots of the Mac panel and settings, in English and in Russian.

    uv run python -m scripts.screenshots [--out docs/images]

The panel state is built by the real panel and settings models (mnema.bar.panel,
mnema.bar.settings_model) from invented recordings, in each interface language, and drawn by the
Mac app's own preview (`Mnema --preview panel|settings`, built here with `swift build`) with
MNEMA_UI_LANG set, so the app's own texts follow the language too. Writes panel-light.png,
panel-dark.png, settings-light.png and settings-dark.png (English) and the same names with .ru
before .png (Russian). macOS only.
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
from mnema.bar.state import BarPrefs, BarState, Job, LocalModel, MenuEnv
from mnema.core.backends import ParakeetBackend
from mnema.i18n import _, using
from mnema.live.meta import RecordState
from mnema.live.models import FINALIZE_STEPS, STEP_MESSAGES

ROOT = Path(__file__).resolve().parent.parent
MACOS = ROOT / "mnema" / "bar" / "macos"
LANGS = ("en", "ru")
NOW = datetime(2026, 10, 21, 18, 0)

# (title, section, start, minutes, gist, video); the first one is being processed
_CONTENT = {
    "en": [
        (
            "Product demo for the client",
            "Work",
            NOW.replace(hour=16, minute=10),
            48,
            "",
            True,
        ),
        (
            "Release 2.0 planning",
            "Work",
            NOW.replace(hour=11, minute=30),
            42,
            "Agreed to ship 2.0 at the end of November without the export feature.",
            True,
        ),
        (
            "Payments incident review",
            "Work",
            NOW.replace(hour=9, minute=15),
            18,
            "Payments failed because of an expired certificate; renewal is now automatic.",
            False,
        ),
        (
            "Onboarding call with the designer",
            "Projects",
            NOW.replace(hour=17, minute=40) - timedelta(days=1),
            35,
            "Cut onboarding down to three screens and dropped the mandatory sign-up.",
            False,
        ),
    ],
    "ru": [
        (
            "Демо продукта для клиента",
            "Работа",
            NOW.replace(hour=16, minute=10),
            48,
            "",
            True,
        ),
        (
            "Планирование релиза 2.0",
            "Работа",
            NOW.replace(hour=11, minute=30),
            42,
            "Договорились выпустить 2.0 в конце ноября без экспорта.",
            True,
        ),
        (
            "Разбор инцидента с оплатами",
            "Работа",
            NOW.replace(hour=9, minute=15),
            18,
            "Платежи падали из-за просроченного сертификата, теперь он продлевается сам.",
            False,
        ),
        (
            "Созвон с дизайнером по онбордингу",
            "Проекты",
            NOW.replace(hour=17, minute=40) - timedelta(days=1),
            35,
            "Сократили онбординг до трёх экранов и убрали обязательную регистрацию.",
            False,
        ),
    ],
}


def showcase(lang: str) -> StateMsg:
    """The panel at rest with a connected server, the three sources on, one recording being
    processed and a few processed ones, in the given language. The settings window is drawn from
    the same state."""
    with using(lang):
        cards = [
            RecordingCard(
                path=Path("mnema") / section / f"{start:%Y-%m-%d}_{title}",
                id=f"showcase-{i}",
                section=section,
                title=title,
                started=start,
                seconds=minutes * 60,
                gist=gist,
                has_text=bool(gist),
                has_summary=bool(gist),
                has_video=video,
                remarks=[],
                state=RecordState.PROCESSED if gist else RecordState.PROCESSING,
                languages=[lang] if gist else [],
                finished=bool(gist),
            )
            for i, (title, section, start, minutes, gist, video) in enumerate(_CONTENT[lang])
        ]
        # recognition of the first card is 40 % done
        job = Job(
            id=cards[0].id,
            folder=cards[0].path,
            running=True,
            stage=STEP_MESSAGES["asr"],
            progress=0.4 / len(FINALIZE_STEPS),
        )
        sections = sorted({c.section for c in cards})
        prefs = BarPrefs(record_section=cards[0].section, screen=True)
        env = MenuEnv(
            server="http://server.example:8000",
            server_ok=True,
            server_name=_("Server"),
            server_llm=True,
            server_model="gemini/gemini-flash-latest",
            mac=True,
            asr_choices=[(c["id"], c["title"]) for c in _local_choices()],
            asr_model=LocalModel(installed=True, size=630_000_000),
        )
        settings = settings_model(prefs, env)
        state = BarState(jobs=[job])
        panel = panel_model(state, prefs, env, cards, settings, now=NOW, sections=sections)
        return StateMsg(panel=panel)


def _local_choices() -> list[dict[str, str]]:
    """The Mac's engines as its settings list them: Automatic and Parakeet."""
    return [
        {"id": "auto", "title": _("Automatic")},
        {"id": ParakeetBackend.id, "title": ParakeetBackend.title},
    ]


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


# what is drawn: the panel, and the settings window on its Recording tab
VIEWS = {"panel": [], "settings": ["--tab", "record"]}


def render(view: str, state: Path, out: Path, *, lang: str, dark: bool) -> None:
    binary = MACOS / ".build" / "debug" / "Mnema"
    args = [str(binary), "--preview", view, str(state), str(out), *VIEWS[view]]
    if dark:
        args.append("--dark")
    subprocess.run(args, check=True, env=os.environ | {"MNEMA_UI_LANG": lang})
    out.write_bytes(strip_png(out.read_bytes()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the README screenshots of the Mac app")
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
            for view in VIEWS:
                for theme in ("light", "dark"):
                    out = args.out / f"{view}-{theme}{suffix}.png"
                    render(view, state, out, lang=lang, dark=theme == "dark")
                    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
