"""Check a built Mnema.app without recognition and without downloading models.

    uv run python -m scripts.smoke_mac_app path/to/Mnema.app [--version X.Y.Z] [--ffmpeg]

The signature holds, no Mach-O loads anything outside the bundle and the system, the bundled
mnema and mnema-mcp start, mnema finds and runs the bundled ffmpeg and ffprobe, the recognition
engines of the Mac list (mnema/extras.py) are installed and nothing else is, the libraries they
import load after the bundle trim, and the engine started the way the app starts it (the Swift
launcher, the embedded Python) answers with a panel state and quits; started as `mnema-bar
--headless`, it offers exactly Automatic and the shipped engines. Everything runs under a
temporary HOME: nothing of the real user's settings is read or written.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.build_mac_app import _DIST_DROP, NOT_SHIPPED, load_commands, macho_files, outside


class SmokeError(SystemExit):
    pass


def say(text: str) -> None:
    print(f"smoke_mac_app: {text}", flush=True)


def foreign_links(binary: Path) -> list[str]:
    """Dependencies, rpaths and install names that point outside the bundle and the system."""
    return [f"{cmd} {path}" for cmd, path in load_commands(binary) if outside(path)]


def check_links(app: Path) -> int:
    files = macho_files(app)
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
        found = dict(zip(files, pool.map(foreign_links, files), strict=True))
    bad = {f: links for f, links in found.items() if links}
    if bad:
        lines = [f"{f.relative_to(app)}: {', '.join(links)}" for f, links in list(bad.items())[:20]]
        raise SmokeError("Mach-O outside the bundle and the system:\n" + "\n".join(lines))
    return len(files)


def isolated_env(home: Path) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("MNEMA_", "PYTHON", "VIRTUAL_ENV", "UV_", "XDG_"))
    }
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_CACHE_HOME=str(home / ".cache"),
        XDG_DATA_HOME=str(home / ".local" / "share"),
        MNEMA_LOG=str(home / "bar.log"),
        MNEMA_UI_LANG="en",
        PATH="/usr/bin:/bin:/usr/sbin:/sbin",  # nothing from Homebrew or a dev venv
    )
    return env


def run(cmd: list[str], env: dict[str, str], **kw) -> subprocess.CompletedProcess:
    kw.setdefault("timeout", 300)
    return subprocess.run(cmd, env=env, capture_output=True, text=True, **kw)


def check_mcp(app: Path, env: dict[str, str]) -> None:
    hello = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "0"},
        },
    }
    out = run([str(app / "Contents/Resources/bin/mnema-mcp")], env, input=json.dumps(hello) + "\n")
    answers = [json.loads(line) for line in out.stdout.splitlines() if line.startswith("{")]
    if not any(a.get("id") == 1 and "serverInfo" in a.get("result", {}) for a in answers):
        raise SmokeError(
            f"mnema-mcp did not answer initialize:\n{out.stdout}\n{out.stderr[-2000:]}"
        )


# mnema's own media path inside the bundle, run the way the engine runs it: the tools it resolves
# and a real probe + mp3 extraction of a generated tone
_MEDIA_CHECK = """
import json, subprocess, sys
from pathlib import Path
from mnema.core import audio, base
from mnema.core.probe import probe
work = Path(sys.argv[1])
tone = work / "tone.wav"
subprocess.run([base.tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "sine=d=2",
                str(tone)], check=True, stdin=subprocess.DEVNULL)
mp3 = audio.extract_audio(tone, work / "tone.mp3", compressed=True)
print(json.dumps({"ffmpeg": base.tool("ffmpeg"), "ffprobe": base.tool("ffprobe"),
                  "seconds": probe(mp3).duration}))
"""


def check_media(app: Path, env: dict[str, str], work: Path) -> None:
    """base.tool picks the bundled ffmpeg and ffprobe even with Homebrew's ahead on PATH."""
    res = app / "Contents" / "Resources"
    env = {**env, "MNEMA_APP_BUNDLE": str(app), "PATH": f"/opt/homebrew/bin:{env['PATH']}"}
    python = res / "python" / "bin" / "python3.12"
    out = run([str(python), "-I", "-B", "-c", _MEDIA_CHECK, str(work)], env)
    if out.returncode != 0:
        raise SmokeError(f"media check failed:\n{out.stdout}\n{out.stderr[-3000:]}")
    found = json.loads(out.stdout.strip().splitlines()[-1])
    for tool in ("ffmpeg", "ffprobe"):
        if Path(found[tool]).resolve() != (res / "bin" / tool).resolve():
            raise SmokeError(f"mnema resolves {tool} to {found[tool]}, not the bundled one")
    if abs(found["seconds"] - 2) > 0.1:
        raise SmokeError(f"the 2 s tone came out {found['seconds']} s long")


# The engines inside the bundle: the registry against the Mac list, every library the shipped
# engines' extras pull in imports (what the trim must not break), silero-vad's bundled weights on
# a tone, and yt-dlp's format selection without mutagen
_ENGINES_CHECK = """
import importlib, importlib.metadata as md, importlib.util, json, sys
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from mnema import extras, node_settings
from mnema.core import backends
removed = set(json.loads(sys.argv[1]))
shipped = [e for e in backends.ENGINES if e.extra in extras.MAC]

def requires(dist, extra=""):
    found = []
    for line in md.requires(dist) or []:
        req = Requirement(line)
        marker = req.marker
        if marker is None:
            applies = not extra
        else:  # an extra's lines name it; another dist's optional extras are not installed
            applies = (not extra or "extra" in str(marker)) and marker.evaluate({"extra": extra})
        if applies:
            found.append(canonicalize_name(req.name))
    return found

direct = {d for e in shipped for d in requires("mnema", e.extra)}
todo, seen, missing = list(direct), set(), []
while todo:
    dist = todo.pop()
    if dist in seen:
        continue
    seen.add(dist)
    try:
        todo += requires(dist)
    except md.PackageNotFoundError:
        if dist in direct or dist not in removed:
            missing.append(dist)
modules = {canonicalize_name(d): set() for d in seen}
for name, dists in md.packages_distributions().items():
    for dist in dists:
        modules.get(canonicalize_name(dist), set()).add(name)
names = sorted({m for found in modules.values() for m in found if m.isidentifier()})
failed = {}
for name in [*names, "mnema.core.parakeet_mlx", "mnema.core.worker"]:
    if name.startswith("_") or importlib.util.find_spec(name) is None:
        continue
    try:
        importlib.import_module(name)
    except Exception as e:
        failed[name] = repr(e)[:300]
if importlib.util.find_spec("silero_vad") is not None:
    import torch
    tone = torch.sin(torch.arange(32000) * (2 * 3.14159 * 440 / 16000)) * 0.5
    backends._speech_timestamps(tone)
import yt_dlp
from yt_dlp.dependencies import mutagen
info = {"id": "x", "title": "t", "extractor": "generic", "extractor_key": "Generic",
        "webpage_url": "https://example.com/x", "formats": [
            {"format_id": "a", "url": "https://example.com/a.m4a", "ext": "m4a",
             "acodec": "mp4a.40.2", "vcodec": "none", "abr": 128},
            {"format_id": "v", "url": "https://example.com/v.mp4", "ext": "mp4",
             "acodec": "none", "vcodec": "avc1", "height": 720}]}
with yt_dlp.YoutubeDL({"quiet": True, "format": "ba/b"}) as ydl:
    picked = ydl.process_ie_result(info, download=False)["format_id"]
print(json.dumps({
    "shipped": [e.id for e in shipped],
    "installed": [e.id for e in backends.installed_engines()],
    "choices": [c["id"] for c in node_settings.local_asr_choices()],
    "missing": sorted(missing), "failed": failed, "imported": len(names),
    "mutagen": mutagen is not None, "format": picked,
}))
"""


def check_engines(app: Path, env: dict[str, str]) -> tuple[list[str], int]:
    """The engines of the Mac list are installed and no other is, and every library their extras
    pull in imports; returns Automatic with them and how many libraries imported."""
    python = app / "Contents" / "Resources" / "python" / "bin" / "python3.12"
    env = {**env, "MNEMA_APP_BUNDLE": str(app)}
    removed = json.dumps([*_DIST_DROP, *NOT_SHIPPED])
    out = run([str(python), "-I", "-B", "-c", _ENGINES_CHECK, removed], env)
    if out.returncode != 0:
        raise SmokeError(f"engine check failed:\n{out.stdout}\n{out.stderr[-3000:]}")
    found = json.loads(out.stdout.strip().splitlines()[-1])
    if not found["shipped"] or found["installed"] != found["shipped"]:
        raise SmokeError(f"engines installed {found['installed']}, shipped {found['shipped']}")
    if found["missing"]:
        raise SmokeError(f"the shipped engines need {found['missing']}, not in the bundle")
    if found["failed"]:
        raise SmokeError(f"libraries of the shipped engines do not import: {found['failed']}")
    expected = ["auto", *found["shipped"]]
    if found["choices"] != expected:
        raise SmokeError(f"local recognition {found['choices']}, expected {expected}")
    if found["mutagen"] or found["format"] != "a":
        raise SmokeError(f"yt-dlp without mutagen: {found}")
    return expected, found["imported"]


def _recognition(state: dict) -> list[str] | None:
    """The asr: choices of the Recognition field in a panel state."""
    for tab in state["panel"]["settings"]["tabs"]:
        for section in tab["sections"]:
            for field in section["fields"]:
                keys = [c["key"] for c in field.get("choices") or []]
                if keys and all(k.startswith("asr:") for k in keys):
                    return [k.removeprefix("asr:") for k in keys]
    return None


def check_recognition(app: Path, env: dict[str, str], expected: list[str]) -> None:
    """mnema-bar --headless in the bundle offers Automatic and the shipped engines, no more."""
    python = app / "Contents" / "Resources" / "python" / "bin" / "python3.12"
    res = app / "Contents" / "Resources"
    env = {
        **env,
        "MNEMA_APP_BUNDLE": str(app),
        "PATH": f"{res / 'bin'}:{res / 'python' / 'bin'}:{env['PATH']}",
    }
    cmd = [str(python), "-I", "-B", "-m", "mnema.bar", "--headless"]
    proc = subprocess.Popen(
        cmd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True,
    )  # fmt: skip
    lines: queue.SimpleQueue[str | None] = queue.SimpleQueue()

    def read() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=read, daemon=True).start()
    offered: list[str] | None = None
    deadline = time.monotonic() + 120
    try:
        while time.monotonic() < deadline:  # the list loads in the background after the start
            try:
                line = lines.get(timeout=1)
            except queue.Empty:
                continue
            if line is None:
                break
            msg = json.loads(line)
            if msg.get("type") == "state":
                offered = _recognition(msg) or offered
                if offered and len(offered) > 1:
                    break
    finally:
        assert proc.stdin is not None
        try:
            proc.stdin.write(json.dumps({"type": "action", "key": "quit_now"}) + "\n")
            proc.stdin.flush()
        except BrokenPipeError:
            pass
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    if offered != expected:
        raise SmokeError(f"the app offers recognition {offered}, expected {expected}")


def smoke(app: Path, *, version: str | None = None, ffmpeg: bool = False) -> None:
    """ffmpeg: the app must carry its own ffmpeg and ffprobe (a release build)."""
    app = app.resolve()
    res = app / "Contents" / "Resources"
    out = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", str(app)], capture_output=True, text=True
    )
    if out.returncode != 0:
        raise SmokeError(f"codesign --verify failed:\n{out.stderr}")
    say("signature ok")
    say(f"{check_links(app)} Mach-O files load only the bundle and the system")
    with tempfile.TemporaryDirectory(prefix="mnema-smoke-") as tmp:
        home = Path(tmp)
        env = isolated_env(home)
        out = run([str(res / "bin" / "mnema"), "--version"], env)
        if out.returncode != 0 or not re.search(rf"mnema {re.escape(version or '')}", out.stdout):
            raise SmokeError(f"mnema --version:\n{out.stdout}\n{out.stderr[-2000:]}")
        say(out.stdout.strip().splitlines()[-1])
        check_mcp(app, env)
        say("mnema-mcp answers initialize")
        tools = res / "bin"
        if (tools / "ffmpeg").exists():
            for tool in ("ffmpeg", "ffprobe"):
                out = run([str(tools / tool), "-version"], env)
                if out.returncode != 0:
                    raise SmokeError(f"{tool} -version:\n{out.stderr}")
            say(out.stdout.splitlines()[0] if out.stdout else "ffprobe ok")
            check_media(app, env, home)
            say("mnema uses the bundled ffmpeg and ffprobe: a 2 s tone probed and made mp3")
        elif ffmpeg:
            raise SmokeError("no bundled ffmpeg and ffprobe in Contents/Resources/bin")
        else:
            say("WARNING: no bundled ffmpeg, the app needs ffmpeg on PATH")
        out = run([str(app / "Contents" / "MacOS" / "Mnema"), "--engine-check"], env)
        log = (home / "bar.log").read_text(errors="replace") if (home / "bar.log").exists() else ""
        if out.returncode != 0:
            raise SmokeError(f"engine check failed:\n{out.stdout}\n{out.stderr}\n{log[-4000:]}")
        if "Traceback" in log:
            raise SmokeError(f"the engine logged an error:\n{log[-4000:]}")
        say("the engine answered the app with a state and quit")
        link = home / ".local" / "bin" / "mnema"
        if Path(os.path.realpath(link)) != (res / "bin" / "mnema").resolve():
            raise SmokeError(f"first start did not link {link} to the bundle")
        agent = home / "Library" / "LaunchAgents" / "com.mnema.bar.plist"
        if not agent.exists() or str(app) not in agent.read_text(errors="replace"):
            raise SmokeError(f"first start did not point {agent} at {app}")
        say("first start linked the mnema command and the login item to this bundle")
        expected, imported = check_engines(app, env)
        say(f"engines {', '.join(expected[1:])} installed, no other; {imported} libraries load")
        check_recognition(app, env, expected)
        log = (home / "bar.log").read_text(errors="replace")
        if "Traceback" in log:
            raise SmokeError(f"the engine logged an error:\n{log[-4000:]}")
        say(f"the app offers recognition: {', '.join(expected)}")
    say(f"{app} ok")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Check a built Mnema.app")
    p.add_argument("app", type=Path)
    p.add_argument("--version")
    p.add_argument("--ffmpeg", action="store_true", help="fail without the bundled ffmpeg")
    args = p.parse_args(argv)
    smoke(args.app, version=args.version, ffmpeg=args.ffmpeg)


if __name__ == "__main__":
    main()
