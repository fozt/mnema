"""Check a built Mnema.app without recognition and without downloading models.

    uv run python -m scripts.smoke_mac_app path/to/Mnema.app [--version X.Y.Z] [--ffmpeg]

The signature holds, no Mach-O loads anything outside the bundle and the system, mnema-asr answers
--version and lists its models without downloading any, the bundled ffmpeg and ffprobe run and
mnema finds them, no package of the forbidden list is in the bundle and every library in it
imports after the trim (system audio included), and the old mnema-mcp names the wheel of the
command. Then the engine is started the way the app starts it (the Swift launcher, the embedded
Python): it answers with a panel state and quits, its first start writes app.json and the login
item, and `mnema-bar --headless` offers exactly Automatic and the models of mnema-asr, as
local_asr_choices does. Everything runs under a temporary HOME: nothing of the real user's
settings is read or written.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
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

from mnema.core.base import app_helper
from scripts.build_mac_app import FORBIDDEN, WHEEL, load_commands, macho_files, outside


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


def check_asr(app: Path, env: dict[str, str], home: Path) -> list[str]:
    """mnema-asr starts from the bundle (no build folder next to it), says its protocol version
    and lists its models; nothing is downloaded. Returns the model ids."""
    asr = str(app_helper(app))
    out = run([asr, "--version"], env, cwd=home)
    if out.returncode != 0 or not re.fullmatch(r"mnema-asr \d+", out.stdout.strip()):
        raise SmokeError(f"mnema-asr --version:\n{out.stdout}\n{out.stderr[-2000:]}")
    request = json.dumps({"operation": "models", "payload": {}}) + "\n"
    out = run([asr], env, cwd=home, input=request)
    replies = [json.loads(line) for line in out.stdout.splitlines() if line.startswith("{")]
    models = [r["value"] for r in replies if r.get("type") == "result"]
    if out.returncode != 0 or len(models) != 1 or not models[0]:
        raise SmokeError(f"mnema-asr models:\n{out.stdout}\n{out.stderr[-2000:]}")
    if any(m["installed"] for m in models[0]):
        raise SmokeError(f"mnema-asr sees models in an empty HOME: {models[0]}")
    if downloaded := [p for p in home.rglob("*") if p.is_file()]:
        raise SmokeError(f"mnema-asr models wrote files: {downloaded[:5]}")
    return [m["id"] for m in models[0]]


def check_mcp(app: Path, env: dict[str, str], version: str) -> None:
    """The mnema-mcp of old MCP configs fails and names the command's wheel of this release."""
    stub = app / "Contents" / "Resources" / "bin" / "mnema-mcp"
    out = run([str(stub)], env, input="{}\n")
    wheel = WHEEL.format(version=version)
    if out.returncode == 0 or out.stdout or wheel not in out.stderr:
        raise SmokeError(f"mnema-mcp:\n{out.stdout}\n{out.stderr[-2000:]}")


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


# The packages inside the bundle: none of the forbidden list, and every top-level module of every
# distribution imports (what the trim must not break), with the app's own modules that load
# libraries late: system audio (numpy, pyobjc) and the recognition worker.
_PACKAGES_CHECK = """
import importlib, importlib.metadata as md, importlib.util, json, re, sys
forbidden = set(json.loads(sys.argv[1]))
dists = {re.sub(r"[-_.]+", "-", d.metadata["Name"]).lower() for d in md.distributions()}
names = sorted({m for m in md.packages_distributions() if m.isidentifier()})
failed = {}
for name in [*names, "mnema.live.system_audio", "mnema.core.worker", "mnema.bar.headless"]:
    if name.startswith("_") or importlib.util.find_spec(name) is None:
        continue
    try:
        importlib.import_module(name)
    except Exception as e:
        failed[name] = repr(e)[:300]
print(json.dumps({"forbidden": sorted(dists & forbidden), "failed": failed,
                  "imported": len(names), "dists": len(dists)}))
"""


def check_packages(app: Path, env: dict[str, str]) -> tuple[int, int]:
    """No forbidden package, and everything in the bundle imports; returns how many
    distributions and modules."""
    python = app / "Contents" / "Resources" / "python" / "bin" / "python3.12"
    env = {**env, "MNEMA_APP_BUNDLE": str(app)}
    out = run([str(python), "-I", "-B", "-c", _PACKAGES_CHECK, json.dumps(FORBIDDEN)], env)
    if out.returncode != 0:
        raise SmokeError(f"package check failed:\n{out.stdout}\n{out.stderr[-3000:]}")
    found = json.loads(out.stdout.strip().splitlines()[-1])
    if found["forbidden"]:
        raise SmokeError(f"the bundle carries {found['forbidden']}")
    if found["failed"]:
        raise SmokeError(f"libraries in the bundle do not import: {found['failed']}")
    return found["dists"], found["imported"]


# What the engine of this bundle offers for recognition on this Mac, as the settings list it
_CHOICES_CHECK = """
import json
from mnema import node_settings
print(json.dumps([c["id"] for c in node_settings.local_asr_choices()]))
"""


def check_choices(app: Path, env: dict[str, str], models: list[str]) -> list[str]:
    """local_asr_choices in the bundle: Automatic and the models of mnema-asr."""
    python = app / "Contents" / "Resources" / "python" / "bin" / "python3.12"
    asr = app_helper(app)
    env = {**env, "MNEMA_APP_BUNDLE": str(app), "MNEMA_ASR": str(asr)}
    out = run([str(python), "-I", "-B", "-c", _CHOICES_CHECK], env)
    if out.returncode != 0:
        raise SmokeError(f"local_asr_choices failed:\n{out.stdout}\n{out.stderr[-3000:]}")
    found = json.loads(out.stdout.strip().splitlines()[-1])
    expected = ["auto", *models]
    if found != expected:
        raise SmokeError(f"local recognition {found}, mnema-asr lists {models}")
    return expected


def check_first_start(app: Path, home: Path, version: str) -> None:
    """The engine's first start: app.json names this bundle, its mnema-asr and version (the
    command finds the app there); no mnema link into the bundle; the login item opens it."""
    record = home / ".local" / "share" / "mnema" / "app.json"
    try:
        found = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SmokeError(f"first start did not write {record}: {e}") from None
    wanted = {
        "bundle": str(app),
        "asr": str(app_helper(app)),
        "version": version,
    }
    if {key: found.get(key) for key in wanted} != wanted:
        raise SmokeError(f"{record}: {found}, expected {wanted}")
    link = home / ".local" / "bin" / "mnema"
    if link.is_symlink() and Path(os.path.realpath(link)).is_relative_to(app):
        raise SmokeError(f"first start linked {link} into the bundle")
    agent = home / "Library" / "LaunchAgents" / "com.mnema.bar.plist"
    if not agent.exists() or str(app) not in agent.read_text(errors="replace"):
        raise SmokeError(f"first start did not point {agent} at {app}")


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
    """mnema-bar --headless in the bundle offers Automatic and the models of mnema-asr, no
    more."""
    python = app / "Contents" / "Resources" / "python" / "bin" / "python3.12"
    res = app / "Contents" / "Resources"
    env = {
        **env,
        "MNEMA_APP_BUNDLE": str(app),
        "MNEMA_ASR": str(app_helper(app)),
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


def _log(home: Path) -> str:
    path = home / "bar.log"
    return path.read_text(errors="replace") if path.exists() else ""


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
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    built = info.get("CFBundleShortVersionString", "")
    if version and built != version:
        raise SmokeError(f"the bundle is version {built}, expected {version}")
    with tempfile.TemporaryDirectory(prefix="mnema-smoke-") as tmp:
        home = Path(tmp)
        env = isolated_env(home)
        models = check_asr(app, env, home)
        say(f"mnema-asr answers, models {', '.join(models)}, none downloaded")
        check_mcp(app, env, built)
        say("mnema-mcp names the wheel of the mnema command")
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
        dists, imported = check_packages(app, env)
        say(f"{dists} distributions, none forbidden; {imported} libraries import")
        out = run([str(app / "Contents" / "MacOS" / "Mnema"), "--engine-check"], env)
        if out.returncode != 0:
            raise SmokeError(
                f"engine check failed:\n{out.stdout}\n{out.stderr}\n{_log(home)[-4000:]}"
            )
        if "Traceback" in _log(home):
            raise SmokeError(f"the engine logged an error:\n{_log(home)[-4000:]}")
        say("the engine answered the app with a state and quit")
        check_first_start(app, home, built)
        say("first start wrote app.json and pointed the login item at this bundle")
        expected = check_choices(app, env, models)
        check_recognition(app, env, expected)
        if "Traceback" in _log(home):
            raise SmokeError(f"the engine logged an error:\n{_log(home)[-4000:]}")
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
