"""``jobstitch`` — the command line.

Parses, builds the :class:`Config` (flags beat the environment beats
``jobstitch.toml`` beats what is next to the executable) and hands over to a
mode. It contains no logic of its own beyond that.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Optional

from .config import COVER_LETTER_MODES, ConfigError, load_config
from .modes import clipboard as clipboard_mode
from .modes import logs as logs_mode
from .modes import render as render_mode
from .modes import submit as submit_mode
from .modes import watch as watch_mode
from .ui import fail

EPILOGUE = """\
files:
  Put candidate_profile.json, candidate_data.json, pers_preferences.md and
  candidate_signature.png next to the jobstitch executable and they are picked
  up by name. A prompt or resume3.tex.jinja found there overrides the
  server's default; anything absent falls back to it.

examples:
  jobstitch clipboard --out ~/applications
  jobstitch watch --in ~/Downloads/jds --out ~/applications --cover-letter yes
  jobstitch submit posting.txt --out ~/applications --yes
  jobstitch render ~/applications/cv/26-01-15/Acme_Head_of_IT/cv_Jordan_Rivera.json
  jobstitch logs 0f9c1a7b-2f4e-4f2a-9a31-5c0d2f1e8b44
"""


def global_options() -> argparse.ArgumentParser:
    """Flags that work on either side of the mode name.

    ``SUPPRESS`` so an unset flag writes nothing: the subparser's copy must
    not overwrite a value given before the mode name.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=argparse.SUPPRESS,
                        help="Path to jobstitch.toml.")
    common.add_argument("--data-dir", type=Path, default=argparse.SUPPRESS,
                        help="Folder to look in for your files (before the executable's own).")
    common.add_argument("--server", default=argparse.SUPPRESS,
                        help="Server URL (default: http://localhost:8080).")
    common.add_argument("--token", default=argparse.SUPPRESS,
                        help="Bearer token, if the server requires one.")
    common.add_argument("--temperature", type=float, default=argparse.SUPPRESS,
                        help="Sampling temperature override.")
    common.add_argument("-d", "--debug", action="store_true", default=argparse.SUPPRESS,
                        help="Fetch the server's log for every call, not only failures.")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="Print each API call and the request id it came back with.")
    return common


def build_parser() -> argparse.ArgumentParser:
    common = global_options()
    parser = argparse.ArgumentParser(
        prog="jobstitch",
        description="Turn job descriptions into tailored CVs, against a jobstitch server.",
        epilog=EPILOGUE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )

    modes = parser.add_subparsers(dest="mode", required=True, metavar="MODE")

    def add_job_flags(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--out", type=Path, required=True,
                         help="Output folder (working/, error/, discarded/, cv/).")
        sub.add_argument("--cover-letter", choices=COVER_LETTER_MODES,
                         help="Also write a cover letter (default: no).")
        sub.add_argument("--yes", action="store_true",
                         help="Submit every valid posting without asking.")
        sub.add_argument("--no-xlsx", action="store_true",
                         help="Do not record delivered CVs in applications.xlsx.")

    clip = modes.add_parser("clipboard", help="Watch the clipboard for postings.",
                              parents=[common])
    add_job_flags(clip)

    watch = modes.add_parser("watch", help="Watch a folder for postings.",
                              parents=[common])
    watch.add_argument("--in", dest="inbox", type=Path, required=True,
                       help="Folder to watch for job description files.")
    add_job_flags(watch)

    submit = modes.add_parser("submit", help="Process one job description file.",
                              parents=[common])
    submit.add_argument("jd_file", type=Path, help="The job description to process.")
    add_job_flags(submit)

    render = modes.add_parser("render", help="Compile a cv_*.json or cv_*.tex into a PDF.",
                              parents=[common])
    render.add_argument("source", type=Path, help="The .json document or .tex source.")
    render.add_argument("--output", type=Path,
                        help="Where to write the PDF (default: beside the input).")

    logs = modes.add_parser("logs", help="Print a past request's server log.",
                            parents=[common])
    logs.add_argument("request_id", help="The request id an envelope or an error reported.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    flag = lambda name: getattr(args, name, None)  # noqa: E731 — SUPPRESS defaults
    try:
        config = load_config(
            config_file=flag("config"),
            data_dir=flag("data_dir"),
            overrides={
                "server_url": flag("server"),
                "token": flag("token"),
                "temperature": flag("temperature"),
                "cover_letter": getattr(args, "cover_letter", None),
                "debug": flag("debug"),
                "verbose": flag("verbose"),
            },
        )
    except (ConfigError, FileNotFoundError) as exc:
        fail(str(exc))

    try:
        if args.mode == "logs":
            return logs_mode.run(config, args.request_id)
        if args.mode == "render":
            return render_mode.run(config, args.source, output=args.output)

        common = {"assume_yes": args.yes, "track": not args.no_xlsx}
        if args.mode == "clipboard":
            return clipboard_mode.run(config, args.out, **common)
        if args.mode == "watch":
            return watch_mode.run(config, args.inbox, args.out, **common)
        return submit_mode.run(config, args.jd_file, args.out, **common)
    except ConfigError as exc:
        fail(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
