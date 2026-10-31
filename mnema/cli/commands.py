"""CLI commands (cmd_*) and their parameter adapters over the core, the LLM layer and dispatch."""

from __future__ import annotations

import copy
import functools
import logging
import sys
from collections.abc import Callable
from pathlib import Path

from rich import box
from rich.markup import escape
from rich.table import Table
from rich.text import Text

import mnema.cli as _cli  # late-bound: tests patch dispatch, run_with_progress and console

from .. import core as mc
from .. import ops
from ..core.base import phased
from ..dispatch import fetch_result_file, resolve_runner
from ..i18n import _, _n
from ..live import meta
from .sources import (
    _dispatch_sources,
    _out_path,
    _sources,
    _url_stem,
    browser_cookies_netscape,
    concat_parts,
    cookies_path,
)
from .ui import ACCENT, die, done_panel, info_panel, warn_panel

_NO_SAVE_AND_OUTPUT = (
    "--no-save and -o make no sense together: either a file at the path or the clipboard "
    "without a file."
)


def _words_lang(text: str, lang: str | None) -> str:
    return _n(
        "{n} word · language: {lang}",
        "{n} words · language: {lang}",
        len(text.split()),
        lang=lang or "—",
    )


def _words_to(text: str, lang: str) -> str:
    return _n("{n} word · {lang}", "{n} words · {lang}", len(text.split()), lang=lang)


def _compressed(before: int, after: int) -> str:
    if not (before and after):
        return mc.fmt_size(after)
    return _(
        "{before} → {after} (×{ratio} smaller)",
        before=mc.fmt_size(before),
        after=mc.fmt_size(after),
        ratio=f"{before / after:.1f}",
    )


def _recording_files(paths: list[Path]) -> str:
    return _("recording: {files}", files=", ".join(p.name for p in paths))


def _no_audio(src: Path) -> None:
    _cli.console.print(f"[red]{_('No audio track in the file, skipping:')}[/] {escape(src.name)}")


def _op_once(
    op: str,
    src: Path | str | list,
    params: dict,
    *,
    runner: str | None,
    local: bool,
    on,
    cookies: str | None = None,
) -> dict:
    """Exactly one op call through dispatch, no UI: shared by the single and the batch paths."""
    return _cli.dispatch(op, src, params, runner=runner, local=local, cookies=cookies, on=on)


def _at_node(command):
    """Where the command processes, by the app's rule (node.selected_processing): runner is the
    selected server (Connection) or None for this computer."""

    @functools.wraps(command)
    def run(args):
        from ..node import selected_processing

        selected = copy.copy(args)
        selected.runner = selected_processing(args.runner, local=args.local)
        selected.local = selected.runner is None
        return command(selected)

    return run


def _cookies_for(args, sources) -> str | None:
    """cookies.txt for downloading links on the client with a runner (--cookies or
    --cookies-from-browser). Cookies never leave this machine: only the media is uploaded.
    Locally yt-dlp reads the browser or the file itself."""
    urls = [s for s in sources if isinstance(s, str)]
    if not urls:
        return None
    path = cookies_path(args)
    if path is not None:
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            die(_("Cannot read the cookies file {path}: {error}", path=path, error=e))
    spec = args.cookies_from_browser
    return browser_cookies_netscape(spec, urls) if spec else None


def _no_save_failure(result) -> str | None:
    """Why --no-save has no text: a recognition failure or deferred processing. A track with
    nothing to recognize (noise, no speech) is not a failure."""
    from ..live import meta

    if result.defer_command or result.transcript_txt is None:
        return _("part of the recording was not transcribed")
    run = meta.last(result.base) if result.base else None
    if run and (run.status == "failed" or any(track.error for track in run.tracks.values())):
        return _("part of the recording failed recognition")
    return None


def _run_op(
    label: str,
    op: str,
    src: Path | str | list,
    params: dict,
    *,
    runner: str | None,
    local: bool = False,
    cookies: str | None = None,
) -> dict:
    """Run an op through dispatch under the fitting progress wrapper.

    Locally run_streaming keeps mlx in the main thread without competing for the GIL;
    remotely run_with_progress polls the runner in the background.
    """
    is_local = resolve_runner(runner, local=local) is None
    wrapper = _cli.run_streaming if is_local else _cli.run_with_progress
    return wrapper(
        label,
        lambda on: _op_once(op, src, params, runner=runner, local=local, cookies=cookies, on=on),
        total=None,
        live_preview=True,
    )


def _run_batch(label: str, items: list, *, runner: str | None, local: bool) -> None:
    """A batch under one bar: item i takes the slice [(i-1)/n, i/n] and prefixes its messages
    with "i/n:", instead of a stack of frozen bars in the scrollback.

    items are callbacks item(on) doing all the work of one file; print results after the bar
    (printing from another thread during Live breaks the screen)."""
    is_local = resolve_runner(runner, local=local) is None
    wrapper = _cli.run_streaming if is_local else _cli.run_with_progress
    n = len(items)

    def work(on):
        slices = phased(on, *([1.0] * n))
        for i, (item, sl) in enumerate(zip(items, slices, strict=True), 1):

            def on_i(frac, msg, _sl=sl, _i=i):
                _sl(frac, f"{_i}/{n}: {msg}" if msg else "")

            item(on_i)
            sl(1.0, "")  # close the slice: the op may not have sent 1.0

    wrapper(label, work, total=1.0, live_preview=True)


def _batch_table(title: str, names: list[str]) -> None:
    """Numbered list of the batch files instead of a summary per file."""
    t = Table(box=box.SIMPLE_HEAVY, title=title, title_style=f"bold {ACCENT}")
    t.add_column("#", style=ACCENT, justify="right")
    t.add_column(_("file"))
    for i, name in enumerate(names, 1):
        t.add_row(str(i), Text(name))
    _cli.console.print(t)


def _deliver_text(text: str, out: Path, *, detail: str, no_save: bool) -> None:
    """Deliver a text result: a file (with done_panel) or, with --no-save, the clipboard; no
    clipboard — print it. A batch comes as one text."""
    if not no_save:
        out.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        done_panel(out, detail)
        return
    from ..live.clipboard import copy_to_clipboard

    if copy_to_clipboard(text):
        _cli.console.print(f"[{ACCENT}]{_('📋 in the clipboard')}[/] · {detail}")
    else:
        _cli.console.print(Text(text), soft_wrap=True)


def _titled_out(result: dict, src, out: Path, args, ext: str) -> Path:
    """Result name from the downloaded file's title (source_name from dispatch).

    Only for links without -o; yt-dlp names downloads "Title [id]", the id is cut off."""
    name = (result or {}).get("source_name")
    if not name or not isinstance(src, str) or args.output:
        return out
    title = name.rsplit(" [", 1)[0].strip() if name.endswith("]") and " [" in name else name
    return out.with_name(f"{title}{ext}") if title else out


def _transcribe_params(args, *, lang: str | None = None) -> dict:
    """Parameters of the transcribe op from the flags; lang overrides --lang."""
    params: dict = {"backend": args.backend, "model": args.model}
    lang = args.lang if lang is None else lang
    if lang:
        params["lang"] = lang
    return params


def hint(label: str, value: str, *, style: str = "yellow") -> None:
    """A "label: value" hint where the value is a path or a command, as one logical line
    (soft_wrap) so it copies whole. Prints through _cli.console like the other messages."""
    _cli.console.print(Text.assemble((label, style), (" ", ""), (value, "bold")), soft_wrap=True)


@_at_node
def cmd_transcribe(args) -> None:
    runner = args.runner
    local = args.local
    if args.merge and args.concat:
        die(
            _(
                "--merge and --concat cannot be combined: tracks are either parallel "
                "(--merge) or back to back."
            )
        )
    if getattr(args, "no_save", False) and args.output:
        die(_(_NO_SAVE_AND_OUTPUT))
    sources = _dispatch_sources(
        args,
        want_video=False,
        runner=runner,
        local=local,
    )
    if args.merge:  # parallel tracks of one call → one transcript by time
        _transcribe_merge(args, sources, runner=runner, local=local)
        return
    is_remote = resolve_runner(runner, local=local) is not None
    if args.concat and is_remote and len(sources) > 1 and all(isinstance(s, str) for s in sources):
        # The client downloads the links and sends them as one join-and-recognize job.
        _transcribe_concat_remote(args, sources, runner=runner, local=local)
        return
    if args.concat and is_remote and any(isinstance(s, str) for s in sources):
        die(
            _(
                "transcribe --concat with a mix of links and files is not supported with a "
                "runner: use only links (joined into one job) or --local."
            )
        )
    if len(sources) > 1:
        _transcribe_batch(args, sources, runner=runner, local=local)
        return
    for src in sources:
        label = _("Transcription")
        out = _out_path(src, args, len(sources), ".txt")
        lang = args.lang
        params = _transcribe_params(args, lang=lang)
        result = _run_op(
            label,
            "transcribe",
            src,
            params,
            runner=runner,
            local=local,
            cookies=_cookies_for(args, [src]),
        )
        tr = mc.Transcript.model_validate(result)
        out = _titled_out(result, src, out, args, ".txt")  # a link with a runner: name from title
        no_save = getattr(args, "no_save", False)
        parts = concat_parts(src)
        if parts:
            # a join: each part's name before its piece of text
            text = _render_concat_parts(tr, parts)
            _deliver_text(text, out, detail=_words_lang(text, tr.language), no_save=no_save)
            if not no_save:  # the translation is written next to the file
                _maybe_translate(args, out, text, title=None)
        else:
            body = tr.as_text()
            title = out.stem  # the first line of the file names it; the clipboard gets the body
            _deliver_text(
                f"{title}\n\n{body}" if not no_save else body,
                out,
                detail=_words_lang(body, tr.language),
                no_save=no_save,
            )
            if not no_save:
                _maybe_translate(args, out, body, title=title)


def _render_concat_parts(tr, parts: list[tuple[str, float]]) -> str:
    """Transcript of a --concat join: each part's name before its piece, by the part offsets.
    Without timestamps or offsets the names are listed on top."""
    offsets = [off for _name, off in parts]
    increasing = all(b > a for a, b in zip(offsets, offsets[1:], strict=False))
    if not tr.segments or (len(parts) > 1 and not increasing):
        names = "\n".join(name for name, _off in parts)
        body = tr.as_text()
        return f"{names}\n\n{body}"
    groups: list[list] = [[] for _part in parts]
    for seg in tr.segments:
        idx = 0
        for i, off in enumerate(offsets):
            if seg.start >= off - 0.5:  # half a second of slack for join inaccuracy
                idx = i
        groups[idx].append(seg)
    blocks = []
    for (name, _off), segs in zip(parts, groups, strict=True):
        sub = mc.Transcript(
            text=" ".join(s.text for s in segs).strip(), segments=segs, language=tr.language
        )
        body = sub.as_text()
        blocks.append(f"{name}\n\n{body}".rstrip())
    return "\n\n".join(blocks)


def _transcribe_concat_remote(args, urls: list, *, runner: str | None, local: bool) -> None:
    """The client downloads and joins the links (inputs.local_input); the runner recognizes
    them as one job."""
    params = _transcribe_params(args)
    result = _run_op(
        _("Transcription"),
        "transcribe",
        list(urls),
        params,
        runner=runner,
        local=local,
        cookies=_cookies_for(args, urls),
    )
    tr = mc.Transcript.model_validate(result)
    body = tr.as_text()
    stems = "+".join(dict.fromkeys(_url_stem(u) for u in urls))[:60] or "merged"
    out = Path(args.output).expanduser() if args.output else Path.cwd() / f"{stems}.txt"
    title = "\n".join(_url_stem(u) for u in urls)  # no part offsets from a runner: names on top
    no_save = getattr(args, "no_save", False)
    _deliver_text(
        f"{title}\n\n{body}" if not no_save else body,
        out,
        detail=_words_lang(body, tr.language),
        no_save=no_save,
    )
    if not no_save:
        _maybe_translate(args, out, body, title=title)


def _transcribe_batch(args, sources: list, *, runner: str | None, local: bool) -> None:
    """A batch of files: one shared bar (each file its own slice), results after it."""
    is_local = resolve_runner(runner, local=local) is None
    todo = []
    for src in sources:
        if is_local and isinstance(src, Path) and not mc.probe(src).has_audio:
            _no_audio(src)
            continue
        todo.append(src)
    if not todo:
        die(_("None of the files has an audio track."))
    _batch_table(_("batch transcription"), [s.name if isinstance(s, Path) else s for s in todo])
    no_save = getattr(args, "no_save", False)
    # Results and errors are printed after Live. out=None is an error line; ephemeral holds
    # the texts to copy at once.
    done_rows: list[tuple[Path | None, str]] = []
    ephemeral: list[str] = []
    failures: list[str] = []

    def make_item(src):
        out = _out_path(src, args, len(sources), ".txt")

        def item(on):
            if not no_save and isinstance(src, Path) and out.exists() and out.stat().st_size:
                # A rerun after a failure continues where it stopped; deleting the .txt redoes it.
                done_rows.append((out, _("already transcribed, skipped (delete the .txt to redo)")))
                on(1.0, "")
                return
            params = _transcribe_params(args)
            # --translate takes the tail of the same slice (a nested Live would break the screen)
            tail_tr = 0.2 if args.translate and not no_save else 0.0
            p_op, p_tr = phased(on, 1.0 - tail_tr, tail_tr or 1e-9)
            p_tr = p_tr if tail_tr else None
            try:
                result = _op_once(
                    "transcribe",
                    src,
                    params,
                    runner=runner,
                    local=local,
                    on=p_op,
                    cookies=_cookies_for(args, [src]),
                )
            except Exception as exc:  # noqa: BLE001 - one file does not stop the batch
                name = src.name if isinstance(src, Path) else str(src)
                failures.append(name)
                done_rows.append((None, f"[red]✗ {escape(name)}: {escape(str(exc))}[/]"))
                on(1.0, "")
                return
            tr = mc.Transcript.model_validate(result)
            titled = _titled_out(result, src, out, args, ".txt")
            body = tr.as_text()
            if no_save:
                ephemeral.append(f"{titled.stem}\n\n{body}")
                return
            title = titled.stem  # never a join here: --concat gives one source
            titled.write_text(f"{title}\n\n{body}\n", encoding="utf-8")
            done_rows.append((titled, _words_lang(body, tr.language)))
            translated = _maybe_translate(args, titled, body, title=title, on=p_tr)
            if translated is not None:
                done_rows.append(translated)

        return item

    completed = False
    try:
        _run_batch(_("Transcription"), [make_item(s) for s in todo], runner=runner, local=local)
        completed = True
    finally:
        if ephemeral and (failures or not completed):
            _cli.console.print(Text("\n\n".join(ephemeral)), soft_wrap=True)
        elif ephemeral:
            _deliver_text(
                "\n\n".join(ephemeral),
                Path(),
                detail=_n("{n} file", "{n} files", len(ephemeral)),
                no_save=True,
            )
        # Finished results are shown even when the batch failed halfway.
        for out, detail in done_rows:
            if out is None:
                _cli.console.print(detail)
            else:
                done_panel(out, detail)
    if failures and no_save:
        raise mc.MediaError(
            _(
                "Not transcribed: {failed} of {total}. Files not saved",
                failed=len(failures),
                total=len(todo),
            )
        )
    if failures:
        note = _(
            "Not transcribed: {failed} of {total}. Running the same command again skips the "
            "finished files and does the rest.",
            failed=len(failures),
            total=len(todo),
        )
        _cli.console.print(f"[yellow]{note}[/]")


def _die_if_concat_urls_unsupported(args, sources, cmd: str) -> None:
    """--concat over links with a runner must not silently split into N files.

    Only transcribe joins links with a runner (all inputs links); the others fail with a
    way out (--local or the concat command)."""
    if not getattr(args, "concat", False):
        return
    if any(isinstance(s, str) for s in sources):
        die(
            _(
                "{command} --concat over links is not supported with a runner: download "
                "locally (--local) or join them first with the concat command.",
                command=cmd,
            )
        )


def cmd_mp3(args) -> None:
    runner, local = args.runner, args.local
    sources = _dispatch_sources(args, want_video=False, runner=runner, local=local)
    _die_if_concat_urls_unsupported(args, sources, "mp3")
    if len(sources) > 1:
        _batch_table(_("batch mp3"), [s.name if isinstance(s, Path) else s for s in sources])
        done_rows: list[tuple[Path, str]] = []

        def make_item(src):
            out = _out_path(src, args, len(sources), ".mp3")

            def item(on):
                result = _op_once(
                    "mp3",
                    src,
                    {"bitrate": args.bitrate},
                    runner=runner,
                    local=local,
                    on=on,
                    cookies=_cookies_for(args, [src]),
                )
                fetch_result_file(result.get("path") or "", out, runner=runner, local=local)
                done_rows.append((out, mc.fmt_size(out.stat().st_size)))

            return item

        try:
            _run_batch("MP3", [make_item(s) for s in sources], runner=runner, local=local)
        finally:
            for out, detail in done_rows:
                done_panel(out, detail)
        return
    for src in sources:
        out = _out_path(src, args, len(sources), ".mp3")
        if resolve_runner(runner, local=local) is None and isinstance(src, Path):
            _cli.console.print(info_panel(mc.probe(src)))
        result = _run_op(
            "MP3",
            "mp3",
            src,
            {"bitrate": args.bitrate},
            runner=runner,
            local=local,
            cookies=_cookies_for(args, [src]),
        )
        fetch_result_file(result.get("path") or "", out, runner=runner, local=local)
        done_panel(out, mc.fmt_size(out.stat().st_size))


def _quiet_tty() -> Callable[[], None]:
    """Silence stray terminal output during a live session (rich.Live is fragile).

    Any tty output outside rich shifts the screen and leaves a ghost of the live area in the
    scrollback. Two sources: the macOS kernel status on Ctrl-T ("load: ...") — NOKERNINFO
    hides it, SIGINFO still arrives; and input echo (Enter prints a newline) — ECHO is turned
    off, Ctrl-C and Ctrl-T still signal (ISIG stays). Returns the restore callback (a no-op
    when stdin is not a tty or termios fails).
    """
    try:
        import termios

        if not sys.stdin.isatty():
            return lambda: None
        fd = sys.stdin.fileno()
        attrs = termios.tcgetattr(fd)
        old_lflag = attrs[3]
        attrs[3] |= getattr(termios, "NOKERNINFO", 0)  # BSD/macOS only
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSANOW, attrs)

        def restore() -> None:
            try:
                cur = termios.tcgetattr(fd)
                cur[3] = old_lflag
                termios.tcsetattr(fd, termios.TCSANOW, cur)
            except Exception:  # noqa: BLE001 - the terminal may be gone
                pass

        return restore
    except Exception:  # noqa: BLE001 - an exotic tty: run without silencing
        return lambda: None


@_at_node
def cmd_live(args) -> None:
    """--no-save keeps the audio only temporarily, also on cancel and on error."""
    if args.no_save and args.rec:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="mnema-rec-") as tmp:
            _cmd_live(args, ephemeral_base=Path(tmp))
    else:
        _cmd_live(args)


def _live_text(tracks) -> str:
    return " ".join(
        seg.text
        for track in tracks
        for seg in track.committed
        if not (seg.text.startswith("[") and seg.text.endswith("]"))
    ).strip()


def _cmd_live(args, *, ephemeral_base: Path | None = None) -> None:
    """Capture audio here; recognition and translation on the selected server.

    Building and finishing the session live in live/setup.py, shared with the menu bar app;
    here are the flags, the terminal view and the hints.
    """

    # lazy: the thin client does not import sounddevice or whisperlivekit
    from ..live.setup import (
        LiveConfig,
        build_live,
        live_base,
    )

    lang = args.lang
    translate_arg = args.translate
    if translate_arg and lang:
        note = _(
            'Recognition is pinned to "{lang}" (--lang): a known language decodes cleaner, '
            "but phrases in other languages come out worse. For automatic detection, drop "
            "--lang.",
            lang=lang,
        )
        _cli.console.print(f"[dim]{note}[/]")

    no_save = args.no_save
    if no_save and args.output:
        die(_("--no-save cannot be combined with -o/--output: nothing is saved"))
    config = LiveConfig(
        system=args.system,
        mic=args.mic,
        lang=lang,
        translate=translate_arg,
        # rec: no live text; the whole recording is transcribed on stop
        stream=not args.rec,
        summary=not no_save,
        mix_audio=not no_save,
        # an ephemeral rec still writes WAV (it is transcribed) to a temporary folder; an
        # ephemeral live writes nothing
        record=not args.no_record and (not no_save or args.rec),
        runner=resolve_runner(args.runner, local=args.local),
    )
    if no_save and args.rec:
        assert ephemeral_base is not None
        base = ephemeral_base
    elif no_save:
        # an ephemeral live session creates no folder: nothing records or saves into base
        import tempfile

        base = Path(tempfile.gettempdir()) / "mnema-live-ephemeral"
    else:
        # without -o and MNEMA_LIVE_DIR: recordings/ in the current folder (not "mnema", which
        # inside the repository would be the package)
        from ..config import library_root

        base = live_base(args.output, root=library_root(Path.cwd() / "recordings"))
    prepared = build_live(config, base=base, connection=args.runner)
    try:
        _capture_live(args, prepared)
    finally:
        prepared.close()


def _capture_live(args, prepared) -> None:
    from ..live.recording_client import finalize_on_node
    from ..live.remarks import Step
    from ..live.render import LiveView, PaneView
    from ..live.setup import finish_command, keep_audio_only

    no_save = args.no_save
    from ..client import MnemaClient
    from ..core import base as core_base

    runner_down_hint = None
    if args.runner is None:
        # a Mac processes here; Linux only on a server, so the audio waits for one
        if not core_base.STANDALONE:
            if no_save:
                raise mc.MediaError(_("Server not connected. Files not saved"))
            runner_down_hint = _("server not connected — the audio can be processed later")
    else:
        status = MnemaClient(args.runner.url, token=args.runner.token).health()
        if status is None or status.get("auth") is False:
            if no_save:
                raise mc.MediaError(_("The processing server is unavailable. Files not saved"))
            runner_down_hint = _("server unavailable — the audio can be processed later")
    if runner_down_hint:
        _cli.console.print(f"[yellow]{runner_down_hint}[/]")
    if prepared.dialog is not None:
        # The --translate argument is YOUR language, not the target: spell the directions out,
        # otherwise `--translate en` from a Russian speaker pins the microphone to English.
        d = prepared.dialog
        note = _(
            'Translation: I speak "{mine}" → to the other side in "{theirs}"; their "{theirs}" '
            '→ to me in "{mine}". The --translate argument is your language.',
            mine=d.my_lang,
            theirs=d.their_lang or _("auto"),
        )
        _cli.console.print(f"[dim]{note}[/]")
    # minutes on the command line, seconds in the session; 0 is off
    stop_after_silence = max(0.0, args.stop_after_silence) * 60.0
    auto_hint = (
        " " + _("Auto-stop after {minutes} min of silence.", minutes=f"{args.stop_after_silence:g}")
        if stop_after_silence
        else ""
    )
    # Recording starts at once while recognition warms up in the background; the source's live
    # line shows when it is ready, so "speak" is not printed here.
    if args.rec:
        intro = _(
            "[bold]Recording audio[/] ({status}). No live text — on [bold]Ctrl-C[/] the whole "
            "recording is transcribed.",
            status=prepared.status,
        )
    else:
        intro = _(
            '[bold]Recording audio[/] ({status}). Wait for [bold]"ready"[/] — the engine is '
            "warming up (up to a minute on the first run). Ctrl-T copies the text to the "
            "clipboard, Ctrl-C stops.",
            status=prepared.status,
        )
    _cli.console.print(intro + auto_hint)

    from ..live.clipboard import copy_to_clipboard

    # With translation a two-column full-screen view by default (original left, translation
    # right); --no-panes prints lines.
    translating = prepared.dialog is not None
    view_cls = PaneView if translating and not args.no_panes else LiveView
    view_kwargs: dict = {"translate": translating}
    if view_cls is PaneView:
        # The full-screen view covers what was printed above: repeat the directions at the bottom.
        d = prepared.dialog
        view_kwargs["hint"] = _(
            'Translation: me "{mine}", the other side "{theirs}" (--translate is YOUR '
            'language) · recording audio, the engine is warming up — wait for "ready"',
            mine=d.my_lang,
            theirs=d.their_lang or _("auto"),
        ) + (f" · {runner_down_hint}" if runner_down_hint else "")
    with view_cls(prepared.labels, **view_kwargs) as view:
        session = prepared.make_session(
            view,
            on_copy=copy_to_clipboard,
            stop_after_silence=stop_after_silence,
            save_identity=not args.no_save,
        )
        # Ctrl-T in a macOS terminal sends SIGINFO: copy the session text to the clipboard.
        import contextlib as _ctx
        import signal as _signal

        with _ctx.suppress(AttributeError, ValueError):  # not macOS or not the main thread
            _signal.signal(_signal.SIGINFO, lambda *_args: session.copy_now())
        restore_tty = _quiet_tty()
        from ..live.stderr_filter import suppress_pamaccore

        try:
            # PortAudio's PaMacCore noise on device changes goes to the native stderr: filter
            # it at the fd level.
            with suppress_pamaccore():
                tracks = session.run()
        finally:
            restore_tty()

    recording_error = getattr(session, "recording_error", None)
    prepared.recording_error = recording_error
    if recording_error:
        _cli.console.print(
            Text(_("Recording stopped: {error}", error=recording_error), style="red")
        )

    if no_save and not args.rec:
        full = _live_text(tracks)
        if recording_error or session.asr_failures:
            if full:
                _cli.console.print(Text(full))
            raise mc.MediaError(recording_error or _("Live recognition ended with an error"))
        copied = bool(full) and copy_to_clipboard(full)
        if copied:
            note = _("ephemeral session — nothing saved to disk · 📋 text in the clipboard")
        elif not full:
            note = _("ephemeral session — nothing saved to disk · no recognized text")
        else:
            note = _("ephemeral session — nothing saved to disk")
        _cli.console.print(note)
        if full and not copied:
            # no clipboard (ssh, headless): the terminal is the only place left for the text
            _cli.console.print(Text(full))
        return

    # -o wins over a title from the content, so retitle only without it. A second Ctrl-C
    # (while finishing) stops the processing and keeps the audio for mnema finish.
    try:
        result, full = _cli.run_streaming(
            _("Transcribing") if no_save else _("Saving"),
            lambda on: finalize_on_node(
                prepared,
                tracks,
                connection=args.runner,
                retitle=not args.output and not no_save,
                on=on,
            ),
            total=1.0,
            live_preview=True,
        )
    except KeyboardInterrupt:
        from ..client import cancel_active_job

        cancel_active_job()  # free the runner
        if no_save:
            _cli.console.print(f"[yellow]{_('Processing stopped; files not saved.')}[/]")
            if partial := _live_text(tracks):
                _cli.console.print(Text(partial))
            return
        keep_audio_only(prepared.base)
        _cli.console.print(
            f"[yellow]{_('Processing stopped — audio saved:')}[/] " + str(prepared.base)
        )
        hint(_("Process later:"), finish_command(prepared.base), style="")
        return
    except Exception:
        if no_save:
            if partial := _live_text(tracks):
                _cli.console.print(Text(partial))
            raise
        # the call is intact: say where it is and how to finish it before the traceback
        _cli.console.print(f"[red]{_('Processing failed — audio saved:')}[/] " + str(prepared.base))
        hint(_("Process again:"), finish_command(prepared.base), style="")
        raise
    if no_save:
        for remark in result.remarks:
            _cli.console.print(Text(remark.cause, style="yellow"), soft_wrap=True)
        reason = recording_error or _no_save_failure(result)
        if reason:
            if full:
                _cli.console.print(Text(full))
            raise mc.MediaError(
                _("Transcription incomplete: {reason}. Files not saved", reason=reason)
            )
        _deliver_text(full, prepared.base, detail=_("files not saved"), no_save=True)
        return
    for w in result.refine_warnings:
        _cli.console.print(Text(w, style="yellow"), soft_wrap=True)
    if (
        prepared.dialog is not None
        and result.base is not None
        and any(r.step == Step.TRANSLATE for r in result.remarks)
    ):  # the server's remark has no command: the path is on this disk
        import shlex

        transcript = shlex.quote(str(result.base / "transcript.txt"))
        command = f"mnema translate {transcript} --lang {prepared.dialog.my_lang}"
        hint(_("Translate later:"), command, style="")
    lines = [
        _("transcript rebuilt from the recording")
        if result.refined_labels
        else _("live transcript saved")
    ]
    if result.recordings:
        lines.append(_recording_files(result.recordings))
    # the final text goes to the clipboard on its own (Ctrl-T gave intermediate copies)
    if full and copy_to_clipboard(full):
        lines.append(_("📋 text copied to the clipboard"))
    detail = "\n".join(lines)
    if recording_error:
        warn_panel(prepared.base, _("recording incomplete"), f"{recording_error}\n{detail}")
    elif result.base and (run := meta.last(result.base)) and run.status == "failed":
        warn_panel(prepared.base, _("processing incomplete"), detail)
    elif result.transcript_txt is not None:
        done_panel(result.transcript_txt, detail)
    else:
        # no text, so no empty transcript files; the recording is intact
        warn_lines = [_("transcription gave no text — no transcript created")]
        if result.recordings:
            warn_lines.append(_recording_files(result.recordings))
        warn_panel(prepared.base, _("recording saved without a transcript"), "\n".join(warn_lines))
    # no transcript, or one cut short by recognition failures: suggest transcribing the audio
    failures = getattr(session, "asr_failures", {})
    wavs = [p for p in result.recordings if p.suffix == ".wav"]
    if wavs and (failures or not full) and not result.refined_labels:
        cmd = "mnema transcribe " + " ".join(str(p) for p in wavs)
        hint(_("Live transcript incomplete — transcribe the recording:"), cmd, style="")
    if recording_error:
        raise mc.MediaError(
            _(
                "Recording incomplete. The available audio and text are saved: {path}",
                path=prepared.base,
            )
        )


@_at_node
def cmd_finish(args) -> None:
    """Process a recording folder (interrupted, stopped or cut) with the same steps as rec on
    stop: transcribe the WAV tracks, title, summary."""
    from ..live.recording_client import recover_on_node
    from ..live.setup import (
        LiveConfig,
        finish_command,
        keep_audio_only,
        parse_offset,
    )

    base = Path(args.path).expanduser()
    if not base.is_dir():
        raise mc.MediaError(_("No recording folder: {path}", path=base))
    start = parse_offset(args.start) if getattr(args, "start", None) else None
    end = parse_offset(args.end) if getattr(args, "end", None) else None
    config = LiveConfig(
        lang=args.lang,
        runner=resolve_runner(args.runner, local=args.local),
    )
    try:
        result, full = _cli.run_streaming(
            _("Processing recording"),
            lambda on: recover_on_node(base, config, args.runner, start=start, end=end, on=on),
            total=1.0,
        )
    except KeyboardInterrupt as e:
        from ..client import cancel_active_job

        cancel_active_job()
        base = getattr(e, "mnema_base", base)  # the folder may have been renamed
        keep_audio_only(base)
        _cli.console.print(f"[yellow]{_('Processing stopped, audio saved:')}[/] " + str(base))
        hint(_("Process later:"), finish_command(base), style="")
        return
    for note in result.info:  # for reference: a part was cut, a header repaired
        _cli.console.print(Text(note, style="dim"), soft_wrap=True)
    for w in result.refine_warnings:
        _cli.console.print(Text(w, style="yellow"), soft_wrap=True)
    if result.transcript_txt is not None:
        detail = (
            _("summary: {name}", name=result.summary_md.name)
            if result.summary_md
            else _("no summary")
        )
        done_panel(result.transcript_txt, detail)
    else:
        warn_panel(base, _("no transcript"), _("audio saved"))


def cmd_compress(args) -> None:
    runner, local = args.runner, args.local
    sources = _dispatch_sources(args, want_video=True, runner=runner, local=local)
    _die_if_concat_urls_unsupported(args, sources, "compress")
    if len(sources) > 1:
        _batch_table(
            _("batch compression"), [s.name if isinstance(s, Path) else s for s in sources]
        )
        done_rows: list[tuple[Path, str]] = []

        def make_item(src):
            out = _out_path(
                src, args, len(sources), lambda s: s.with_name(s.stem + "_compressed.mov")
            )
            orig = src.stat().st_size if isinstance(src, Path) else 0

            def item(on):
                params = {"fps": args.fps, "quality": args.quality}
                result = _op_once(
                    "compress",
                    src,
                    params,
                    runner=runner,
                    local=local,
                    on=on,
                    cookies=_cookies_for(args, [src]),
                )
                fetch_result_file(result.get("path") or "", out, runner=runner, local=local)
                size = out.stat().st_size
                done_rows.append((out, _compressed(orig, size)))

            return item

        try:
            _run_batch(
                _("Compression"), [make_item(s) for s in sources], runner=runner, local=local
            )
        finally:
            for out, detail in done_rows:
                done_panel(out, detail)
        return
    for src in sources:
        label = _("Compression")
        out = _out_path(src, args, len(sources), lambda s: s.with_name(s.stem + "_compressed.mov"))
        # a file is on this disk even with a runner; a link has no local original size
        orig = src.stat().st_size if isinstance(src, Path) else 0
        if resolve_runner(runner, local=local) is None and isinstance(src, Path):
            _cli.console.print(info_panel(mc.probe(src)))
        params = {"fps": args.fps, "quality": args.quality}
        result = _run_op(
            label,
            "compress",
            src,
            params,
            runner=runner,
            local=local,
            cookies=_cookies_for(args, [src]),
        )
        fetch_result_file(result.get("path") or "", out, runner=runner, local=local)
        done_panel(out, _compressed(orig, out.stat().st_size))


def cmd_concat(args) -> None:
    """Joining runs on this computer: links are downloaded here, every install has ffmpeg."""
    files = _sources(args, want_video=True)
    # in the user's order
    t = Table(box=box.SIMPLE_HEAVY, title=_("join order"), title_style=f"bold {ACCENT}")
    t.add_column("#", style=ACCENT, justify="right")
    t.add_column(_("file"))
    for i, f in enumerate(files, 1):
        t.add_row(str(i), Text(f.name))  # Text: yt-dlp names carry [brackets]
    _cli.console.print(t)
    if args.output:
        out = Path(args.output).expanduser()
    else:
        # downloaded inputs are temporary: the result goes to the current folder
        from .sources import _DOWNLOADED, _merged_name

        name = _merged_name(files)
        out = (Path.cwd() / name) if files[0] in _DOWNLOADED else files[0].with_name(name)
    result = _cli.run_with_progress(
        _("Joining"), lambda on: mc.merge(files, out, on=on), total=None
    )
    done_panel(
        out,
        _n(
            "{n} file · {duration} · {size}",
            "{n} files · {duration} · {size}",
            len(files),
            duration=mc.fmt_duration(result.duration),
            size=mc.fmt_size(result.size),
        ),
    )


def cmd_merge(args) -> None:
    """Mix the synchronous audio tracks of one call (mic.wav + system.wav, common t=0) into one
    track (ffmpeg amix). Audio only: `transcribe --merge` makes a transcript of parallel tracks."""
    files = _sources(args, want_video=False)
    if len(files) < 2:
        die(_("merge mixes several tracks — give at least two (e.g. mic.wav system.wav)."))
    if args.output:
        out = Path(args.output).expanduser()
    else:
        stems = "+".join(dict.fromkeys(f.stem for f in files))[:60]
        name = f"{stems or 'mixed'}.m4a"
        # downloaded inputs sit in the link cache: the result goes to the current folder
        from .sources import _DOWNLOADED

        out = (Path.cwd() / name) if files[0] in _DOWNLOADED else files[0].with_name(name)
    _cli.run_with_progress(_("Mixing"), lambda on: mc.mix(files, out, on=on), total=None)
    size = mc.fmt_size(out.stat().st_size)
    done_panel(out, _n("{n} track · {size}", "{n} tracks · {size}", len(files), size=size))


def _transcribe_merge(args, sources: list[Path | str], *, runner: str | None, local: bool) -> None:
    """transcribe --merge: parallel tracks of one call → one transcript interleaved by time,
    labelled by file name (mic/system). One track is a valid input too."""
    merged: list[mc.Segment] = []
    lang: str | None = None
    todo: list = []
    for src in sources:
        # locally a file without audio is skipped; remotely the runner decides
        if resolve_runner(runner, local=local) is None and isinstance(src, Path):
            if not mc.probe(src).has_audio:
                _no_audio(src)
                continue
        todo.append(src)

    def make_track(src):
        stem = (src.stem if isinstance(src, Path) else _url_stem(src)) or "track"

        def item(on):
            nonlocal lang
            params = _transcribe_params(args)
            result = _op_once(
                "transcribe",
                src,
                params,
                runner=runner,
                local=local,
                on=on,
                cookies=_cookies_for(args, [src]),
            )
            tr = mc.Transcript.model_validate(result)
            lang = lang or tr.language
            # each segment is labelled with its track name (mic/system)
            merged.extend(
                mc.Segment(
                    start=s.start,
                    end=s.end,
                    text=s.text,
                    speaker=stem,
                )
                for s in tr.segments
            )

        return item

    if todo:
        _run_batch(_("Tracks"), [make_track(s) for s in todo], runner=runner, local=local)

    if not merged:
        die(_("No track gave any text."))
    merged.sort(key=lambda s: s.start)  # synchronous tracks share t=0
    body = mc.Transcript(
        text=" ".join(s.text for s in merged).strip(), segments=merged, language=lang
    ).with_speakers()

    file_sources = [s for s in sources if isinstance(s, Path)]
    if args.output:
        out = Path(args.output).expanduser()
    elif file_sources:
        stems = "+".join(dict.fromkeys(s.stem for s in file_sources))[:60]
        out = file_sources[0].with_name(f"{stems or 'merged'}.txt")
    else:
        out = Path.cwd() / "merged.txt"
    no_save = getattr(args, "no_save", False)
    _deliver_text(
        f"{out.stem}\n\n{body}" if not no_save else body,
        out,
        detail=_n(
            "{n} word · tracks: {tracks} · language: {lang}",
            "{n} words · tracks: {tracks} · language: {lang}",
            len(body.split()),
            tracks=len(sources),
            lang=lang or "—",
        ),
        no_save=no_save,
    )


def _translate_params(args) -> ops.TranslateParams:
    """Parameters of the translate op from argparse (--lang → to_lang)."""
    return ops.TranslateParams(to_lang=args.lang)


@_at_node
def cmd_translate(args) -> None:
    runner = args.runner
    local = args.local
    no_save = getattr(args, "no_save", False)
    if no_save and args.output:
        die(_(_NO_SAVE_AND_OUTPUT))
    sources = _dispatch_sources(args, want_video=False, runner=runner, local=local)
    batch = len(sources) > 1
    ephemeral: list[str] = []
    completed = False
    try:
        for src in sources:
            out = _out_path(src, args, len(sources), f".{args.lang}.md")
            kw = _translate_params(args).model_dump(exclude_none=True)
            result = _run_op(
                _("Translation"),
                "translate",
                src,
                kw,
                runner=runner,
                local=local,
                cookies=_cookies_for(args, [src]),
            )
            text = result["markdown"]
            out = _titled_out(result, src, out, args, f".{args.lang}.md")
            if no_save and batch:
                ephemeral.append(f"{out.stem}\n\n{text}")
            else:
                _deliver_text(
                    text,
                    out,
                    detail=_words_to(text, args.lang),
                    no_save=no_save,
                )
        completed = True
    finally:
        if ephemeral and not completed:
            _cli.console.print(Text("\n\n".join(ephemeral)), soft_wrap=True)
        elif ephemeral:
            _deliver_text(
                "\n\n".join(ephemeral),
                Path(),
                detail=_n(
                    "{n} file · {lang}", "{n} files · {lang}", len(ephemeral), lang=args.lang
                ),
                no_save=True,
            )


def _maybe_translate(
    args, base_out: Path, text: str, *, title: str | None, on=None
) -> tuple | None:
    """With --translate, translate to Russian and save .ru.md next to the file.

    The title line is metadata, not content: it is not translated and heads the .ru.md as in
    the .txt."""
    if not args.translate:
        return None
    to = "ru"
    out = base_out.with_suffix(f".{to}.md")
    head = f"{title}\n\n" if title else ""

    def translate(progress):
        import tempfile

        with tempfile.TemporaryDirectory(prefix="mnema-translate-") as folder:
            path = Path(folder) / "transcript.txt"
            path.write_text(text, encoding="utf-8")
            result = _op_once(
                "translate",
                path,
                {"to_lang": to},
                runner=args.runner,
                local=False,
                on=progress,
            )
            return result["markdown"]

    try:
        if on is not None:
            # a slice of the batch bar: no Live of its own; the result is printed after the bar
            translated = translate(on)
            out.write_text(f"{head}{translated}\n", encoding="utf-8")
            return (out, _words_to(translated, to))
        translated = _cli.run_with_progress(
            _("Translation"),
            translate,
            total=None,
            live_preview=True,
        )
    except Exception:  # noqa: BLE001 — the transcript is already on disk
        if on is not None:
            # in a batch the warning is printed after the bar; out=None makes it a yellow
            # line rather than a second "done" for the same .txt
            failed = _("translating {name} failed (is the LLM unavailable?)", name=base_out.name)
            return (None, f"[yellow]{failed}[/]")
        failed = _(
            "Translation failed (is the LLM unavailable?) — the transcript is saved, translation "
            "skipped."
        )
        _cli.console.print(f"[red]{failed}[/]")
        return None
    out.write_text(f"{head}{translated}\n", encoding="utf-8")
    done_panel(out, _words_to(translated, to))
    return None


def _configure_runner_logging(level_name: str) -> None:
    """ "mnema.*" logs to stderr, the service journal, at the given level (INFO by default).

    uvicorn runs with log_level=warning to hide its access log, so our INFO would be lost
    otherwise; propagate=False avoids duplicates through the root logger."""

    level = getattr(logging, level_name.upper(), logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S")
    )
    mlog = logging.getLogger("mnema")
    mlog.setLevel(level)
    mlog.addHandler(handler)
    mlog.propagate = False


def cmd_serve(args) -> None:
    """Start the headless runner API: the server for the CLI and the apps."""

    from ..config import Settings
    from ..core.base import MediaError
    from ..install_setup import DEFAULT_PORT, server_token

    try:
        import fastapi  # noqa: F401 - the Mac app has uvicorn (through mcp) but no fastapi
        import uvicorn
    except ImportError:
        die(_("The server needs the full extra: uv sync --extra full"))
    from ..server import create_app, require_safe_bind

    token = server_token()
    # The guard lives in server.py for every caller of create_app; here it becomes a CLI error.
    try:
        require_safe_bind(args.host, token)
    except MediaError as e:
        die(str(e))
    port = args.port or DEFAULT_PORT
    if not token:
        # A proxy may publish a loopback bind without its own auth, which the guard cannot
        # see: always warn, but loopback without a token is the usual dev setup.
        warning = _("MNEMA_TOKEN is not set — the API is open without authorization!")
        _cli.console.print(f"[yellow]{warning}[/]")

    root = Path(args.dir).expanduser() if args.dir else Path.cwd()
    from ..users import set_strict

    set_strict(True)  # a model used outside a user's request would spend the owner's key
    app = create_app(root)
    shown_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    url = f"http://{shown_host}:{port}"
    if Settings().container:  # devices reach the published port, not the container's own
        from ..node import node_url

        url = node_url(port) or url
    _cli.console.print(
        _("[{accent}]Mnema server[/]: {url}, folder {root}", accent=ACCENT, url=url, root=root)
    )
    if token:
        _cli.console.print(_("[{accent}]🔒 token sign-in[/] (server token)", accent=ACCENT))
    _configure_runner_logging(Settings().log_level)
    uvicorn.run(app, host=args.host, port=port, log_level="warning")


def _node_root(args) -> Path:
    """The server folder: it must hold .mnema, otherwise the server never sees the invite."""
    from ..install_setup import server_root

    root = Path(args.dir).expanduser() if getattr(args, "dir", None) else server_root()
    if not (root / ".mnema").is_dir():
        die(_("No server in the folder {root}", root=root))
    return root


def _invite_url(args, root: Path) -> str:
    """The address in an invitation: --url (remembered for this server), otherwise
    node.node_url; another folder (--dir, a development server) needs --url and remembers
    nothing."""
    from ..install_setup import DEFAULT_PORT, save_url, server_root
    from ..node import node_url

    own = root.resolve() == server_root().resolve()
    url = args.url
    try:
        if url and own:
            url = save_url(url)
        elif own:
            url = node_url(DEFAULT_PORT)
    except ValueError as e:
        die(str(e))
    if not url:
        die(_("Give --url: the address the clients reach the server at"))
    return url or ""


def _user_name(raw: str) -> str:
    from ..core.base import MediaError
    from ..users import valid_name

    try:
        return valid_name(raw)
    except MediaError as e:
        die(str(e))
        return ""


def _need_server_token() -> None:
    from ..install_setup import server_token

    if not server_token():  # without it every device is the owner's
        die(_("The server has no token: users need one"))


def cmd_invite(args) -> None:
    """An invite for one device of the owner or of --user, on the server machine itself
    without HTTP."""
    import os
    import time

    from ..devices import issue_invite
    from ..users import OWNER

    os.umask(0o077)  # docker exec does not inherit the server's umask
    root = _node_root(args)
    user = OWNER
    if args.user is not None and args.user.lower() != OWNER:
        _need_server_token()
        user = _user_name(args.user)
    url = _invite_url(args, root)
    try:
        text = issue_invite(root, url, user=user, hours=args.hours, now=time.time())
    except ValueError as e:
        die(str(e))
        return
    print(text)


def cmd_users(args) -> None:
    """Users of the server: each has own devices and own library. remove revokes the devices
    and invitations, the recordings stay; add of a removed name brings them back."""
    import os
    from collections import Counter

    from ..devices import DeviceStore, devices_db
    from ..users import OWNER, no_user

    os.umask(0o077)  # docker exec does not inherit the server's umask
    root = _node_root(args)
    store = DeviceStore(devices_db(root))
    if args.action == "add":
        _add_user(args, root, store)
        return
    if args.action == "remove":
        if args.name.lower() == OWNER:
            die(_("The server owner cannot be removed"))
        name = _user_name(args.name)
        if not store.remove_user(name):
            die(no_user(name))
        print(_("User {name} removed, the recordings stay", name=name))
        return
    from ..llm.config import user_settings

    devices = Counter(d.user for d in store.devices() if not d.revoked)
    table = Table(box=box.SIMPLE)
    for column in (_("Name"), _("Devices"), _("Model")):
        table.add_column(column)
    for user in store.users():
        model = user_settings(user.name).model or _("no model")
        table.add_row(Text(user.name), str(devices[user.name]), Text(model))
    _cli.console.print(table)


def _add_user(args, root: Path, store) -> None:
    import time

    from ..devices import check_invite, issue_invite
    from ..library.disk import LibraryError
    from ..library.files import LibraryFiles
    from ..node import server_command
    from ..users import user_library

    _need_server_token()
    name = _user_name(args.name)
    url = _invite_url(args, root)
    try:
        check_invite(url, args.hours)
    except ValueError as e:
        die(str(e))
    # the folder before the user: a failed mkdir leaves no user without a library
    library = user_library(root, name)
    library.mkdir(mode=0o700, parents=True, exist_ok=True)
    library.chmod(0o700)
    try:
        _user, returned = store.add_user(name, now=time.time())
    except ValueError:
        command = server_command(f"invite --user {name}")
        die(_("User {name} already exists. Invitation: {command}", name=name, command=command))
        return
    if returned:
        try:
            count = len(LibraryFiles(library).manifest().records)
        except LibraryError:
            print(_("User {name} is back", name=name))
        else:
            print(
                _n(
                    "User {name} is back with {n} recording",
                    "User {name} is back with {n} recordings",
                    count,
                    name=name,
                )
            )
    print(issue_invite(root, url, user=name, hours=args.hours, now=time.time()))


def cmd_devices(args) -> None:
    """Devices connected to the server; revoke cancels a device token at once."""
    import time

    from ..devices import DeviceStore, devices_db

    store = DeviceStore(devices_db(_node_root(args)))
    active = [d for d in store.devices() if not d.revoked]
    if args.action == "revoke":
        device = next((d for d in active if d.id == args.id), None)
        if device is None or not store.revoke(device.id):
            die(_("No connected device {id}", id=args.id))
            return
        _cli.console.print(
            Text(_("Revoked: {name} {id}", name=device.name, id=device.id)), soft_wrap=True
        )
        return

    def when(stamp: float | None) -> str:
        return time.strftime(_("%Y-%m-%d %H:%M"), time.localtime(stamp)) if stamp else _("never")

    if not active:
        from ..node import server_command

        _cli.console.print(
            _("No connected devices. New invitation: {command}", command=server_command("invite"))
        )
        return
    table = Table(box=box.SIMPLE)
    for column in (_("Name"), "id", _("User"), _("Added"), _("Last seen")):
        table.add_column(column)
    for d in active:
        table.add_row(Text(d.name), d.id, d.user, when(d.created), when(d.last_seen))
    _cli.console.print(table)


def cmd_update(args) -> None:
    from ..update import main

    main()
