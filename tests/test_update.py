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
from mnema import config as mnema_config
from mnema.core import base
from mnema.core.base import MediaError


@pytest.fixture
def release(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("MNEMA_SYSTEMD_DIR", str(tmp_path / "systemd"))
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
    state = {"version": "0.3.0", "calls": [], "commands": [], "failure": ""}

    def archive(extra=None):
        files = {
            "pyproject.toml": (
                '[project]\nname="mnema"\nversion="0.3.0"\n'
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
        if request.url.path == "/api/capabilities":
            state.setdefault("readiness", []).append(request)
            return httpx.Response(
                200,
                json={"ok": True, "llm": True, "name": "node"},
            )
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
    assert found.any and found.text() == "Mnema 0.3.0"
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
                    version="0.3.0", archive="https://release.test/archive", sha256="0" * 64
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
    assert result.version == "0.3.0" and result.changed
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
        release["version"] = "0.4.0"
    with pytest.raises(MediaError):
        update.apply()
    assert release["commands"] == []
    assert not (tmp_path / "data/mnema/releases/outside").exists()


def test_build_release_matches_manifest_archive_and_lock(tmp_path):
    from scripts.build_release import build

    root = tmp_path / "source"
    (root / "mnema").mkdir(parents=True)
    (root / "mnema/__init__.py").write_text('__version__ = "0.3.0"\n')
    (root / "pyproject.toml").write_text('[project]\nversion="0.3.0"\n')
    (root / "uv.lock").write_text('[[package]]\nname="mnema"\nversion="0.3.0"\n')
    (root / "README.md").write_text("Mnema")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "mnema/__init__.py"], check=True)
    (root / "mnema/.env").write_text("HF_TOKEN=secret\n")  # outside git: never in the archive
    archive, manifest = build(root, tmp_path / "dist", "v0.3.0", "https://releases.test")
    original_archive = archive.read_bytes()
    original_manifest = manifest.read_bytes()
    for path in root.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            os.utime(path, (1_600_000_000, 1_600_000_000))
            path.chmod(0o600)
    rebuilt_archive, rebuilt_manifest = build(
        root, tmp_path / "rebuilt", "v0.3.0", "https://releases.test"
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
    assert (destination / "mnema/__init__.py").read_text() == '__version__ = "0.3.0"\n'
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
    """A server or a bare CLI without the app: a clear refusal instead of building the app."""
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
    (root / "0.3.0-abc123/.venv").mkdir(parents=True)
    (root / "0.1.0").mkdir()
    (root / "0.1.0/.mnema-installed.json").write_text("{}")
    (root / "personal").mkdir()
    release["version"] = __version__  # no new version, the cleanup still runs
    update.apply()
    assert sorted(p.name for p in root.iterdir() if p.is_dir()) == ["0.1.0", "personal"]


@pytest.fixture
def server_install(release, tmp_path, monkeypatch):
    import pwd

    monkeypatch.setattr(update.sys, "platform", "linux")  # systemd is faked, like _service
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    config = mnema_config.config_dir()
    config.mkdir(parents=True)
    owner = pwd.getpwuid(os.geteuid()).pw_name
    repo = tmp_path / 'checkout spaces % $ "quote"'
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nversion="0.2.0"\n')
    root = tmp_path / "custom recordings % $"
    root.mkdir()
    (root / "audio.wav").write_bytes(b"original audio")
    (config / "server.json").write_text(json.dumps({"port": 4321, "url": "http://custom:4321"}))
    (config / "server.env").write_text('MNEMA_TOKEN="unchanged-secret"\n')
    (config / ".env").write_text(
        "MNEMA_ASR_THREADS=auto\nMNEMA_MODEL=provider/model\nMNEMA_API_KEY=cloud-secret\nMNEMA_LIBRARY=true\n"
    )
    for path in config.iterdir():
        path.chmod(0o600)
    command = [
        str(repo / ".venv/bin/mnema"),
        "serve",
        "--host",
        "0.0.0.0",
        "--saved-port",
        "--dir",
        str(root),
    ]
    unit = install_setup.server_service_path()
    unit.parent.mkdir()
    original = install_setup._unit(repo, owner, command, config / "server.env")
    original = original.replace("Restart=on-failure", "TimeoutStopSec=42\nRestart=on-failure")
    unit.write_text(original)
    before = {p.name: (p.read_bytes(), p.stat().st_mode) for p in config.iterdir()}
    activation = []
    fail = []

    def service(name, content, directory):
        assert name == "mnema" and directory == unit.parent
        unit.write_text(content)
        activation.append(content)
        if fail and fail.pop(0):
            raise subprocess.CalledProcessError(1, ["systemctl", "restart", "mnema"])

    def forbidden(*args, **kwargs):
        pytest.fail("update must not configure identity/settings or issue invitations")

    monkeypatch.setattr(install_setup, "_service", service)
    monkeypatch.setattr(install_setup, "configure_server", forbidden)
    monkeypatch.setattr(install_setup, "server_identity", forbidden)
    monkeypatch.setattr(install_setup, "issue_invite", forbidden)
    return {
        "repo": repo,
        "root": root,
        "unit": unit,
        "original": original,
        "owner": owner,
        "config": config,
        "before": before,
        "activation": activation,
        "fail": fail,
    }


def unchanged_server_config(server_install):
    config = server_install["config"]
    assert {p.name: (p.read_bytes(), p.stat().st_mode) for p in config.iterdir()} == server_install[
        "before"
    ]
    assert (server_install["root"] / "audio.wav").read_bytes() == b"original audio"


def test_server_update_preserves_custom_service_settings_and_identity(release, server_install):
    result = update.apply(server=True)
    target = Path(result.python).parent.parent.parent
    installed = install_setup.installed_server()
    assert installed.repo == target and installed.user == server_install["owner"]
    assert installed.content == install_setup.InstalledServer(
        server_install["unit"],
        server_install["original"],
        server_install["repo"],
        server_install["owner"],
    ).retarget(target)
    assert install_setup._unit_value(installed.content, "ExecStart").endswith(
        install_setup._unit_value(server_install["original"], "ExecStart").split(' "serve"', 1)[1]
    )
    assert len(server_install["activation"]) == 1
    extras = release["commands"][0][0]
    assert "full" in extras and "live" in extras and "menubar" not in extras
    assert release["commands"][1][0][-1] == "import mnema.server"
    assert not any(
        "install_release" in str(command) or command[0] == "git"
        for command, _ in release["commands"]
    )
    unchanged_server_config(server_install)


def test_server_repeated_update_reads_installed_version_not_old_cli(release, server_install):
    assert update.apply(server=True).changed
    release["commands"].clear()
    release["calls"].clear()
    result = update.apply(server=True)
    assert not result.changed and result.version == "0.3.0"
    assert len(server_install["activation"]) == 1
    assert release["calls"] == ["/latest"] and not release["commands"]
    unchanged_server_config(server_install)


@pytest.mark.parametrize("failure", ["sync", "import mnema.server"])
def test_server_preparation_failure_does_not_touch_service(release, server_install, failure):
    release["failure"] = failure
    with pytest.raises(MediaError, match="новую версию|Новая версия"):
        update.apply(server=True)
    assert not server_install["activation"]
    assert server_install["unit"].read_text() == server_install["original"]
    assert not list(update._release_root().glob("0.3.0-*"))
    unchanged_server_config(server_install)


def test_server_activation_failure_restores_original_unit(release, server_install):
    server_install["fail"].extend([True, False])
    with pytest.raises(MediaError, match="прежняя версия восстановлена"):
        update.apply(server=True)
    assert len(server_install["activation"]) == 2
    assert server_install["unit"].read_text() == server_install["original"]
    assert not list(update._release_root().glob("0.3.0-*"))
    assert server_install["repo"].is_dir()
    unchanged_server_config(server_install)


@pytest.fixture(scope="module")
def built_release(tmp_path_factory) -> Path:
    """A release archive of this checkout, built before release fakes subprocess (needs git)."""
    from scripts.build_release import build

    repo = Path(__file__).resolve().parents[1]
    dist = tmp_path_factory.mktemp("dist")
    return build(repo, dist, f"v{__version__}", "https://releases.test")[0]


def test_server_starts_the_launcher_of_the_built_release(built_release, release, server_install):
    """A real release archive: the service starts the console script uv installs from the
    archive's pyproject, not a checkout file; if it doesn't start, the previous unit stays."""
    release["archive"], release["version"] = built_release.read_bytes(), __version__

    server_install["fail"].append(True)
    with pytest.raises(MediaError, match="прежняя версия восстановлена"):
        update.apply(server=True)
    assert server_install["unit"].read_text() == server_install["original"]

    result = update.apply(server=True)
    target = Path(result.python).parent.parent.parent
    launcher, *args = install_setup._unit_value(
        install_setup.installed_server().content, "ExecStart"
    ).split(" ")
    assert launcher == f'"{target}/.venv/bin/mnema"' and args[0] == '"serve"'
    assert (target / ".venv/bin/mnema").is_file()
    project = tomllib.loads((target / "pyproject.toml").read_text())["project"]
    module, function = project["scripts"]["mnema"].split(":")
    assert f"def {function}(" in (target / module.replace(".", "/") / "__init__.py").read_text()
    unchanged_server_config(server_install)


def test_server_release_without_launcher_does_not_touch_service(release, server_install):
    release["archive"] = release["build_archive"](
        {"pyproject.toml": '[project]\nname="mnema"\nversion="0.3.0"\n'}
    )
    with pytest.raises(MediaError, match="проверку запуска"):
        update.apply(server=True)
    assert not server_install["activation"]
    assert server_install["unit"].read_text() == server_install["original"]
    assert not list(update._release_root().glob("0.3.0-*"))


def test_server_failed_rollback_keeps_both_releases(release, server_install):
    server_install["fail"].extend([True, True])
    with pytest.raises(MediaError, match="Обе версии сохранены"):
        update.apply(server=True)
    assert server_install["repo"].is_dir()
    assert len(list(update._release_root().glob("0.3.0-*"))) == 1
    unchanged_server_config(server_install)


def test_server_update_refuses_other_service_owner_before_network(
    release, server_install, monkeypatch
):
    import pwd
    from types import SimpleNamespace

    monkeypatch.setattr(pwd, "getpwnam", lambda user: SimpleNamespace(pw_uid=os.geteuid() + 1))
    with pytest.raises(MediaError, match="пользователя службы"):
        update.apply(server=True)
    assert not release["calls"] and not release["commands"] and not server_install["activation"]
    unchanged_server_config(server_install)


def test_server_changed_unit_is_not_overwritten(release, server_install, monkeypatch):
    run = update._run

    def edit(*args, **kwargs):
        result = run(*args, **kwargs)
        if "import mnema.server" in args:
            server_install["unit"].write_text(
                server_install["original"] + "# changed during update\n"
            )
        return result

    monkeypatch.setattr(update, "_run", edit)
    with pytest.raises(MediaError, match="изменились во время обновления"):
        update.apply(server=True)
    assert not server_install["activation"]
    assert server_install["unit"].read_text().endswith("# changed during update\n")
    unchanged_server_config(server_install)


@pytest.mark.parametrize("marker", [False, True])
def test_desktop_cleanup_preserves_release_referenced_by_server(release, server_install, marker):
    root = update._release_root()
    service_repo = root / "0.2.0-server"
    service_repo.mkdir(parents=True)
    if marker:
        (service_repo / ".mnema-installed.json").write_text("{}")
    installed = install_setup.installed_server()
    server_install["unit"].write_text(installed.retarget(service_repo))
    assert update.apply().changed
    assert service_repo.is_dir()


def test_server_prune_keeps_previous_service_release_and_current_python(
    release, server_install, monkeypatch
):
    root = update._release_root()
    old_server = root / "0.2.0-server"
    old_server.mkdir(parents=True)
    (old_server / ".mnema-installed.json").write_text("{}")
    (old_server / "pyproject.toml").write_text('[project]\nversion="0.2.0"\n')
    installed = install_setup.installed_server()
    server_install["unit"].write_text(installed.retarget(old_server))
    current = root / "0.1.0-desktop"
    current.mkdir()
    (current / ".mnema-installed.json").write_text("{}")
    monkeypatch.setattr(update.sys, "prefix", str(current / ".venv"))
    assert update.apply(server=True).changed
    assert old_server.is_dir() and current.is_dir()


def test_server_update_uses_common_lock(release, server_install):
    import fcntl

    root = update._release_root()
    root.mkdir(parents=True)
    with (root / ".update.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(MediaError, match="уже идёт"):
            update.apply(server=True)
    assert not release["calls"] and not server_install["activation"]


def test_server_readiness_uses_saved_service_address_token_and_retries(
    release, server_install, monkeypatch
):
    previous = update.TRANSPORT
    attempts = []

    def handler(request):
        if request.url.path == "/api/capabilities":
            attempts.append(request)
            if len(attempts) == 1:
                return httpx.Response(503)
        return previous.handle_request(request)

    monkeypatch.setattr(update, "TRANSPORT", httpx.MockTransport(handler))
    assert update.apply(server=True).changed
    assert len(attempts) == 2
    assert all(str(r.url) == "http://127.0.0.1:4321/api/capabilities" for r in attempts)
    assert all(r.headers["authorization"] == "Bearer unchanged-secret" for r in attempts)
    unchanged_server_config(server_install)


@pytest.mark.parametrize("invalid", [404, 401, {"ok": True, "name": "Сервер"}])
def test_server_false_readiness_rolls_back_instead_of_reporting_success(
    release, server_install, monkeypatch, invalid
):
    previous = update.TRANSPORT
    monkeypatch.setattr(update, "_SERVER_READY_TIMEOUT", 0.02)
    checked = []

    def handler(request):
        if request.url.path == "/api/capabilities" and len(server_install["activation"]) == 1:
            checked.append(request)
            return (
                httpx.Response(invalid)
                if isinstance(invalid, int)
                else httpx.Response(200, json=invalid)
            )
        return previous.handle_request(request)

    monkeypatch.setattr(update, "TRANSPORT", httpx.MockTransport(handler))
    with pytest.raises(MediaError, match="прежняя версия восстановлена"):
        update.apply(server=True)
    assert checked and len(server_install["activation"]) == 2
    assert server_install["unit"].read_text() == server_install["original"]
    assert not list(update._release_root().glob("0.3.0-*"))


def test_server_timeout_and_failed_rollback_preserve_candidate(
    release, server_install, monkeypatch
):
    previous = update.TRANSPORT
    monkeypatch.setattr(update, "_SERVER_READY_TIMEOUT", 0.02)

    def handler(request):
        if request.url.path == "/api/capabilities":
            raise httpx.ConnectError("not ready")
        return previous.handle_request(request)

    monkeypatch.setattr(update, "TRANSPORT", httpx.MockTransport(handler))
    with pytest.raises(MediaError, match="Обе версии сохранены"):
        update.apply(server=True)
    assert len(server_install["activation"]) == 2
    assert len(list(update._release_root().glob("0.3.0-*"))) == 1
    unchanged_server_config(server_install)


@pytest.mark.parametrize(
    "host, expected",
    [
        ("0.0.0.0", "127.0.0.1"),
        ("::", "[::1]"),
        ("192.0.2.7", "192.0.2.7"),
        ("::1", "[::1]"),
    ],
)
def test_installed_service_connection_uses_actual_bind_and_custom_env(
    server_install, monkeypatch, host, expected
):
    env = server_install["config"].parent / "custom environment %.env"
    env.write_text('MNEMA_TOKEN="actual-service-token"\n')
    content = server_install["original"].replace('"--host" "0.0.0.0"', f'"--host" "{host}"')
    content = content.replace(
        install_setup._unit_value(content, "EnvironmentFile"), install_setup._scalar(str(env))
    )
    server_install["unit"].write_text(content)
    monkeypatch.setenv("MNEMA_TOKEN", "unrelated-client-token")
    monkeypatch.setenv("MNEMA_RUNNER", "https://unrelated-node")
    connection = install_setup.installed_server().connection()
    assert connection.url == f"http://{expected}:4321"
    assert connection.token == "actual-service-token"
    unchanged_server_config(server_install)


def test_unsupported_service_command_refused_before_network(release, server_install):
    content = server_install["original"].replace('"serve"', '"unknown-command"')
    server_install["unit"].write_text(content)
    with pytest.raises(MediaError, match="прочитать установленный сервер"):
        update.apply(server=True)
    assert not release["calls"] and not server_install["activation"]
    assert server_install["unit"].read_text() == content


def test_update_moves_the_mnema_command_to_the_new_release(release, tmp_path):
    """After an app update ~/.local/bin/mnema leads to the new release."""
    link = install_setup.cli_link()
    link.parent.mkdir(parents=True)
    link.symlink_to(tmp_path / "old/.venv/bin/mnema")
    result = update.apply()
    assert Path(os.readlink(link)) == Path(result.python).with_name("mnema")


def test_server_update_moves_the_command_only_after_the_service_switched(release, server_install):
    """The link moves to the new server release only once the service is up."""
    link = install_setup.cli_link()
    link.parent.mkdir(parents=True)
    old = server_install["repo"] / ".venv/bin/mnema"
    link.symlink_to(old)
    server_install["fail"].extend([True, False])
    with pytest.raises(MediaError):
        update.apply(server=True)
    assert Path(os.readlink(link)) == old
    result = update.apply(server=True)
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
                tagged = url.replace("/latest/download/", "/download/v0.3.0/")
                return httpx.Response(302, headers={"Location": tagged})
            asset = f"https://release-assets.githubusercontent.com/asset/{path.rsplit('/', 1)[1]}"
            return httpx.Response(302, headers={"Location": asset + "?sp=r&sig=signed"})
        assert request.url.host == "release-assets.githubusercontent.com"
        if path.endswith("/mnema-release.json"):
            manifest = {
                "version": "0.3.0",
                "archive": f"{releases}/download/v0.3.0/mnema-0.3.0.tar.gz",
                "sha256": hashlib.sha256(release["archive"]).hexdigest(),
            }
            return httpx.Response(
                200,
                content=json.dumps(manifest).encode(),
                headers={"Content-Type": "application/octet-stream"},
            )
        return httpx.Response(200, content=release["archive"])

    monkeypatch.setattr(update, "TRANSPORT", httpx.MockTransport(github))
    assert update.apply().version == "0.3.0"
    assert all(url.startswith("https://") for url in seen)
    assert [url for url in seen if "release-assets" in url] == [
        "https://release-assets.githubusercontent.com/asset/mnema-release.json?sp=r&sig=signed",
        "https://release-assets.githubusercontent.com/asset/mnema-0.3.0.tar.gz?sp=r&sig=signed",
    ]


def test_update_texts_follow_the_interface_language(monkeypatch):
    from mnema.i18n import using

    monkeypatch.setattr(update, "TRANSPORT", httpx.MockTransport(lambda _: httpx.Response(404)))
    with using("en"), pytest.raises(MediaError, match="^Updates are unavailable right now$"):
        update.check()
    with using("ru"), pytest.raises(MediaError, match="^Обновления сейчас недоступны$"):
        update.check()
