"""The argparse parser of the CLI. Help texts are translated when the parser is built."""

from __future__ import annotations

import argparse

from .. import __version__
from .. import core as mc
from ..i18n import _
from .commands import (
    cmd_compress,
    cmd_concat,
    cmd_devices,
    cmd_finish,
    cmd_invite,
    cmd_live,
    cmd_merge,
    cmd_mp3,
    cmd_serve,
    cmd_transcribe,
    cmd_translate,
    cmd_update,
)
from .model import cmd_model


class _Parser(argparse.ArgumentParser):
    """Whole flags only: a typo is an error, not some other flag. Subparsers use this class too."""

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="mnema", description=_("Process meeting recordings."))
    p.add_argument("--version", action="version", version=f"mnema {__version__}")
    sub = p.add_subparsers(dest="cmd")

    def cookies_flag(sp):
        g = sp.add_mutually_exclusive_group()
        g.add_argument(
            "--cookies",
            metavar=_("FILE"),
            help=_("Netscape cookies.txt for links behind a login"),
        )
        g.add_argument(
            "--cookies-from-browser",
            dest="cookies_from_browser",
            metavar=_("BROWSER"),
            help=_(
                "download links with the browser's cookies for content behind a login "
                "(chrome/safari/firefox; a profile: chrome:Profile 1)"
            ),
        )

    def io(sp):
        sp.add_argument(
            "inputs",
            nargs="+",
            help=_("files, folders or links (at least one; folders are read recursively)"),
        )
        sp.add_argument(
            "-o", "--output", help=_("output path; for several inputs, the output folder")
        )
        sp.add_argument(
            "--concat",
            action="store_true",
            help=_("join the inputs into one recording before processing"),
        )
        cookies_flag(sp)

    def runner_flags(sp):
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--runner", metavar="URL", help=_("URL of the mnema runner to process on"))
        g.add_argument(
            "--local",
            action="store_true",
            help=_("process on this computer (ignoring MNEMA_RUNNER)"),
        )

    sp = sub.add_parser("transcribe", help=_("transcribe to text"))
    io(sp)
    sp.add_argument(
        "--merge",
        action="store_true",
        help=_(
            "the inputs are parallel tracks of one call (mic + system): transcribe them "
            "and interleave by time into one transcript"
        ),
    )
    sp.add_argument(
        "--lang",
        metavar="LANG",
        help=_("recognition language (e.g. ru); automatic without it"),
    )
    sp.add_argument(
        "--backend",
        choices=mc.backend_choices(),
        default=mc.AUTO,
    )
    sp.add_argument("--model")

    sp.add_argument(
        "--translate",
        action="store_true",
        help=_("translate to Russian after transcribing (other languages: the translate command)"),
    )
    sp.add_argument(
        "--no-save",
        dest="no_save",
        action="store_true",
        help=_("save no file: the result goes to the clipboard (several inputs: to the console)"),
    )
    runner_flags(sp)
    sp.set_defaults(func=cmd_transcribe)

    sp = sub.add_parser("mp3", help=_("extract the audio to mp3"))
    io(sp)
    sp.add_argument("--bitrate", default="128k")
    runner_flags(sp)
    sp.set_defaults(func=cmd_mp3)

    sp = sub.add_parser("compress", help=_("compress video (HEVC)"))
    io(sp)
    sp.add_argument("--fps", type=int, default=30)
    sp.add_argument("--quality", type=int, default=60)
    runner_flags(sp)
    sp.set_defaults(func=cmd_compress)

    # concat runs here: links are downloaded on this computer, then ffmpeg; no server needed
    sp = sub.add_parser(
        "concat",
        help=_("join several recordings into one file"),
    )
    sp.add_argument("inputs", nargs="+", help=_("files, folders or links to join"))
    sp.add_argument("-o", "--output")
    cookies_flag(sp)
    sp.set_defaults(func=cmd_concat)

    # merge mixes synchronous audio tracks (mic + system, common t=0) into one track (amix).
    # It is about audio; a transcript of parallel tracks is transcribe --merge.
    sp = sub.add_parser(
        "merge", help=_("mix the synchronous audio tracks of one call into one (mic + system)")
    )
    sp.add_argument(
        "inputs",
        nargs="+",
        help=_("synchronous tracks of one call (mic.wav system.wav) starting together"),
    )
    sp.add_argument(
        "-o", "--output", help=_("path of the mixed track (by default next to the inputs, .m4a)")
    )
    cookies_flag(sp)
    sp.set_defaults(func=cmd_merge)

    sp = sub.add_parser(
        "translate", help=_("translate a transcript with the model of the selected server")
    )
    io(sp)
    sp.add_argument(
        "--lang", default="ru", metavar="LANG", help=_("target language (ru by default)")
    )
    sp.add_argument(
        "--no-save",
        dest="no_save",
        action="store_true",
        help=_("save no file: the result goes to the clipboard (several inputs: to the console)"),
    )
    runner_flags(sp)
    sp.set_defaults(func=cmd_translate)

    # live text goes through a server: no io() or runner_flags() (no input files, no --local)
    def capture_flags(sp):
        sp.add_argument(
            "--system",
            action="store_true",
            help=_(
                "add the system audio as a source (the other side's voices, works with "
                "headphones; macOS 13+, needs the Screen & System Audio Recording permission)"
            ),
        )
        sp.add_argument(
            "--mic",
            action=argparse.BooleanOptionalAction,
            default=True,
            help=_(
                "record the microphone (on by default; --no-mic: system audio only, with --system)"
            ),
        )

    sp = sub.add_parser("live", help=_("live text from the microphone through the selected server"))
    capture_flags(sp)
    sp.add_argument("--lang", metavar="LANG", help=_("recognition language (automatic by default)"))
    sp.add_argument(
        "--translate",
        nargs="?",
        const="ru",
        metavar=_("MY_LANG"),
        help=_(
            "live translation of a call both ways; the argument is your language (ru by "
            "default), the other side's language is detected from speech"
        ),
    )
    sp.add_argument(
        "--no-panes",
        dest="no_panes",
        action="store_true",
        help=_("with --translate: line output instead of two full-screen columns"),
    )
    sp.add_argument(
        "--no-record",
        dest="no_record",
        action="store_true",
        help=_("do not keep the audio (text only)"),
    )
    sp.add_argument(
        "--no-save",
        dest="no_save",
        action="store_true",
        help=_(
            "ephemeral session: nothing is written to disk, text only on screen and in the "
            "clipboard"
        ),
    )
    sp.add_argument(
        "-o",
        "--output",
        help=_("folder of the session files (recordings/ in the current folder by default)"),
    )
    sp.add_argument(
        "--stop-after-silence",
        dest="stop_after_silence",
        type=float,
        default=5.0,
        metavar="MINUTES",
        help=_("stop after N minutes of silence (0 turns it off, 5 by default)"),
    )
    # live text and live translation run on a server only: live has no --local
    sp.add_argument("--runner", metavar="URL", help=_("server URL (env MNEMA_RUNNER)"))
    sp.set_defaults(func=cmd_live, rec=False, local=False)

    # finish processes a recording folder: interrupted, stopped by a second Ctrl-C, or a part
    sp = sub.add_parser(
        "finish",
        help=_("process a recording: all of it or a part"),
    )
    sp.add_argument("path", help=_("recording folder with WAV tracks (mic.wav and/or system.wav)"))
    sp.add_argument(
        "--from",
        dest="start",
        metavar=_("H:MM:SS"),
        help=_(
            "process from this point: 1:05:30 (hours:minutes:seconds), 1:35 "
            "(hours:minutes) or 35 (minutes)"
        ),
    )
    sp.add_argument(
        "--to",
        dest="end",
        metavar=_("H:MM:SS"),
        help=_("process up to this point (same format as --from)"),
    )
    sp.add_argument("--lang", metavar="LANG", help=_("transcript language (automatic by default)"))
    g = sp.add_mutually_exclusive_group()
    g.add_argument(
        "--runner",
        metavar="URL",
        help=_("address of the processing server, MNEMA_RUNNER by default"),
    )
    g.add_argument("--local", action="store_true", help=_("process on this computer"))
    sp.set_defaults(func=cmd_finish)

    # rec records without live text; the whole recording is transcribed on stop
    sp = sub.add_parser(
        "rec",
        help=_("record a call without live text; Ctrl-C transcribes the whole recording"),
    )
    capture_flags(sp)
    sp.add_argument("--lang", metavar="LANG", help=_("transcript language (automatic by default)"))
    sp.add_argument(
        "-o",
        "--output",
        help=_("folder of the session files (recordings/ in the current folder by default)"),
    )
    sp.add_argument(
        "--no-save",
        dest="no_save",
        action="store_true",
        help=_(
            "ephemeral: transcribe and copy the text; the audio goes to a temporary folder "
            "that is deleted after transcribing"
        ),
    )
    runner_flags(sp)
    sp.set_defaults(
        func=cmd_live,
        rec=True,
        translate=None,
        no_panes=True,
        no_record=False,
        stop_after_silence=0.0,
    )

    sp = sub.add_parser(
        "serve", help=_("start the headless runner (the API for the CLI and the apps)")
    )
    sp.add_argument("--dir", help=_("recordings folder (the current one by default)"))
    sp.add_argument(
        "--host",
        default="127.0.0.1",
        help=_("address to listen on (0.0.0.0 for the network)"),
    )
    from ..install_setup import DEFAULT_PORT

    sp.add_argument("--port", type=int, help=_("port ({port} by default)", port=DEFAULT_PORT))
    sp.set_defaults(func=cmd_serve)

    # invites and devices only on the server machine: straight from devices.db, no HTTP; the
    # server folder is the image's own (--dir only for a development server)
    sp = sub.add_parser("invite", help=_("an invitation for one device"))
    sp.add_argument(
        "--hours",
        type=float,
        default=6.0,
        help=_("how long the invitation works, in hours: 6 by default, up to 72"),
    )
    sp.add_argument("--dir", help=argparse.SUPPRESS)
    sp.add_argument(
        "--url",
        help=_(
            "the address devices use to reach this server, e.g. http://203.0.113.5:38837; "
            "remembered for later invitations. Without it, this machine's network address"
        ),
    )
    sp.set_defaults(func=cmd_invite)

    sp = sub.add_parser("devices", help=_("devices connected to the server"))
    sp.add_argument("--dir", help=argparse.SUPPRESS)
    actions = sp.add_subparsers(dest="action")
    revoke = actions.add_parser("revoke", help=_("revoke a device"))
    revoke.add_argument("id", help=_("id from the mnema devices list"))
    revoke.add_argument("--dir", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sp.set_defaults(func=cmd_devices)
    sp = sub.add_parser("model", help=_("the server model for titles and summaries"))
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--show", action="store_true", help=_("only show the current model"))
    g.add_argument(
        "--remove", action="store_true", help=_("remove the model from ~/.config/mnema/.env")
    )
    sp.set_defaults(func=cmd_model)

    sp = sub.add_parser("update", help=_("update the installed app"))
    sp.set_defaults(func=cmd_update)

    return p
