"""Applying the node's result: a failure or retry neither loses files nor repeats processing."""

import pytest

from mnema.live import meta


def result(*, summary=False, failed=False, folder=None):
    return {
        "files": {}
        if failed
        else {"summary.txt": "new summary", **({} if summary else {"transcript.txt": "new text"})},
        "folder": folder,
        "run": meta.Run(
            kind="summary" if summary else "process",
            status="failed" if failed else "ok",
            error="Недостаточно средств" if failed else None,
        ).model_dump(mode="json"),
    }


class Node:
    def __init__(self, answer):
        self.answer, self.calls, self.forgotten = answer, [], []
        self._base_url = "http://selected-node"

    def upload_resumable(self, path, *, on):
        self.calls.append(("upload", path.name))
        return f"/uploads/{path.name}"

    def submit(self, op, **kwargs):
        self.calls.append(("submit", op, kwargs))
        return f"job-{len([c for c in self.calls if c[0] == 'submit'])}"

    def wait_job(self, job, **kwargs):
        return {"status": "done", "result": self.answer}

    def forget(self, job):
        self.forgotten.append(job)

    def cancel(self, job):
        self.calls.append(("cancel", job))


def folder(tmp_path):
    base = tmp_path / "2026-10-23_12-30"
    base.mkdir()
    (base / "mic.wav").write_bytes(b"audio")
    (base / "transcript.txt").write_text("old text")
    (base / "summary.txt").write_text("old summary")
    meta.set_started(base, "2026-10-23T12:30:00+03:00")
    return base


def params(base):
    return {"name": base.name, "started": meta.started(base), "asr": "auto"}


def test_failed_summary_preserves_files_and_manual_name(tmp_path):
    from mnema.live.recording_client import process

    base = folder(tmp_path)
    base = base.rename(base.with_name("2026-10-23_Manual name"))
    node = Node(result(summary=True, failed=True))
    out = process(base, node, "recording_summary", params(base), {"text": base / "transcript.txt"})
    assert out.base == base
    assert (base / "transcript.txt").read_text() == "old text"
    assert (base / "summary.txt").read_text() == "old summary"
    assert out.summary_md is None and base.is_dir()
    assert meta.last(base).status == "failed"
    assert meta.last(base).job == "job-1" and node.forgotten == ["job-1"]


def test_new_summary_replaces_summary_md_of_an_earlier_version(tmp_path):
    from mnema.live.recording_client import process

    base = folder(tmp_path)
    (base / "summary.txt").rename(base / "summary.md")
    node = Node(result(summary=True))
    out = process(base, node, "recording_summary", params(base), {"text": base / "transcript.txt"})
    assert (out.base / "summary.txt").read_text() == "new summary"
    assert not (out.base / "summary.md").exists()


def test_disk_failure_retries_apply_without_resubmitting(tmp_path, monkeypatch):
    from mnema.live import recording_client as rc

    base = folder(tmp_path)
    node = Node(result())
    original = rc._atomic_bytes
    failed = False

    def write(path, data):
        nonlocal failed
        if path.name == "summary.txt" and not failed:
            failed = True
            raise OSError("disk full")
        original(path, data)

    monkeypatch.setattr(rc, "_atomic_bytes", write)
    with pytest.raises(OSError, match="disk full"):
        rc.process(base, node, "recording", params(base), {"mic": base / "mic.wav"})
    assert node.forgotten == [] and (base / "summary.txt").read_text() == "old summary"
    out = rc.process(base, node, "recording", params(base), {"mic": base / "mic.wav"})
    assert (
        out.transcript_txt.read_text() == "new text" and out.summary_md.read_text() == "new summary"
    )
    assert len([c for c in node.calls if c[0] == "submit"]) == 1
    assert len([c for c in node.calls if c[0] == "upload"]) == 1
    assert node.forgotten == ["job-1"]


def test_rename_and_history_are_idempotent_after_forget_failure(tmp_path, monkeypatch):
    from mnema.bar.recordings import library
    from mnema.live.recording_client import process
    from mnema.live.setup import PROCESSING_MARK

    base = folder(tmp_path)
    node = Node(result(folder="2026-10-23_New-title"))
    forget = node.forget
    monkeypatch.setattr(node, "forget", lambda job: (_ for _ in ()).throw(OSError("lost response")))
    with pytest.raises(OSError, match="lost response") as caught:
        process(base, node, "recording", params(base), {"mic": base / "mic.wav"})
    renamed = tmp_path / "2026-10-23_New-title"
    assert caught.value.mnema_base == renamed and not base.exists()
    (renamed / "transcript.txt").write_text("user edit after apply")
    monkeypatch.setattr(node, "forget", forget)
    process(renamed, node, "recording", params(renamed), {"mic": renamed / "mic.wav"})
    assert (renamed / "transcript.txt").read_text() == "user edit after apply"
    import json

    assert len(json.loads((renamed / meta.META_FILE).read_text())["runs"]) == 1
    assert len([c for c in node.calls if c[0] == "submit"]) == 1
    assert not (renamed / PROCESSING_MARK).exists()
    assert [c.state for c in library(tmp_path).cards] == [meta.RecordState.PROCESSED]


def partial(*, folder=None):
    answer = result(folder=folder)
    answer["run"]["tracks"] = {"mic": {"engine": "gigaam"}, "system": {"error": "429"}}
    return answer


@pytest.mark.parametrize("text, accepted", [("old text", False), (" \n", True), (None, True)])
def test_half_of_conversation_is_accepted_only_without_previous_transcript(
    tmp_path, text, accepted
):
    from mnema.live.recording_client import process

    base = folder(tmp_path)
    if text is None:
        (base / "transcript.txt").unlink()
    else:
        (base / "transcript.txt").write_text(text)
    node = Node(result())
    process(base, node, "recording", params(base), {"mic": base / "mic.wav"})
    (submit,) = [c for c in node.calls if c[0] == "submit"]
    # A track failed: without partial the node fails with no files and the old ones stay
    assert submit[2]["params"]["partial"] is accepted


def test_partial_result_of_new_recording_is_applied_once_after_disk_failure(tmp_path, monkeypatch):
    from mnema.live import recording_client as rc

    base = folder(tmp_path)
    (base / "transcript.txt").unlink()
    (base / "summary.txt").unlink()
    (base / "system.wav").write_bytes(b"audio")
    inputs = {"mic": base / "mic.wav", "system": base / "system.wav"}
    node = Node(partial())
    original = rc._atomic_bytes
    failed = False

    def write(path, data):
        nonlocal failed
        if path.name == meta.META_FILE and not failed:
            failed = True
            raise OSError("disk full")
        original(path, data)

    monkeypatch.setattr(rc, "_atomic_bytes", write)
    with pytest.raises(OSError, match="disk full"):
        rc.process(base, node, "recording", params(base), inputs)
    assert (base / "transcript.txt").read_text() == "new text"
    out = rc.process(base, node, "recording", params(base), inputs)
    assert out.transcript_txt.read_text() == "new text"
    assert out.summary_md.read_text() == "new summary"
    assert meta.last(base).status == "ok" and meta.last(base).tracks["system"].error
    assert len([c for c in node.calls if c[0] == "submit"]) == 1


@pytest.mark.parametrize("options", [{"asr": "gigaam"}, {"lang": "en"}, {"summary": False}])
def test_applied_job_is_finished_before_new_requested_options(tmp_path, monkeypatch, options):
    from mnema.live.recording_client import process

    base = folder(tmp_path)
    node = Node(result(folder="2026-10-23_New-title"))
    forget = node.forget
    monkeypatch.setattr(node, "forget", lambda job: (_ for _ in ()).throw(OSError("lost response")))
    with pytest.raises(OSError):
        process(base, node, "recording", params(base), {"mic": base / "mic.wav"})
    renamed = tmp_path / "2026-10-23_New-title"
    monkeypatch.setattr(node, "forget", forget)
    process(
        renamed, node, "recording", {**params(renamed), **options}, {"mic": renamed / "mic.wav"}
    )
    submissions = [c[2] for c in node.calls if c[0] == "submit"]
    assert len(submissions) == 2 and submissions[0]["key"] != submissions[1]["key"]
    assert all(submissions[1]["params"][key] == value for key, value in options.items())
    assert submissions[1]["params"]["name"] == renamed.name


@pytest.mark.parametrize("file", ["../outside", ".mnema-meta.json", "mic.wav"])
def test_invalid_node_file_is_rejected_before_mutation(tmp_path, file):
    from mnema.core.base import MediaError
    from mnema.live.recording_client import process

    base = folder(tmp_path)
    answer = result()
    answer["files"][file] = "bad"
    node = Node(answer)
    with pytest.raises(MediaError):
        process(base, node, "recording", params(base), {"mic": base / "mic.wav"})
    assert (base / "transcript.txt").read_text() == "old text" and node.forgotten == []


def test_new_explicit_node_does_not_reuse_old_job_or_touch_manual_edits(tmp_path):
    from mnema.live.recording_client import process

    base = folder(tmp_path)
    old = Node(result())
    old.wait_job = lambda *a, **k: (_ for _ in ()).throw(OSError("node offline"))
    with pytest.raises(OSError):
        process(base, old, "recording", params(base), {"mic": base / "mic.wav"})
    (base / "summary.txt").write_text("my manual summary")
    before = list(old.calls)
    new = Node(result(summary=True, failed=True))
    new._base_url = "http://new-selected-node"
    process(base, new, "recording_summary", params(base), {"text": base / "transcript.txt"})
    assert old.calls == before
    assert new.calls[-1][2]["key"] != old.calls[-1][2]["key"]
    assert (base / "summary.txt").read_text() == "my manual summary"


def test_metadata_write_failure_keeps_server_result_for_retry(tmp_path, monkeypatch):
    from mnema.live import recording_client as rc

    base = folder(tmp_path)
    node = Node(result())
    original = rc._atomic_bytes
    monkeypatch.setattr(
        rc,
        "_atomic_bytes",
        lambda path, data: (
            (_ for _ in ()).throw(OSError("metadata full"))
            if path.name == meta.META_FILE
            else original(path, data)
        ),
    )
    with pytest.raises(OSError, match="metadata full"):
        rc.process(base, node, "recording", params(base), {"mic": base / "mic.wav"})
    assert node.forgotten == [] and meta.last(base) is None
    monkeypatch.setattr(rc, "_atomic_bytes", original)
    rc.process(base, node, "recording", params(base), {"mic": base / "mic.wav"})
    assert meta.last(base).job == "job-1" and node.forgotten == ["job-1"]


def test_cancel_progress_targets_original_node_and_keeps_audio(tmp_path):
    from mnema.live.recording_client import process
    from mnema.live.setup import FinalizeCancelled

    base = folder(tmp_path)
    node = Node(result())
    node.wait_job = lambda job, *, on: on(0.5, "processing")

    def progress(fraction, message):
        if message == "processing":
            raise FinalizeCancelled

    with pytest.raises(FinalizeCancelled):
        process(base, node, "recording", params(base), {"mic": base / "mic.wav"}, on=progress)
    assert ("cancel", "job-1") in node.calls and node.forgotten == []
    assert (base / "mic.wav").read_bytes() == b"audio"
    assert (base / "summary.txt").read_text() == "old summary"


def test_apply_refuses_symlink_before_writing_any_result(tmp_path):
    from mnema.core.base import MediaError
    from mnema.live.recording_client import process

    base = folder(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("private")
    (base / "summary.txt").unlink()
    (base / "summary.txt").symlink_to(outside)
    node = Node(result())
    with pytest.raises(MediaError, match="Нельзя заменить"):
        process(base, node, "recording", params(base), {"mic": base / "mic.wav"})
    assert outside.read_text() == "private" and (base / "transcript.txt").read_text() == "old text"


def test_job_expired_while_offline_gets_a_new_key_without_uploading_again(tmp_path):
    """The node forgot the job while the client was offline: the next run submits it again with
    a new key; the node did not report the upload lost, so audio is not uploaded again."""
    from mnema.live.recording_client import process

    base = folder(tmp_path)
    node = Node(result())
    node.wait_job = lambda *a, **k: (_ for _ in ()).throw(OSError("lost connection"))
    with pytest.raises(OSError):
        process(base, node, "recording", params(base), {"mic": base / "mic.wav"})
    first_key = node.calls[-1][2]["key"]
    replies = iter([None, {"status": "done", "result": node.answer}])
    node.wait_job = lambda *a, **k: next(replies)
    out = process(base, node, "recording", params(base), {"mic": base / "mic.wav"})
    assert out.transcript_txt.read_text() == "new text"
    assert node.calls[-1][2]["key"] != first_key
    assert len([c for c in node.calls if c[0] == "upload"]) == 1


def test_expired_upload_only_resends_missing_track(tmp_path):
    from mnema.client import UploadMissing
    from mnema.live.recording_client import process

    base = folder(tmp_path)
    (base / "system.wav").write_bytes(b"other audio")
    node = Node(result())
    submit = node.submit
    attempts = []

    def lost(op, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise UploadMissing("mic")
        return submit(op, **kwargs)

    node.submit = lost
    process(
        base, node, "recording", params(base), {x: base / f"{x}.wav" for x in ("mic", "system")}
    )
    assert [c[1] for c in node.calls if c[0] == "upload"] == ["mic.wav", "system.wav", "mic.wav"]
    assert attempts[0]["key"] == attempts[1]["key"]


def test_previous_text_stays_readable_until_terminal_result(tmp_path):
    from mnema.live.recording_client import process

    base = folder(tmp_path)
    node = Node(result())

    def waiting(job, **kwargs):
        assert (base / "transcript.txt").read_text() == "old text"
        assert (base / "summary.txt").read_text() == "old summary"
        return {"status": "done", "result": node.answer}

    node.wait_job = waiting
    process(base, node, "recording", params(base), {"mic": base / "mic.wav"})
    assert (base / "transcript.txt").read_text() == "new text"


def test_failure_after_rename_resumes_same_job_and_target(tmp_path, monkeypatch):
    from mnema.live import recording_client as rc

    base = folder(tmp_path)
    node = Node(result(folder="2026-10-23_New-title"))
    write = rc._atomic_bytes

    def fail_metadata(path, data):
        if path.name == meta.META_FILE:
            raise OSError("metadata full")
        write(path, data)

    monkeypatch.setattr(rc, "_atomic_bytes", fail_metadata)
    with pytest.raises(OSError) as error:
        rc.process(base, node, "recording", params(base), {"mic": base / "mic.wav"})
    renamed = error.value.mnema_base
    assert renamed.name == "2026-10-23_New-title" and not base.exists()
    monkeypatch.setattr(rc, "_atomic_bytes", write)
    out = rc.process(renamed, node, "recording", params(renamed), {"mic": renamed / "mic.wav"})
    assert out.base == renamed and meta.last(renamed).job == "job-1"
    assert len([c for c in node.calls if c[0] == "submit"]) == 1


def _cancelled_first(tmp_path):
    from mnema.live.recording_client import process
    from mnema.live.setup import FinalizeCancelled

    base = folder(tmp_path)
    node = Node(result())
    node.wait_job = lambda job, *, on: on(0.5, "processing")

    def stop(fraction, message):
        if message == "processing":
            raise FinalizeCancelled

    with pytest.raises(FinalizeCancelled):
        process(base, node, "recording", params(base), {"mic": base / "mic.wav"}, on=stop)
    return base, node


def _stopping(node, polls):
    """The cancelled job-1 finishes its model step for `polls` more polls; the new job answers
    at once."""

    def wait(job, *, on):
        if job == "job-1":
            for _ in range(polls):
                assert node.forgotten == []
                on(None, "Составляю пересказ…")
            return {"status": "error", "error": "Отменено"}
        return {"status": "done", "result": node.answer}

    node.wait_job = wait


def test_new_request_waits_until_cancelled_job_stops(tmp_path):
    from mnema.live.recording_client import process

    base, node = _cancelled_first(tmp_path)
    _stopping(node, polls=3)
    seen = []
    out = process(
        base,
        node,
        "recording",
        {**params(base), "lang": "en"},
        {"mic": base / "mic.wav"},
        on=lambda fraction, message: seen.append(message),
    )
    assert out.transcript_txt.read_text() == "new text"
    assert node.forgotten == ["job-1", "job-2"]
    submits = [c[2] for c in node.calls if c[0] == "submit"]
    assert len(submits) == 2 and submits[1]["params"]["lang"] == "en"
    assert "Составляю пересказ…" not in seen  # the old job's progress is not shown as the new one's


def test_cancelled_job_that_keeps_running_is_reported_and_awaited_again(tmp_path, monkeypatch):
    from mnema.core.base import MediaError
    from mnema.live import recording_client as rc

    base, node = _cancelled_first(tmp_path)
    monkeypatch.setattr(rc, "_STOP_WAIT_S", 0.0)
    _stopping(node, polls=3)
    with pytest.raises(MediaError, match="не остановилась"):
        rc.process(base, node, "recording", params(base), {"mic": base / "mic.wav"})
    assert node.forgotten == [] and len([c for c in node.calls if c[0] == "submit"]) == 1
    assert (base / "transcript.txt").read_text() == "old text"
    monkeypatch.setattr(rc, "_STOP_WAIT_S", 120.0)
    out = rc.process(base, node, "recording", params(base), {"mic": base / "mic.wav"})
    assert out.transcript_txt.read_text() == "new text"
    assert node.forgotten == ["job-1", "job-2"]


def test_stop_while_waiting_for_replaced_job_does_not_revive_it(tmp_path):
    from mnema.live.recording_client import process
    from mnema.live.setup import FinalizeCancelled

    base = folder(tmp_path)
    node = Node(result())

    def offline(job, *, on):
        raise OSError("связь с узлом оборвалась")

    node.wait_job = offline
    with pytest.raises(OSError):  # job-1 stays on the node to be continued
        process(base, node, "recording", params(base), {"mic": base / "mic.wav"})
    _stopping(node, polls=3)

    def stop(fraction, message):
        if message == "Останавливаю прежнюю обработку…":
            raise FinalizeCancelled  # another language chosen, then processing stopped at once

    with pytest.raises(FinalizeCancelled):
        english = {**params(base), "lang": "en"}
        process(base, node, "recording", english, {"mic": base / "mic.wav"}, on=stop)
    out = process(base, node, "recording", params(base), {"mic": base / "mic.wav"})
    assert out.transcript_txt.read_text() == "new text"  # cancelled job-1 is not the answer
    assert node.forgotten == ["job-1", "job-2"]


def test_upload_is_its_own_stage_and_does_not_fill_the_node_scale(tmp_path):
    from mnema.live.recording_client import process

    base = folder(tmp_path)
    node = Node(result())

    def upload(path, *, on):
        on(0.5, "Отправляю на сервер…")
        on(1.0, "Отправляю на сервер…")
        return f"/uploads/{path.name}"

    def wait(job, *, on):
        on(0.1, "Распознаю речь…")
        on(1.0, "Сохраняю…")
        return {"status": "done", "result": node.answer}

    node.upload_resumable, node.wait_job = upload, wait
    seen = []
    process(
        base,
        node,
        "recording",
        params(base),
        {"mic": base / "mic.wav"},
        on=lambda fraction, message: seen.append((fraction, message)),
    )
    assert [f for f, _ in seen if f is not None] == [0.1, 1.0]
    assert seen[0] == (None, "Отправляю на сервер…")


@pytest.mark.parametrize(
    ("own", "expected"),
    [
        ("Планёрка: итоги", "2026-10-23_Планёрка итоги"),
        ("", "2026-10-23_Дейли"),
        ("???", "2026-10-23_Дейли"),
    ],
)
def test_own_title_set_while_the_node_works_names_the_folder(tmp_path, own, expected):
    """A title set after the request with the earlier one was sent names the folder, not the
    node's earlier one; a cleared title (or one empty as a folder name) does not bring the
    earlier one back."""
    from mnema.live.recording_client import begin, own_title, process, set_own_title

    base = folder(tmp_path).rename(tmp_path / "2026-10-23_Дейли")
    begin(base)
    set_own_title(base, "Раньше")
    node = Node(result(folder="2026-10-23_Раньше"))  # the node used the requested title

    def wait(job, **kwargs):
        set_own_title(base, own)
        return {"status": "done", "result": node.answer}

    node.wait_job = wait
    request = {**params(base), "user_title": own_title(base)}
    out = process(base, node, "recording", request, {"mic": base / "mic.wav"})
    assert out.base.name == expected
    assert own_title(out.base) is None  # processing is over: no work file


def test_own_title_set_after_the_result_is_applied_still_names_the_folder(tmp_path):
    """A title set after the result was applied but before processing ended names the folder,
    not the node."""
    from mnema.live.recording_client import own_title, process, set_own_title

    base = folder(tmp_path)
    node = Node(result(folder="2026-10-23_Модель"))
    moved: list = []
    node.forget = lambda job: set_own_title(moved[-1], "Позже")
    out = process(
        base, node, "recording", params(base), {"mic": base / "mic.wav"}, on_base=moved.append
    )
    assert out.base.name == "2026-10-23_Позже" and moved[-1] == out.base
    assert own_title(out.base) is None and out.transcript_txt.read_text() == "new text"


def test_renamed_folder_no_longer_waits_for_its_own_title(tmp_path):
    """A title waited for processing but the folder was renamed by hand: processing no longer
    renames it, and the processing window offers the new name."""
    from mnema.live.recording_client import own_title, set_own_title
    from mnema.live.setup import rename_session

    base = folder(tmp_path)
    set_own_title(base, "Планёрка", create=True)
    renamed = rename_session(base, "Дейли")
    assert renamed.name == "2026-10-23_Дейли" and own_title(renamed) is None


def test_own_title_waits_without_marking_a_processed_recording_unfinished(tmp_path):
    """A title waits for processing that never reached the node: a processed recording does
    not turn into an interrupted one; one interrupted after the request was sent does."""
    from mnema.bar.recordings import library
    from mnema.live.recording_client import process, set_own_title
    from mnema.live.setup import unfinished_sessions

    base = folder(tmp_path)
    (base / "mic.wav").write_bytes(b"\0" * 32044)
    meta.append(base, meta.Run(status="ok"))
    set_own_title(base, "Планёрка", create=True)
    assert unfinished_sessions(tmp_path) == []
    assert library(tmp_path).cards[0].state is meta.RecordState.PROCESSED

    node = Node(result())
    node.wait_job = lambda *a, **k: (_ for _ in ()).throw(OSError("node offline"))
    with pytest.raises(OSError):
        process(base, node, "recording", params(base), {"mic": base / "mic.wav"})
    assert unfinished_sessions(tmp_path) == [base]
    assert library(tmp_path).cards[0].state is meta.RecordState.UNPROCESSED


def _english(text: str) -> bool:
    return not any("а" <= ch.lower() <= "я" or ch in "ёЁ" for ch in text)


def test_client_texts_render_in_english(tmp_path, monkeypatch):
    from mnema.core import base as core_base
    from mnema.core.base import MediaError
    from mnema.i18n import using
    from mnema.live import recording_client
    from mnema.live.recording_client import alone, process

    base = folder(tmp_path)
    seen = []

    def upload(path, *, on):
        on(0.5, "upload")
        return f"/uploads/{path.name}"

    node = Node(result())
    node.upload_resumable = upload
    monkeypatch.setattr(core_base, "STANDALONE", False)
    with using("en"):
        process(
            base,
            node,
            "recording",
            params(base),
            {"mic": base / "mic.wav"},
            on=lambda fraction, message: seen.append(message),
        )
        assert seen[0] == "Uploading to the server…"
        with pytest.raises(MediaError) as offline:
            recording_client._client(None)
        assert (
            str(offline.value) == "Server not connected. The recording is saved; process it later"
        )
        with alone(base), pytest.raises(MediaError) as busy:
            recording_client.process(base, node, "recording", params(base), {})
        assert str(busy.value) == "This recording is already being processed"
        (base / "summary.txt").unlink()
        (base / "summary.txt").symlink_to(tmp_path / "elsewhere")
        with pytest.raises(MediaError) as unsafe:
            process(base, Node(result()), "recording", params(base), {"mic": base / "mic.wav"})
        assert str(unsafe.value) == "Cannot replace a recording file: summary.txt"
    assert all(_english(text) for text in (*seen, str(offline.value), str(busy.value)))
