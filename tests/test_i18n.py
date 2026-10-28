"""Interface language: catalog coverage over the source and the behaviour of _ / _n / using."""

from __future__ import annotations

import ast
import json
import os
import string
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import WebSocket
from fastapi.testclient import TestClient
from pydantic import BaseModel

from mnema import core, i18n, ops
from mnema.i18n import _, _n, current, from_accept_language, month_short, using

SOURCE = Path(i18n.__file__).resolve().parents[1]
CALLS = {"_", "_n"}


def _fields(text: str) -> set[str]:
    return {name for _lit, name, _spec, _conv in string.Formatter().parse(text) if name}


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name) and func.id in CALLS:
        return func.id
    if isinstance(func, ast.Attribute) and func.attr in CALLS:
        if isinstance(func.value, ast.Name) and func.value.id == "i18n":
            return func.attr
    return None


def _modules() -> list[tuple[Path, ast.Module]]:
    return [(p, ast.parse(p.read_text(encoding="utf-8"))) for p in sorted(SOURCE.rglob("*.py"))]


def _str_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level NAME = "literal": `_(NAME)` is checked like a literal."""
    found: dict[str, str] = {}
    for stmt in tree.body:
        targets = (
            stmt.targets
            if isinstance(stmt, ast.Assign)
            else [stmt.target]
            if isinstance(stmt, ast.AnnAssign) and stmt.value is not None
            else []
        )
        value = getattr(stmt, "value", None)
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            for target in targets:
                if isinstance(target, ast.Name):
                    found[target.id] = value.value
    return found


def _shared_constants(modules: list[tuple[Path, ast.Module]]) -> dict[str, str]:
    """String constants of every module by name, for `_(other.NAME)` and imported NAME;
    a name two modules give different values is left out."""
    found: dict[str, str] = {}
    clash: set[str] = set()
    for _path, tree in modules:
        for name, value in _str_constants(tree).items():
            if found.setdefault(name, value) != value:
                clash.add(name)
    return {name: value for name, value in found.items() if name not in clash}


def _translated() -> list[tuple[str, int, str, ast.Call, list[str]]]:
    """Every _ / _n call: file, line, function name, the call, its literal source strings."""
    calls = []
    modules = _modules()
    shared = _shared_constants(modules)
    for path, tree in modules:
        constants = {**shared, **_str_constants(tree)}
        rel = str(path.relative_to(SOURCE.parent))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or (name := _call_name(node)) is None:
                continue
            texts = []
            for arg in node.args[: 2 if name == "_n" else 1]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    texts.append(arg.value)
                elif isinstance(arg, ast.Name) and arg.id in constants:
                    texts.append(constants[arg.id])
                elif isinstance(arg, ast.Attribute) and arg.attr in shared:
                    texts.append(shared[arg.attr])
            calls.append((rel, node.lineno, name, node, texts))
    return calls


def test_every_source_string_has_a_russian_entry_with_the_same_placeholders() -> None:
    catalog = i18n.catalog()
    problems = []
    for rel, line, name, _node, texts in _translated():
        if not texts:
            continue
        where = f"{rel}:{line}"
        value = catalog.get(texts[0])
        if name == "_":
            if not isinstance(value, str):
                problems.append(f"{where}: no ru entry for {texts[0]!r}")
            elif _fields(value) != _fields(texts[0]):
                problems.append(f"{where}: placeholders differ in {texts[0]!r}")
            continue
        if not isinstance(value, list) or len(value) != 3:
            problems.append(f"{where}: ru entry for {texts[0]!r} needs [one, few, many]")
            continue
        english = set().union(*(_fields(t) for t in texts)) - {"n"}
        for form in value:
            if _fields(form) - {"n"} != english:
                problems.append(f"{where}: placeholders differ in {form!r}")
    assert not problems, "\n".join(problems)


def test_every_russian_entry_is_used_by_the_source() -> None:
    literals = {
        node.value
        for _path, tree in _modules()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    unused = sorted(set(i18n.catalog()) - literals)
    assert not unused, f"ru entries nobody uses: {unused}"


def test_source_strings_are_plain_literals() -> None:
    """A formatted or glued source string never matches a catalog key."""
    bad = []
    for rel, line, name, node, _texts in _translated():
        for arg in node.args[: 2 if name == "_n" else 1]:
            glued = isinstance(arg, ast.JoinedStr | ast.BinOp) or (
                isinstance(arg, ast.Call)
                and isinstance(arg.func, ast.Attribute)
                and arg.func.attr in ("format", "join")
            )
            if glued:
                bad.append(f"{rel}:{line}")
    assert not bad, "f-string or concatenation inside _()/_n(): " + ", ".join(bad)


def test_modules_that_translate_never_rebind_the_underscore() -> None:
    """`for _ in ...` or `_, x = ...` makes `_` local to the function: `_("...")` there fails."""
    bad = []
    for path, tree in _modules():
        imported = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("i18n")
            for alias in node.names
        } & CALLS
        if not imported:
            continue
        for node in ast.walk(tree):
            names = (
                [node.id]
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
                else [
                    a.arg for a in [*node.args.args, *node.args.kwonlyargs, *node.args.posonlyargs]
                ]
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda)
                else []
            )
            if imported & set(names):
                bad.append(f"{path.relative_to(SOURCE.parent)}:{node.lineno}")
    assert not bad, "_ / _n rebound where they translate: " + ", ".join(bad)


class _ImportTime(ast.NodeVisitor):
    """_() evaluated while a module is imported: module level, class bodies, default arguments,
    decorators — everything outside a function or lambda body."""

    def __init__(self) -> None:
        self.found: list[int] = []

    def visit_Call(self, node: ast.Call) -> None:
        if _call_name(node):
            self.found.append(node.lineno)
        self.generic_visit(node)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for item in [*node.decorator_list, *node.args.defaults, *node.args.kw_defaults]:
            if item is not None:
                self.visit(item)

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for item in [*node.args.defaults, *node.args.kw_defaults]:
            if item is not None:
                self.visit(item)


def test_nothing_is_translated_at_import_time() -> None:
    """The server picks the language per request: a text translated on import is frozen."""
    found = []
    for path, tree in _modules():
        visitor = _ImportTime()
        visitor.visit(tree)
        found += [f"{path.relative_to(SOURCE.parent)}:{line}" for line in visitor.found]
    assert not found, "_()/_n() at import time: " + ", ".join(found)


@pytest.fixture
def ru_catalog(monkeypatch, tmp_path):
    """A catalog of its own: the tests do not depend on what the areas translated."""
    folder = tmp_path / "ru"
    folder.mkdir()
    (folder / "a.json").write_text(
        json.dumps(
            {
                "Start recording": "Начать запись",
                "Recording {name}": "Запись {name}",
                "{n} minute": ["{n} минута", "{n} минуты", "{n} минут"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (folder / "b.json").write_text(
        json.dumps({"Start recording": "Начать запись"}, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(i18n, "_CATALOG_DIR", folder)
    i18n.catalog.cache_clear()
    yield folder
    i18n.catalog.cache_clear()


def test_russian_comes_from_the_catalog_and_english_is_the_source(ru_catalog) -> None:
    assert current() == "ru"  # conftest
    assert _("Start recording") == "Начать запись"
    assert _("Recording {name}", name="Звонок") == "Запись Звонок"
    assert _("Not in the catalog {x}", x=1) == "Not in the catalog 1"
    assert _("Braces {stay} without arguments") == "Braces {stay} without arguments"
    with using("en"):
        assert _("Start recording") == "Start recording"
        assert _("Recording {name}", name="Call") == "Recording Call"


def test_catalog_files_must_agree_on_a_shared_key(ru_catalog) -> None:
    (ru_catalog / "c.json").write_text(
        json.dumps({"Start recording": "Старт"}, ensure_ascii=False), encoding="utf-8"
    )
    i18n.catalog.cache_clear()
    with pytest.raises(ValueError, match="Start recording"):
        i18n.catalog()


@pytest.mark.parametrize(
    ("n", "russian"),
    [
        (0, "0 минут"),
        (1, "1 минута"),
        (2, "2 минуты"),
        (4, "4 минуты"),
        (5, "5 минут"),
        (11, "11 минут"),
        (12, "12 минут"),
        (14, "14 минут"),
        (21, "21 минута"),
        (22, "22 минуты"),
        (111, "111 минут"),
        (112, "112 минут"),
    ],
)
def test_russian_plural_has_three_forms(ru_catalog, n, russian) -> None:
    assert _n("{n} minute", "{n} minutes", n) == russian


def test_english_plural_and_missing_russian_entry(ru_catalog) -> None:
    with using("en"):
        assert _n("{n} minute", "{n} minutes", 1) == "1 minute"
        assert _n("{n} minute", "{n} minutes", 11) == "11 minutes"
    assert _n("{n} file in {where}", "{n} files in {where}", 3, where="x") == "3 files in x"


def test_using_nests_restores_and_stays_in_its_thread() -> None:
    i18n.set_language("en")
    seen = []
    with using("ru-RU"):
        assert current() == "ru"
        with using("en"):
            assert current() == "en"
        assert current() == "ru"
        worker = threading.Thread(target=lambda: seen.append(current()))
        worker.start()
        worker.join()
    assert current() == "en"
    assert seen == ["en"]  # a new thread starts in the process language


@pytest.mark.parametrize(
    ("env", "lang"),
    [
        ({"MNEMA_UI_LANG": "ru", "LANG": "en_US.UTF-8"}, "ru"),
        ({"MNEMA_UI_LANG": "en", "LANG": "ru_RU.UTF-8"}, "en"),
        ({"LC_ALL": "ru_RU.UTF-8", "LANG": "en_US.UTF-8"}, "ru"),
        ({"LC_MESSAGES": "ru_UA.UTF-8"}, "ru"),
        ({"LANG": "de_DE.UTF-8"}, "en"),
        ({"LANG": "C.UTF-8"}, "en"),
        ({}, "en"),
    ],
)
def test_detect_reads_the_system_language(monkeypatch, env, lang) -> None:
    for name in ("MNEMA_UI_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert i18n.detect() == lang


@pytest.mark.parametrize(
    ("header", "lang"),
    [
        ("ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7", "ru"),
        ("en-US,en;q=0.9,ru;q=0.8", "en"),
        ("de-DE,de;q=0.9", "en"),
        ("de,ru;q=0.5", "ru"),
        ("en;q=0.3, ru;q=0.6", "ru"),
        ("ru;q=0", "en"),
        ("RU", "ru"),
        ("*", "en"),
    ],
)
def test_accept_language_picks_the_preferred_supported_language(header, lang) -> None:
    assert from_accept_language(header) == lang


def test_no_accept_language_is_the_process_language() -> None:
    assert from_accept_language(None) == "ru"
    i18n.set_language("en")
    assert from_accept_language("") == "en"


def test_short_month_follows_the_language() -> None:
    assert month_short(10) == "окт." and month_short(5) == "мая"
    with using("en"):
        assert month_short(10) == "Oct" and month_short(5) == "May"


class _Params(BaseModel):
    model_config = {"extra": "forbid"}


@pytest.fixture
def server(tmp_path, monkeypatch):
    from mnema.server import create_app

    monkeypatch.setattr(core, "probe", lambda path: SimpleNamespace(duration=10.0))
    app = create_app(tmp_path)
    app.state.operations["lang"] = ops.OpSpec(
        name="lang",
        fn=lambda path, params, on: {"lang": current()},
        params=_Params,
        input_kind=ops.InputKind.MEDIA,
        output_kind=ops.OutputKind.JSON,
    )

    @app.get("/api/test-lang")
    def lang_of_request() -> dict:
        return {"lang": current()}

    @app.websocket("/api/test-lang-ws")
    async def lang_of_socket(ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_json({"lang": current()})
        await ws.close()

    (tmp_path / "talk.wav").write_bytes(b"audio")
    return SimpleNamespace(app=app, client=TestClient(app), root=tmp_path)


def _finished(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.01)
    pytest.fail("the job did not finish")


@pytest.mark.parametrize(("header", "lang"), [("en-US,en;q=0.9", "en"), ("ru", "ru")])
def test_job_runs_in_the_language_of_the_client_that_queued_it(server, header, lang) -> None:
    response = server.client.post(
        "/api/jobs",
        json={"op": "lang", "path": str(server.root / "talk.wav")},
        headers={"Accept-Language": header},
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["id"]
    # polled without a header: the job keeps the language it was queued in
    assert _finished(server.client, job_id)["result"] == {"lang": lang}
    from mnema.store import SqliteJobStore

    reopened = SqliteJobStore(server.root / ".mnema" / "jobs.db").get(job_id)
    assert reopened is not None and reopened.lang == lang


def test_request_and_live_handshake_use_accept_language(server) -> None:
    client = server.client
    assert client.get("/api/test-lang", headers={"Accept-Language": "en"}).json()["lang"] == "en"
    assert client.get("/api/test-lang").json()["lang"] == "ru"  # no header: the server language
    with client.websocket_connect("/api/test-lang-ws", headers={"Accept-Language": "en"}) as ws:
        assert ws.receive_json() == {"lang": "en"}


def test_client_sends_its_language(monkeypatch) -> None:
    import httpx

    from mnema.client import MnemaClient

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("accept-language"))
        return httpx.Response(200, json={"health": "ok"})

    client = MnemaClient("http://node", transport=httpx.MockTransport(handler))
    client.probe()
    with using("en"):
        client.probe()
    assert seen == ["ru", "en"]


_LANG_WORKER = r"""
import os
from mnema.core.worker import emit, isolate_stdout
isolate_stdout()
emit({'type': 'result', 'value': os.environ.get('MNEMA_UI_LANG')})
"""


def test_worker_process_gets_the_language_of_its_job(monkeypatch, tmp_path) -> None:
    from mnema.core import worker as w

    (tmp_path / "lang_worker.py").write_text(_LANG_WORKER)
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join(filter(None, (str(tmp_path), os.getenv("PYTHONPATH"))))
    )
    with using("en"), w.Worker("lang_worker") as child:
        deadline = time.monotonic() + 30
        event = None
        while event is None and time.monotonic() < deadline:
            event = child.receive()
    assert event == {"type": "result", "value": "en"}
