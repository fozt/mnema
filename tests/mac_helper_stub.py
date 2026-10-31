"""A stand-in for mnema-asr: a real process that speaks the protocol of the shared fixture
(tests/fixtures/asr-helper-v1.json). Its behaviour is a JSON file next to it (what is
installed, which operation fails with which code or gives which result, the protocol version);
every request is logged there with the helper's environment and parent pid. A request whose
keys or value types differ from every fixture request of its operation gets "protocol", as
the real helper answers a request it cannot read.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "asr-helper-v1.json"

_SCRIPT = r"""#!{python}
import json, os, sys, time
from pathlib import Path

here = Path(__file__)
config_file, log = here.with_name("helper.json"), here.with_name("helper.log")
contract = json.loads(Path({fixture!r}).read_text(encoding="utf-8"))
replies = contract["replies"]


def shape(value):  # keys and value types, nested: what the fixture fixes about a request
    if isinstance(value, dict):
        return {{key: shape(item) for key, item in value.items()}}
    if isinstance(value, list):
        return [shape(item) for item in value[:1]]
    return type(value).__name__


known = [shape(r) for r in contract["requests"].values()]


def config():
    return json.loads(config_file.read_text(encoding="utf-8"))


def send(reply):
    sys.stdout.write(json.dumps({{**reply, "v": config().get("v", 1)}}) + "\n")
    sys.stdout.flush()


def progress(stage, fraction):
    send({{"type": "progress", "stage": stage, "fraction": fraction}})


def error(code):
    send({{"type": "error", "code": code, "detail": "stub"}})


for line in sys.stdin:
    request = json.loads(line)
    entry = {{"request": request, "env": sorted(os.environ)}}
    entry.update(ppid=os.getppid(), pid=os.getpid())
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    c = config()
    if shape(request) not in known:
        error("protocol")
        continue
    operation, payload = request["operation"], request["payload"]
    if operation in c.get("fail", {{}}):
        error(c["fail"][operation])
    elif operation in c.get("result", {{}}):  # a result of the test's own shape
        send({{"type": "result", "value": c["result"][operation]}})
    elif operation == "models":
        models = [dict(m, installed=c["installed"].get(m["id"], False))
                  for m in replies["models"]["value"]]
        send({{"type": "result", "value": models}})
    elif operation == "download":
        progress("download", 0.25)
        if c.get("hang"):
            time.sleep(300)
        while c.get("wait_for") and not Path(c["wait_for"]).exists():  # the test lets it go on
            time.sleep(0.05)
        progress("load", None)
        c["installed"][payload["id"]] = True
        if payload["id"] == "parakeet":
            c["installed"]["vad"] = True
        config_file.write_text(json.dumps(c), encoding="utf-8")
        send(replies["download"])
    elif operation == "transcribe":
        if not c["installed"].get(payload["id"]):
            error("no_model")
            continue
        progress("load", None)
        progress("recognize", 0.5)
        known = replies["transcribe"]["value"]["tracks"]
        tracks = [known[i] if i < len(known) else {{"words": []}}
                  for i in range(len(payload["wavs"]))]
        send({{"type": "result", "value": {{"tracks": tracks}}}})
    elif operation == "speech":
        if not c["installed"].get("vad"):
            error("no_model")
            continue
        progress("recognize", 1.0)
        segments = c.get("speech", replies["speech"]["value"]["segments"])
        send({{"type": "result", "value": {{"segments": segments}}}})
"""


class Helper:
    """The stub's files: the program, its behaviour and the log of what it was asked."""

    def __init__(self, folder: Path, **config: object) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / "mnema-asr"
        self.path.write_text(
            _SCRIPT.format(python=sys.executable, fixture=str(FIXTURE)), encoding="utf-8"
        )
        self.path.chmod(0o755)
        self._config = folder / "helper.json"
        self._log = folder / "helper.log"
        self.configure(installed={"parakeet": True, "vad": True}, **config)

    def configure(self, **changes: object) -> None:
        current = self.config() if self._config.exists() else {}
        self._config.write_text(json.dumps({**current, **changes}), encoding="utf-8")

    def config(self) -> dict:
        return json.loads(self._config.read_text(encoding="utf-8"))

    def calls(self) -> list[dict]:
        if not self._log.exists():
            return []
        return [json.loads(line) for line in self._log.read_text(encoding="utf-8").splitlines()]

    def operations(self) -> list[str]:
        return [c["request"]["operation"] for c in self.calls()]


def install(monkeypatch, folder: Path, **config: object) -> Helper:
    """Mnema.app on an Apple silicon Mac: its mnema-asr is the stub (MNEMA_ASR, as Bridge
    passes it); the model is downloaded unless config says otherwise."""
    import platform

    helper = Helper(folder, **config)
    monkeypatch.setenv("MNEMA_ASR", str(helper.path))
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    return helper
