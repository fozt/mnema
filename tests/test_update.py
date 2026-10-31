"""A release updates a separate installation; the network and the installer are the boundary."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
import tomllib
from pathlib import Path

import httpx
import pytest

from mnema import __version__, install_setup, update
from mnema.core import base
from mnema.core.base import MediaError


@pytest.fixture
def release(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("MNEMA_UPDATE_URL", "https://releases.test/latest")
    monkeypatch.setattr(update.shutil, "which", lambda *a, **k: "/bin/uv")
    monkeypatch.setattr(update.sys, "platform", "linux")  # Mnema.app has its own tests
    monkeypatch.setattr(base, "STANDALONE", False)  # a Linux client
    real_find_spec = update.importlib.util.find_spec
    monkeypatch.setattr(  # the app is installed (CI has no menubar extra)
        update.importlib.util,
        "find_spec",
        lambda name, *a: object() if name == "pystray" else real_find_spec(name, *a),
    )
    # 99.0.0: a release newer than any real one
    state = {"version": "99.0.0", "calls": [], "commands": [], "failure": ""}

    def archive(extra=None):
        files = {
            "pyproject.toml": (
                '[project]\nname="mnema"\nversion="99.0.0"\n'
                '[project.scripts]\nmnema = "mnema.cli:main"\n'
            ),
            "uv.lock": "locked",
        }
        files.update(extra or {})
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w:gz") as package:
            for name, contents in files.items():
                data = contents.encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                package.addfile(info, io.BytesIO(data))
        return out.getvalue()

    state["archive"] = archive()
    state["build_archive"] = archive

    def handler(request):
        state["calls"].append(request.url.path)
        if request.url.path == "/latest":
            return httpx.Response(
                200,
                json={
                    "version": state["version"],
                    "archive": "https://releases.test/archive",
                    "sha256": state.get("sha256") or hashlib.sha256(state["archive"]).hexdigest(),
                },
            )
        return httpx.Response(200, content=state["archive"])

    def run(cmd, **kwargs):
        state["commands"].append((cmd, kwargs))
        failed = state["failure"] and any(state["failure"] in arg for arg in cmd)
        if cmd[:2] == ("/bin/uv", "sync") and not failed:  # uv installs the console scripts
            project = tomllib.loads((kwargs["cwd"] / "pyproject.toml").read_text())["project"]
            (kwargs["cwd"] / ".venv/bin").mkdir(parents=True, exist_ok=True)
            for name in project.get("scripts", {}):
                (kwargs["cwd"] / ".venv/bin" / name).write_text("#!/bin/sh\n")
        stderr = state.get("stderr", "test failure")
        return subprocess.CompletedProcess(cmd, int(bool(failed)), stdout="", stderr=stderr)

    monkeypatch.setattr(update, "TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(subprocess, "run", run)
    return state


def test_check_shows_only_application_version_without_git_or_dependency_resolution(release):
    found = update.check()
    assert found.any and found.release is not None and found.release.version == "99.0.0"
    assert release["commands"] == []
    assert release["calls"] == ["/latest"]


@pytest.mark.parametrize("version", [__version__, "0.0.1"])
def test_current_or_older_release_does_not_install_anything(release, version):
    release["version"] = version
    assert not update.apply().changed
    assert release["commands"] == [] and release["calls"] == ["/latest"]


@pytest.mark.parametrize("archive", [False, True])
def test_http_redirect_is_rejected_before_request_even_if_next_hop_is_https(
    monkeypatch, tmp_path, archive
):
    seen = []

    def redirect(request):
        seen.append(str(request.url))
        if request.url.scheme == "https":
            return httpx.Response(302, headers={"Location": "http://release.test/intermediate"})
        return httpx.Response(302, headers={"Location": "https://another.test/manifest"})

    monkeypatch.setenv("MNEMA_UPDATE_URL", "https://release.test/latest")
    monkeypatch.setattr(update, "TRANSPORT", httpx.MockTransport(redirect))
    with pytest.raises(MediaError, match="незащищённый"):
        if archive:
            update._download(
                update.Release(
                    version="99.0.0", archive="https://release.test/archive", sha256="0" * 64
                ),
                tmp_path / "release.tar.gz",
            )
        else:
            update.check()
    assert len(seen) == 1 and seen[0].startswith("https://")


def test_apply_prepares_new_locked_environment_and_never_changes_checkout(
    release, tmp_path, monkeypatch
):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "CLAUDE.md").write_text("незакоммиченная правка")
    monkeypatch.chdir(checkout)
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(checkout / ".venv"))
    monkeypatch.setenv("PYTHONPATH", str(checkout))
    monkeypatch.setenv("PYTHONEXECUTABLE", str(checkout / ".venv/bin/python"))
    result = update.apply()
    assert result.version == "99.0.0" and result.changed
    assert result.python is not None
    assert Path(result.python).parent.parent.parent.is_relative_to(tmp_path / "data/mnema/releases")
    commands = release["commands"]
    assert len(commands) == 3
    assert commands[0][0][:7] == (
        "/bin/uv",
        "sync",
        "--locked",
        "--no-dev",
        "--python",
        "3.12",
        "--extra",
    )
    assert "--upgrade" not in commands[0][0]
    assert "install_release" in commands[-1][0][-1]
    for command, kwargs in commands:
        assert kwargs["cwd"].is_relative_to(tmp_path / "data/mnema/releases")
        assert "UV_PROJECT_ENVIRONMENT" not in kwargs["env"] and "PYTHONPATH" not in kwargs["env"]
        assert "PYTHONEXECUTABLE" not in kwargs["env"]
        assert command[0] != "git"
    assert (checkout / "CLAUDE.md").read_text() == "незакоммиченная правка"
    assert not (checkout / ".venv").exists()


@pytest.mark.parametrize(
    ("standalone", "installed", "expected"),
    [
        (True, {"AppKit"}, {"menubar", "mcp", "parakeet", "whisper"}),
        (False, {"pystray", "whisperx", "whisperlivekit", "mcp"}, {"menubar", "mcp"}),
    ],
)
def test_app_release_installs_what_its_system_processes_with(
    release, monkeypatch, standalone, installed, expected
):
    """The app updates as ./install.sh client installs it: a Mac processes itself and gets the
    engines, a Linux client sends recordings to the server and drops them. User settings stay."""
    monkeypatch.setattr(base, "STANDALONE", standalone)
    monkeypatch.setattr(
        update.importlib.util, "find_spec", lambda name: object() if name in installed else None
    )
    monkeypatch.setenv("MNEMA_ASR_THREADS", "6")
    monkeypatch.setenv("MNEMA_RUNNER", "https://runner.test")
    update.apply()
    command, kwargs = release["commands"][0]
    assert {command[index + 1] for index, item in enumerate(command) if item == "--extra"} == (
        expected
    )
    assert kwargs["env"]["MNEMA_ASR_THREADS"] == "6"
    assert kwargs["env"]["MNEMA_RUNNER"] == "https://runner.test"


@pytest.mark.parametrize("failure", ["sync", "import mnema.bar.runtime", "install_release"])
def test_failed_preparation_removes_only_incomplete_release(release, tmp_path, failure):
    previous = tmp_path / "data/mnema/releases/0.1.0"
    previous.mkdir(parents=True)
    (previous / "untouched").write_text("old")
    release["failure"] = failure
    with pytest.raises(MediaError):
        update.apply()
    assert [p.name for p in previous.parent.iterdir() if p.is_dir()] == ["0.1.0"]
    assert (previous / "untouched").read_text() == "old"


@pytest.mark.parametrize("damage", ["checksum", "traversal", "version"])
def test_damaged_archive_never_runs_installer(release, tmp_path, damage):
    if damage == "checksum":
        release["sha256"] = "0" * 64
    elif damage == "traversal":
        release["archive"] = release["build_archive"]({"../outside": "escape"})
    else:
        release["version"] = "98.0.0"
    with pytest.raises(MediaError):
        update.apply()
    assert release["commands"] == []
    assert not (tmp_path / "data/mnema/releases/outside").exists()


def test_build_release_matches_manifest_archive_and_lock(tmp_path):
    from scripts.build_release import build

    root = tmp_path / "source"
    (root / "mnema").mkdir(parents=True)
    (root / "mnema/__init__.py").write_text('__version__ = "99.0.0"\n')
    (root / "pyproject.toml").write_text('[project]\nversion="99.0.0"\n')
    (root / "uv.lock").write_text('[[package]]\nname="mnema"\nversion="99.0.0"\n')
    (root / "README.md").write_text("Mnema")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "mnema/__init__.py"], check=True)
    (root / "mnema/.env").write_text("HF_TOKEN=secret\n")  # outside git: never in the archive
    archive, manifest = build(root, tmp_path / "dist", "v99.0.0", "https://releases.test")
    original_archive = archive.read_bytes()
    original_manifest = manifest.read_bytes()
    for path in root.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            os.utime(path, (1_600_000_000, 1_600_000_000))
            path.chmod(0o600)
    rebuilt_archive, rebuilt_manifest = build(
        root, tmp_path / "rebuilt", "v99.0.0", "https://releases.test"
    )
    assert rebuilt_archive.read_bytes() == original_archive
    assert rebuilt_manifest.read_bytes() == original_manifest
    assert original_archive[4:8] == b"\0\0\0\0"  # no build time in the gzip header
    with tarfile.open(archive) as package:
        for member in package.getmembers():
            assert (member.mtime, member.uid, member.gid) == (0, 0, 0)
            assert (member.uname, member.gname) == ("", "")
    info = update.Release.model_validate(json.loads(manifest.read_text()))
    assert info.sha256 == hashlib.sha256(archive.read_bytes()).hexdigest()
    destination = tmp_path / "unpacked"
    destination.mkdir()
    update._unpack(archive, destination, info.version)
    assert (destination / "mnema/__init__.py").read_text() == '__version__ = "99.0.0"\n'
    assert not (destination / "mnema/.env").exists()
    with pytest.raises(ValueError, match="одну версию"):
        build(root, tmp_path / "bad", "v0.4.0", "https://releases.test")


def test_success_keeps_current_process_release_and_removes_only_older_managed_versions(
    release, tmp_path, monkeypatch
):
    root = tmp_path / "data/mnema/releases"
    for name in ("0.0.1-old", "0.1.0-running", "personal"):
        folder = root / name
        folder.mkdir(parents=True)
        if name != "personal":
            (folder / ".mnema-installed.json").write_text("{}")
    monkeypatch.setattr(update.sys, "prefix", str(root / "0.1.0-running/.venv"))
    result = update.apply()
    assert result.changed
    assert not (root / "0.0.1-old").exists()
    assert (root / "0.1.0-running").is_dir() and (root / "personal").is_dir()


def test_update_refuses_without_the_app_and_downloads_nothing(release, monkeypatch):
    """A bare CLI (also inside the server image) without the app: a clear refusal instead of
    building the app."""
    monkeypatch.setattr(update.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(MediaError, match="приложение Mnema, а здесь его нет"):
        update.apply()
    assert release["calls"] == [] and release["commands"] == []


def test_mac_update_without_command_line_tools_stops_before_download(
    release, tmp_path, monkeypatch, caplog
):
    """swift from the Command Line Tools builds Mnema.app: without them the update stops before
    downloading, the notice has no terminal commands (the command is in the log), the previous
    version stays."""
    monkeypatch.setattr(update.sys, "platform", "darwin")
    previous = tmp_path / "data/mnema/releases/0.1.0"
    previous.mkdir(parents=True)
    (previous / ".mnema-installed.json").write_text("{}")
    release["failure"] = "xcode-select"
    with pytest.raises(MediaError) as refused:
        update.apply()
    assert (
        str(refused.value) == "Для обновления нужны Command Line Tools. Подробности в журнале Mnema"
    )
    assert "xcode-select --install" in caplog.text
    assert release["calls"] == ["/latest"]
    assert [command[0] for command, _ in release["commands"]] == ["xcode-select"]
    assert [p.name for p in previous.parent.iterdir() if p.is_dir()] == ["0.1.0"]


def test_terminal_update_without_command_line_tools_names_the_command(release, monkeypatch, capsys):
    """A terminal update without the Command Line Tools names the command in the refusal."""
    monkeypatch.setattr(update.sys, "platform", "darwin")
    release["failure"] = "xcode-select"
    with pytest.raises(SystemExit) as failed:
        update.main()
    assert failed.value.code == 1
    assert capsys.readouterr().err == "Установите Command Line Tools: xcode-select --install\n"
    assert release["calls"] == ["/latest"]


def test_mac_update_tells_why_the_new_app_did_not_build(release, tmp_path, monkeypatch):
    """The new release's panel didn't build: the user gets the app installer's reason, and the
    unfinished release is removed."""
    monkeypatch.setattr(update.sys, "platform", "darwin")
    reason = "Mnema.app не собралась, подробности в bar.log"
    release["failure"], release["stderr"] = "install_release", reason + "\n"
    with pytest.raises(MediaError) as failed:
        update.apply()
    assert str(failed.value) == reason
    assert not [p for p in (tmp_path / "data/mnema/releases").iterdir() if p.is_dir()]


def test_interrupted_attempts_are_removed_but_installed_versions_kept(release, tmp_path):
    """An exit or a crash during preparation must not leave its folder behind for good."""
    root = tmp_path / "data/mnema/releases"
    (root / "99.0.0-abc123/.venv").mkdir(parents=True)
    (root / "0.1.0").mkdir()
    (root / "0.1.0/.mnema-installed.json").write_text("{}")
    (root / "personal").mkdir()
    release["version"] = __version__  # no new version, the cleanup still runs
    update.apply()
    assert sorted(p.name for p in root.iterdir() if p.is_dir()) == ["0.1.0", "personal"]


def test_update_moves_the_mnema_command_to_the_new_release(release, tmp_path):
    """After an app update ~/.local/bin/mnema leads to the new release."""
    link = install_setup.cli_link()
    link.parent.mkdir(parents=True)
    link.symlink_to(tmp_path / "old/.venv/bin/mnema")
    result = update.apply()
    assert Path(os.readlink(link)) == Path(result.python).with_name("mnema")


def test_update_keeps_a_foreign_mnema_command(release):
    """A foreign file where the link goes is left alone; the update still installs."""
    link = install_setup.cli_link()
    link.parent.mkdir(parents=True)
    link.write_text("#!/bin/sh\necho чужой\n")
    assert update.apply().changed
    assert not link.is_symlink() and "чужой" in link.read_text()


def test_github_release_assets_are_followed_to_their_host_over_https(release, monkeypatch):
    """GitHub serves `latest/download/<asset>` as redirects: to the tagged release, then to the
    asset host with a signed URL. Every hop is HTTPS, so the manifest and the archive arrive."""
    releases = "https://github.com/fozt/mnema/releases"
    monkeypatch.setenv("MNEMA_UPDATE_URL", f"{releases}/latest/download/mnema-release.json")
    seen = []

    def github(request):
        url, path = str(request.url), request.url.path
        seen.append(url)
        if request.url.host == "github.com":
            if "/latest/download/" in path:
                tagged = url.replace("/latest/download/", "/download/v99.0.0/")
                return httpx.Response(302, headers={"Location": tagged})
            asset = f"https://release-assets.githubusercontent.com/asset/{path.rsplit('/', 1)[1]}"
            return httpx.Response(302, headers={"Location": asset + "?sp=r&sig=signed"})
        assert request.url.host == "release-assets.githubusercontent.com"
        if path.endswith("/mnema-release.json"):
            manifest = {
                "version": "99.0.0",
                "archive": f"{releases}/download/v99.0.0/mnema-99.0.0.tar.gz",
                "sha256": hashlib.sha256(release["archive"]).hexdigest(),
            }
            return httpx.Response(
                200,
                content=json.dumps(manifest).encode(),
                headers={"Content-Type": "application/octet-stream"},
            )
        return httpx.Response(200, content=release["archive"])

    monkeypatch.setattr(update, "TRANSPORT", httpx.MockTransport(github))
    assert update.apply().version == "99.0.0"
    assert all(url.startswith("https://") for url in seen)
    assert [url for url in seen if "release-assets" in url] == [
        "https://release-assets.githubusercontent.com/asset/mnema-release.json?sp=r&sig=signed",
        "https://release-assets.githubusercontent.com/asset/mnema-99.0.0.tar.gz?sp=r&sig=signed",
    ]


def _refused(request):
    raise httpx.ConnectError("offline", request=request)


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (_refused, "Нет связи с сервером обновлений"),
        (lambda r: httpx.Response(500), "Сервер обновлений ответил ошибкой"),
        (lambda r: httpx.Response(200, json={"version": "latest"}), "Сервер обновлений ответил"),
        (lambda r: httpx.Response(404), "Обновления сейчас недоступны"),
    ],
)
def test_failed_check_says_why(release, monkeypatch, answer, reason):
    monkeypatch.setattr(update, "TRANSPORT", httpx.MockTransport(answer))
    with pytest.raises(MediaError, match=reason):
        update.check()


def _in_chunks(state, count):
    """The archive arrives in pieces with its Content-Length, as from the release host."""
    data = state["archive"]
    size = -(-len(data) // count)
    pieces = [data[i : i + size] for i in range(0, len(data), size)]
    return httpx.Response(200, headers={"content-length": str(len(data))}, content=iter(pieces))


def test_download_reports_its_share_and_the_steps_after_it_have_none(release, monkeypatch):
    handler = update.TRANSPORT.handler

    def chunked(request):
        if request.url.path == "/archive":
            return _in_chunks(release, 4)
        return handler(request)

    monkeypatch.setattr(update, "TRANSPORT", httpx.MockTransport(chunked))
    steps = []

    update.apply(on=lambda share, message: steps.append((share, message)))

    downloading = "Скачиваю Mnema 99.0.0…"
    shares = [share for share, message in steps if message == downloading]
    assert shares[0] is None and shares[-1] == 1.0
    assert shares[1:] == sorted(shares[1:]) and len(shares) == 5  # a share per piece
    after = steps[len(shares) :]
    assert [m for _s, m in after] == ["Готовлю новую версию…", "Устанавливаю Mnema 99.0.0…"]
    assert all(share is None for share, _m in after)


def test_download_without_a_size_has_no_share(release, monkeypatch):
    handler = update.TRANSPORT.handler

    def unsized(request):
        if request.url.path == "/archive":
            return httpx.Response(200, content=iter([release["archive"]]))
        return handler(request)

    monkeypatch.setattr(update, "TRANSPORT", httpx.MockTransport(unsized))
    steps = []

    update.apply(on=lambda share, message: steps.append((share, message)))

    assert steps and all(share is None for share, _m in steps)


def test_terminal_prints_each_step_once(release, monkeypatch, capsys):
    handler = update.TRANSPORT.handler
    monkeypatch.setattr(
        update,
        "TRANSPORT",
        httpx.MockTransport(
            lambda r: _in_chunks(release, 10) if r.url.path == "/archive" else handler(r)
        ),
    )
    monkeypatch.setattr(install_setup, "link_cli", lambda venv: None)

    update.main()

    lines = capsys.readouterr().out.splitlines()
    assert lines[:3] == [
        "Скачиваю Mnema 99.0.0…",
        "Готовлю новую версию…",
        "Устанавливаю Mnema 99.0.0…",
    ]


def test_update_texts_follow_the_interface_language(monkeypatch):
    from mnema.i18n import using

    monkeypatch.setattr(update, "TRANSPORT", httpx.MockTransport(lambda _: httpx.Response(404)))
    with using("en"), pytest.raises(MediaError, match="^Updates are unavailable right now$"):
        update.check()
    with using("ru"), pytest.raises(MediaError, match="^Обновления сейчас недоступны$"):
        update.check()
