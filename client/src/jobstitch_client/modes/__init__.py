"""The four things the client does.

Each mode is thin on purpose: pick a source, build the runner, drain the
source. Everything else lives in :mod:`jobstitch_client.runner` and
:mod:`jobstitch_client.workspace`, so adding a fifth source would not touch
the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from ..api import HttpApi, JobstitchApi
from ..config import Config
from ..runner import JobRunner, Outcome
from ..sources import JDSource
from ..tracking import build_tracker
from ..ui import AutoConfirmer, Confirmer, PromptConfirmer, ask_recovery
from ..workspace import Workspace


@dataclass
class Session:
    """What every mode needs, assembled once."""

    config: Config
    workspace: Workspace
    api: JobstitchApi
    runner: JobRunner

    @classmethod
    def build(cls, config: Config, out: Path, *, assume_yes: bool = False,
              track: bool = True, resume: Optional[str] = None) -> "Session":
        workspace = Workspace(out).ensure()
        api = HttpApi(config.server_url, token=config.token, verbose=config.verbose)
        confirmer: Confirmer = AutoConfirmer() if assume_yes else PromptConfirmer()
        runner = JobRunner(
            api=api,
            workspace=workspace,
            config=config,
            confirmer=confirmer,
            tracker=build_tracker(workspace.root, track),
            resume=resume,
        )
        return cls(config=config, workspace=workspace, api=api, runner=runner)

    def drain(self, source: JDSource) -> int:
        """Run every candidate the source produces. Returns an exit code."""
        failures = 0
        for candidate in source.candidates():
            outcome = self.runner.handle(candidate)
            report(outcome)
            if outcome.status == "quit":
                break
            failures += outcome.status == "failed"
        return 1 if failures else 0

    def recover(self) -> None:
        """Deal with whatever a previous run left in ``working/``."""
        files, folders = self.workspace.pending()
        if not files and not folders:
            return
        choice = ask_recovery(len(files), len(folders))
        if choice == "quit":
            raise SystemExit(0)
        if choice == "clean":
            print(f"🧹 removed {self.workspace.clean()} leftover entr(ies)", flush=True)
            return
        for folder in folders:
            report(self.runner.resume(folder))
        for path in files:
            from ..sources import JDCandidate
            from ..sources.folder import read_text

            report(self.runner.handle(
                JDCandidate(text=read_text(path), origin=path, label=path.name)
            ))


ICONS = {"delivered": "✅", "discarded": "🗑", "rejected": "⚠", "failed": "❌", "quit": "⏹"}


def report(outcome: Outcome) -> None:
    where = f" -> {outcome.path}" if outcome.path else ""
    print(f"{ICONS.get(outcome.status, '•')} {outcome.status}: {outcome.message}{where}\n",
          flush=True)


__all__ = ["Session", "report"]
