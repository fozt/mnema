"""Video operations: compression and merging."""

from __future__ import annotations

import functools
import os
import platform
import re
import selectors
import subprocess
import tempfile
from pathlib import Path

from ..i18n import _
from .base import MISSING_TOOL, MediaError, Progress, fmt_duration, noop, run, stop_process, tool
from .probe import MediaInfo, probe


@functools.lru_cache(maxsize=1)
def _ffmpeg_encoders() -> frozenset[str]:
    """Encoder names from `ffmpeg -encoders` (cached), to pick a hardware encoder."""
    try:
        out = subprocess.run(
            [tool("ffmpeg"), "-hide_banner", "-encoders"], capture_output=True, text=True
        ).stdout
    except (FileNotFoundError, MediaError):
        return frozenset()
    return frozenset(re.findall(r"^\s*[A-Z.]{6}\s+(\S+)", out, re.M))


# The Mac app's LGPL ffmpeg has no libopus/libvorbis: their native encoders are experimental.
_NATIVE_AUDIO = {"libopus": "opus", "libvorbis": "vorbis"}


def _audio_codec(name: str) -> list[str]:
    """`-c:a` options for an encoder, or for its native stand-in when this ffmpeg lacks it."""
    native = _NATIVE_AUDIO.get(name)
    enc = _ffmpeg_encoders()
    if native and name not in enc and native in enc:
        return ["-c:a", native, "-strict", "experimental"]
    return ["-c:a", name]


def _hevc_profile(quality: int) -> tuple[list[str], list[str]]:
    """(decode options, codec and quality options) for this platform.

    Hardware: VideoToolbox (Mac) or NVENC (CUDA server), else CPU libx265. Decoding is on the
    GPU too (the bottleneck at 4K); frames are copied to RAM so the CPU fps filter still works.
    `quality` 0..100 (higher is better) → CRF/CQ 0..51 (lower is better).
    """
    enc = _ffmpeg_encoders()
    crf = round((100 - quality) * 0.5)
    if platform.system() == "Darwin" and "hevc_videotoolbox" in enc:
        # -prio_speed: VideoToolbox speed mode (~20% faster, smaller file)
        return ["-hwaccel", "videotoolbox"], [
            "-c:v",
            "hevc_videotoolbox",
            "-q:v",
            str(quality),
            "-prio_speed",
            "1",
        ]
    if "hevc_nvenc" in enc:
        return ["-hwaccel", "cuda"], [
            "-c:v",
            "hevc_nvenc",
            "-preset",
            "p4",
            "-rc",
            "vbr",
            "-cq",
            str(crf),
        ]
    return [], ["-c:v", "libx265", "-preset", "fast", "-crf", str(crf)]


def _ffmpeg_with_progress(cmd: list[str], *, total: float, on: Progress) -> None:
    """Run ffmpeg and turn `-progress` (out_time_us) into a time-based progress bar.

    `-nostats -progress pipe:1` are global options, so they go right after the binary name.
    """
    on(None, "")  # cancellation before start, the CPU fallback included
    full = [cmd[0], "-nostats", "-progress", "pipe:1", *cmd[1:]]
    # stderr goes to a temp file, not a PIPE: only stdout is read, so a full stderr pipe
    # would deadlock a long ffmpeg run.
    with tempfile.TemporaryFile("w+") as errf:
        try:
            proc = subprocess.Popen(
                full, stdout=subprocess.PIPE, stderr=errf, stdin=subprocess.DEVNULL
            )
        except FileNotFoundError as e:
            raise MediaError(_(MISSING_TOOL, program=Path(cmd[0]).name)) from e
        try:
            assert proc.stdout is not None
            with selectors.DefaultSelector() as ready:
                ready.register(proc.stdout, selectors.EVENT_READ)
                pending = b""
                while ready.get_map():
                    on(None, "")
                    for key, _events in ready.select(timeout=0.2):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            ready.unregister(key.fileobj)
                            continue
                        lines = (pending + chunk).split(b"\n")
                        pending = lines.pop()
                        for line in lines:
                            if line.startswith(b"out_time_us=") and total:
                                try:
                                    us = int(line.strip().split(b"=", 1)[1])
                                except ValueError:
                                    continue
                                on(min(us / 1_000_000 / total, 1.0), "")
            while True:
                try:
                    proc.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    on(None, "")
        finally:
            stop_process(proc)
            if proc.stdout is not None:
                proc.stdout.close()
        if proc.returncode != 0:
            errf.seek(0)
            raise MediaError(
                _(
                    "{command} → exit code {code}\n{output}",
                    command=Path(cmd[0]).name,
                    code=proc.returncode,
                    output=errf.read().strip()[-800:],
                )
            )


def compress(
    src: Path,
    dst: Path,
    *,
    fps: int = 30,
    quality: int = 60,
    on: Progress = noop,
) -> MediaInfo:
    """Visually lossless HEVC, in hardware (VideoToolbox/NVENC) or on CPU; caps fps if needed."""
    info = probe(src)
    if not info.has_video:
        raise MediaError(_("The file has no video track: {name}", name=src.name))

    on(None, _("Compressing (HEVC)…"))
    vf = ["-vf", f"fps={fps}"] if (info.fps and fps and info.fps > fps) else []
    hwaccel, codec = _hevc_profile(quality)
    cmd = [
        tool("ffmpeg"),
        *hwaccel,
        "-y",
        "-i",
        str(src),
        *vf,
        *codec,
        "-tag:v",
        "hvc1",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        str(dst),
    ]
    try:
        _ffmpeg_with_progress(cmd, total=info.duration, on=on)
    except MediaError:
        # Universal fallback: CPU libx265 without hardware decoding works everywhere
        # (exotic source codec, unsupported -q:v, no GPU).
        crf = round((100 - quality) * 0.5)
        _ffmpeg_with_progress(
            [
                tool("ffmpeg"),
                "-y",
                "-i",
                str(src),
                *vf,
                "-c:v",
                "libx265",
                "-preset",
                "fast",
                "-crf",
                str(crf),
                "-tag:v",
                "hvc1",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                str(dst),
            ],
            total=info.duration,
            on=on,
        )
    return probe(dst)


def _streams_match(infos: list[MediaInfo]) -> bool:
    f = infos[0]
    return all(
        i.video_codec == f.video_codec
        and i.width == f.width
        and i.height == f.height
        and i.audio_codec == f.audio_codec
        for i in infos[1:]
    )


def _concat_list(files: list[Path]) -> str:
    """The ffmpeg concat list. Single quotes are escaped as '\\'', or a name with an apostrophe
    (common in yt-dlp titles, "Alice's talk") breaks the whole merge."""
    lines = []
    for f in files:
        escaped = str(f.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'\n")
    return "".join(lines)


def _assert_merge_complete(dst: Path, expected: float) -> MediaInfo:
    """Guard against silent loss: the merged length is checked against the sum of the parts.

    ffmpeg can "succeed" while dropping a piece (concat demuxer on mismatched streams): exit
    code 0, the file exists, half the recording is gone. A shortfall over 2% (+1 s of slack
    for container boundaries) is a loud error."""
    res = probe(dst)
    if expected and res.duration and res.duration < expected * 0.98 - 1.0:
        raise MediaError(
            _(
                "The merge lost material: the result is {result} instead of ~{expected}. "
                "The file is unusable — please report this.",
                result=fmt_duration(res.duration),
                expected=fmt_duration(expected),
            )
        )
    return res


def merge(files: list[Path], dst: Path, *, on: Progress = noop) -> MediaInfo:
    if len(files) < 2:
        raise MediaError(_("Merging needs at least 2 files."))
    infos = [probe(f) for f in files]
    expected = sum(i.duration or 0.0 for i in infos)
    on(None, _("Merging {n} files…", n=len(files)))

    with tempfile.TemporaryDirectory() as tmp:
        listfile = Path(tmp) / "concat.txt"
        listfile.write_text(_concat_list(files), encoding="utf-8")
        if _streams_match(infos):
            try:
                run(
                    [
                        tool("ffmpeg"),
                        "-y",
                        "-f",
                        "concat",
                        "-safe",
                        "0",
                        "-i",
                        str(listfile),
                        "-c",
                        "copy",
                        str(dst),
                    ],
                    on=on,
                )
                res = probe(dst)
                if not (expected and res.duration and res.duration < expected * 0.98 - 1.0):
                    return res
                # the demuxer "succeeded" and lost a piece: re-encode with the filter
                on(None, _("The stream-copy merge lost part of the material — re-encoding…"))
            except MediaError:
                pass  # parameters differ: re-encode
        total = sum(i.duration or 0.0 for i in infos)
        on(
            None,
            _(
                "The parts' codecs or parameters differ — re-encoding {duration} "
                "(stream copy cannot merge them; slow for hours of material)…",
                duration=fmt_duration(total),
            ),
        )
        # The audio codec follows the output container: .mp3 has no aac, .webm only opus/vorbis
        _by_suffix = {
            ".mp3": "libmp3lame",
            ".webm": "libopus",
            ".opus": "libopus",
            ".ogg": "libvorbis",
        }
        audio_codec = _by_suffix.get(dst.suffix.lower(), "aac")
        if not any(i.has_video for i in infos):
            # Audio only with different codecs (mp3+aac): the concat demuxer silently drops
            # pieces (it needs identical streams), so use the concat filter and normalize every
            # input (rate, channels).
            n = len(files)
            inputs: list[str] = []
            for f in files:
                inputs += ["-i", str(f)]
            norm = "".join(
                f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}];" for i in range(n)
            )
            chain = "".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[out]"
            _ffmpeg_with_progress(
                [
                    tool("ffmpeg"),
                    "-y",
                    *inputs,
                    "-filter_complex",
                    norm + chain,
                    "-map",
                    "[out]",
                    *_audio_codec(audio_codec),
                    "-b:a",
                    "128k",
                    str(dst),
                ],
                total=total,
                on=on,
            )
            return _assert_merge_complete(dst, expected)
        # Video with mismatched parameters: the demuxer drops pieces here too (two clips of
        # different sizes gave one). The concat filter scales and pads every input to the first
        # one's canvas and fps, and normalizes the audio.
        if dst.suffix.lower() in (".m4a", ".mp3", ".ogg", ".opus", ".oga", ".wav"):
            raise MediaError(
                _(
                    "“{name}” is an audio container, but some parts are video: "
                    "choose a video output (such as .mp4) or the merge command for an audio mix.",
                    name=dst.name,
                )
            )
        # An audio file among videos has no [i:v], and the filter would fail cryptically.
        no_video = [i.path.name for i in infos if not i.has_video]
        if no_video:
            raise MediaError(
                _(
                    "Merging video with audio-only files is not supported (no video stream: "
                    "{files}). Merge them separately, or extract the audio as mp3 and merge that.",
                    files=", ".join(no_video[:3]),
                )
            )
        first = infos[0]
        w, h = first.width or 1280, first.height or 720
        fps = int(first.fps or 30)
        n = len(files)
        inputs = []
        for f in files:
            inputs += ["-i", str(f)]
        with_audio = all(i.has_audio for i in infos)
        if not with_audio and any(i.has_audio for i in infos):
            on(None, _("Some files have no audio track — merging without sound"))
        parts = []
        for i in range(n):
            parts.append(
                f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps={fps},setsar=1[v{i}];"
            )
            if with_audio:
                parts.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}];")
        chain = "".join(f"[v{i}]" + (f"[a{i}]" if with_audio else "") for i in range(n))
        chain += f"concat=n={n}:v=1:a={int(with_audio)}[v]" + ("[a]" if with_audio else "")
        maps = ["-map", "[v]"] + (["-map", "[a]"] if with_audio else [])
        # The video codec follows the container too: WebM takes only VP8/VP9/AV1, not HEVC.
        if dst.suffix.lower() == ".webm":
            video_args = ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "34"]
        else:
            # codec only: hardware decoding does not mix with filter_complex
            codec = _hevc_profile(quality=60)[1]
            video_args = [*codec, "-tag:v", "hvc1"]
        cmd = [
            tool("ffmpeg"),
            "-y",
            *inputs,
            "-filter_complex",
            "".join(parts) + chain,
            *maps,
            *video_args,
        ]
        if with_audio:
            cmd += [*_audio_codec(audio_codec), "-b:a", "128k"]
        cmd.append(str(dst))
        _ffmpeg_with_progress(cmd, total=total, on=on)
    return _assert_merge_complete(dst, expected)
