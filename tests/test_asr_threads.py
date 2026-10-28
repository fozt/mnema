"""Recognition CPU budget: the ffmpeg, Silero and ASR loading boundaries are stubbed."""

import subprocess
import sys
import types
import wave
from pathlib import Path

import pytest

from mnema.core import backends as b
from mnema.core import parakeet
from mnema.core.models import Segment


@pytest.fixture(autouse=True)
def cpu_environment(monkeypatch):
    """OS limits are the external boundary: the test does not inherit its CI container quotas."""
    files = {}
    original = Path.read_text

    def read(path, *args, **kwargs):
        if str(path).startswith("/sys/fs/cgroup") or str(path) == "/proc/self/cgroup":
            return files.get(str(path), "")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.delenv("MNEMA_ASR_THREADS", raising=False)
    return files


@pytest.mark.parametrize("backend", [b.GigaAmBackend, b.ParakeetBackend])
@pytest.mark.parametrize("seconds, fail", [(3, False), (30, False), (30, True)])
def test_asr_uses_configured_threads_after_vad_and_restores_caller(
    monkeypatch, tmp_path, backend, seconds, fail
):
    """VAD on one thread, ASR on the default budget, the budget survives an error."""
    torch = pytest.importorskip("torch")
    before = torch.get_num_threads()
    events = []
    monkeypatch.setattr(b.os, "sched_getaffinity", lambda _: set(range(16)), raising=False)
    monkeypatch.setattr(b, "_SILERO_VAD", None)
    monkeypatch.setattr(b, "_GIGAAM_MODEL", None)
    monkeypatch.setattr(parakeet, "_MODEL", None)
    monkeypatch.setattr(parakeet.platform, "machine", lambda: "x86_64")  # Parakeet's torch path

    # A fresh import reproduces the library's real side effect on torch.
    (tmp_path / "silero_vad.py").write_text(
        "import torch\n"
        "torch.set_num_threads(1)\n"
        "def load_silero_vad():\n"
        "    assert torch.get_num_threads() == 1\n"
        "    return object()\n"
        "def get_speech_timestamps(audio, model, **kwargs):\n"
        "    assert torch.get_num_threads() == 1\n"
        "    return [{'start': 0, 'end': 16000}]\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "silero_vad", raising=False)

    def ffmpeg(cmd, **kwargs):
        assert cmd[0] == "ffmpeg"
        with wave.open(cmd[-1], "wb") as out:
            out.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            out.writeframes(b"\0\0" * 16000 * seconds)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", ffmpeg)

    class Model:
        _device, _dtype, head = "cpu", torch.float32, None

        def __init__(self, *args, **kwargs):
            events.append(("load", torch.get_num_threads()))
            self.decoding = types.SimpleNamespace(decode=lambda *a: [("Привет.", [], [])])

        def forward(self, audio, length):
            events.append(("infer", torch.get_num_threads()))
            if fail:
                raise RuntimeError("ошибка движка")
            return audio, length

        def transcribe(self, audio, lang):
            self.forward(audio, None)
            return [Segment(start=0, end=1, text="Привет.")]

    monkeypatch.setitem(sys.modules, "gigaam", types.SimpleNamespace(load_model=Model))
    monkeypatch.setattr(parakeet, "Model", Model)
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    snapshot = tmp_path / "hub/models--moondream--parakeet-ultra/snapshots/test"
    snapshot.mkdir(parents=True)
    for name in ("model.safetensors", "config.json", "tokenizer.json"):
        (snapshot / name).write_text("test")

    try:
        torch.set_num_threads(2)
        if fail:
            with pytest.raises(RuntimeError, match="ошибка движка"):
                backend().run(Path("call.wav"), lang="ru")
        else:
            assert backend().run(Path("call.wav"), lang="ru").text == "Привет."
        assert events == [("load", 4), ("infer", 4)]
        assert torch.get_num_threads() == 2
    finally:
        torch.set_num_threads(before)
        sys.modules.pop("silero_vad", None)


@pytest.mark.parametrize(
    "setting, available, expected",
    [
        *[("auto", n, n) for n in (6, 32)],
        # unset: 4, but no more than available
        *[(None, n, e) for n, e in ((2, 2), (6, 4))],
        ("2", 24, 2),
        ("32", 24, 24),  # an explicit number is capped at what is available
    ],
)
def test_asr_cpu_budget_respects_available_cpus(monkeypatch, setting, available, expected):
    monkeypatch.setattr(b.os, "sched_getaffinity", lambda _: set(range(available)), raising=False)
    if setting is None:
        monkeypatch.delenv("MNEMA_ASR_THREADS", raising=False)
    else:
        monkeypatch.setenv("MNEMA_ASR_THREADS", setting)
    assert b._asr_cpu_threads() == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        *[(v, None) for v in ("0", "-1", "1.5", "all", "")],
        ("Auto", "auto"),  # a real .env typo: case and spaces do not matter
        (" 6 ", 6),
    ],
)
def test_asr_cpu_setting_has_one_validation_for_app_and_core(monkeypatch, value, expected):
    """One check for the app and the core; an invalid value falls back to the default."""
    from mnema.config import Settings
    from mnema.core.settings import DEFAULT_ASR_THREADS, RuntimeSettings

    monkeypatch.setenv("MNEMA_ASR_THREADS", value)
    for cls in (Settings, RuntimeSettings):
        assert cls().asr_threads == (DEFAULT_ASR_THREADS if expected is None else expected)


def test_cpu_budget_falls_back_to_cpu_count_on_macos(monkeypatch):
    def no_affinity(_):
        raise AttributeError

    monkeypatch.setattr(b.os, "sched_getaffinity", no_affinity, raising=False)
    monkeypatch.setattr(b.os, "cpu_count", lambda: 10)
    monkeypatch.setenv("MNEMA_ASR_THREADS", "auto")
    assert b._asr_cpu_threads() == 10


def test_cpu_budget_respects_cgroup_v2_cpuset_and_parent_quota(monkeypatch, cpu_environment):
    monkeypatch.setattr(b.os, "sched_getaffinity", lambda _: set(range(32)), raising=False)
    cpu_environment.update(
        {
            "/proc/self/cgroup": "0::/jobs/worker",
            "/sys/fs/cgroup/cpu.max": "max 100000",
            "/sys/fs/cgroup/jobs/cpu.max": "250000 100000",  # 2.5 CPUs → three worker threads
            "/sys/fs/cgroup/jobs/worker/cpu.max": "400000 100000",
            "/sys/fs/cgroup/jobs/worker/cpuset.cpus.effective": "2-5,7",
        }
    )
    assert b._asr_cpu_threads() == 3
    cpu_environment["/sys/fs/cgroup/jobs/worker/cpuset.cpus.effective"] = "2,4"
    assert b._asr_cpu_threads() == 2


def test_cpu_budget_respects_cgroup_v1_and_container_namespace(monkeypatch, cpu_environment):
    monkeypatch.setattr(b.os, "sched_getaffinity", lambda _: set(range(32)), raising=False)
    cpu_environment.update(
        {
            "/proc/self/cgroup": "3:cpu,cpuacct:/docker/job\n4:cpuset:/docker/job",
            "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us": "100000",
            "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_period_us": "100000",
            "/sys/fs/cgroup/cpuset/cpuset.cpus": "0-3",
        }
    )
    assert b._asr_cpu_threads() == 1
    cpu_environment["/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us"] = "-1"
    assert b._asr_cpu_threads() == 4


@pytest.mark.parametrize(
    "device, model",
    [("cpu", b.DEFAULT_WHISPERX_MODEL), ("cuda", b.DEFAULT_WHISPERX_MODEL), ("cuda", "small")],
)
def test_whisperx_cpu_budget_reaches_model_on_cpu_and_cuda(
    monkeypatch, cpu_environment, device, model
):
    from mnema.core.settings import asr_thread_budget

    monkeypatch.setattr(b.os, "sched_getaffinity", lambda _: set(range(8)), raising=False)
    cpu_environment["/sys/fs/cgroup/cpu.max"] = "max 100000"
    torch_threads, models, pipelines = [], [], []
    monkeypatch.setitem(
        sys.modules,
        "torch",
        types.SimpleNamespace(
            set_num_threads=torch_threads.append,
            cuda=types.SimpleNamespace(
                is_available=lambda: True, set_per_process_memory_fraction=lambda _: None
            ),
        ),
    )

    class WhisperModel:
        def __init__(self, *args, cpu_threads, **kwargs):
            self.threads = cpu_threads
            models.append(self)

    def load_model(name, device, *, compute_type, model=None, threads):
        pipelines.append((threads, model is not None))
        return types.SimpleNamespace(
            model=model or WhisperModel(name, device=device, cpu_threads=threads)
        )

    external = types.SimpleNamespace(load_model=load_model)
    monkeypatch.setitem(sys.modules, "whisperx", external)
    monkeypatch.setitem(
        sys.modules, "whisperx.asr", types.SimpleNamespace(WhisperModel=WhisperModel)
    )
    monkeypatch.setattr(b, "_SHARED_WHISPER", None)
    monkeypatch.setattr(b, "_WHISPERX_ASR", {})
    with asr_thread_budget(6):
        pipeline = b._whisperx_model(external, model, device, "float16")
        assert b._whisperx_model(external, model, device, "float16") is pipeline
    assert [m.threads for m in models] == [6]
    assert pipelines == [(6, device == "cuda" and model == b.DEFAULT_WHISPERX_MODEL)]
    if device == "cuda" and model == b.DEFAULT_WHISPERX_MODEL:
        assert torch_threads == [6]
