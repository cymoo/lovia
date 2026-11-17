"""``default_app``: the app ``lovia web`` serves, as a function.

:func:`~lovia.web.create_app` serves the agent you built, as you built it.
This builds the ready-made one instead — configured in the browser, saved in
a home directory — and takes the same handful of switches the CLI does, plus
``customize`` for what the CLI can't express.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI

from ..agent import Agent
from ..exceptions import UserError
from ..tools import HumanChannel
from ..tracing import Tracer
from ..workspace import WorkspaceMode
from . import config as webconfig
from .app import Lifespan, create_app
from .builder import Customize, Deployment
from .run_deps import RunDepsFn
from .store import ChatStore
from .ui import ChatUI
from .workspaces import WorkspaceSwitch

# An unanswered question is given up on after this long. Without it a
# clientless run (a scheduled fire, a background task) parked on one holds a
# concurrency slot forever; in a chat someone is watching, ten idle minutes
# means the answer is no.
ANSWER_TIMEOUT = 600

# An unanswered approval card suspends its run after this long: the slot is
# freed and nothing is lost, since answering the card later resumes the run.
# Someone watching answers within a minute; anyone else answers whenever.
SUSPEND_AFTER = 60


def default_app(
    *,
    home: str | Path = webconfig.DEFAULT_HOME,
    workspace: str | Path | None = ".",
    readonly: bool = False,
    lock_workspace: bool = False,
    db_path: str | Path | None = None,
    customize: Customize | None = None,
    title: str = "lovia",
    token: str | None = None,
    auth: Callable[..., Any] | None = None,
    ui: bool | ChatUI = True,
    cors_origins: Sequence[str] | None = None,
    lifespan: Lifespan | None = None,
    run_deps: RunDepsFn | None = None,
    tracer: Tracer | None = None,
) -> FastAPI:
    """Build the app ``lovia web`` serves: its assistant, Settings and all.

    The first five arguments are the CLI's own switches:

    * ``home`` — where ``config.json``, the chat history and memory live
      (``--home``). It becomes lovia's own directory: owner-only, with a
      ``*`` ``.gitignore``.
    * ``workspace`` — the directory new chats work in (``DIR``); ``None``
      for no filesystem or shell at all (``--no-workspace``).
    * ``readonly`` — every run is read-only, whatever Settings says
      (``--readonly``).
    * ``lock_workspace`` — every chat stays in ``workspace``
      (``--lock-workspace``).
    * ``db_path`` — the chat history file (``--db``); default
      ``<home>/lovia.db``.

    ``customize`` is the seam for the rest: it is handed each agent the
    configuration builds — the assistant, ``lovia``, and its background
    child, ``lovia.sub`` — and returns the one to serve, under the same
    name (``agent.clone(tools=[*agent.tools, mine])``). It runs at start and
    again after every save in Settings. Its base ``instructions`` are
    rebuilt for each chat from Settings and the project's ``AGENTS.md``, so
    add tools, plugins, hooks or instruction fragments rather than replace
    that text.

    The remaining arguments are :func:`~lovia.web.create_app`'s, unchanged.
    What a chat may do beyond them — turn limits, follow-ups, permission
    modes, models — comes from ``config.json``, edited in Settings. Until a
    model is configured the app serves the setup screen.
    """
    # Absolute, so both name one place whatever the working directory
    # becomes: the agent is rebuilt from them on every save in Settings.
    home = Path(home).expanduser().absolute()
    if workspace is not None:
        workspace = Path(workspace).expanduser().absolute()
    webconfig.protect_home(home)
    loaded = webconfig.load_active(home)
    deployment = Deployment(
        workspace=None if workspace is None else str(workspace),
        readonly=readonly,
        lock_workspace=lock_workspace,
        home=home,
    )
    store = ChatStore.sqlite(db_path if db_path is not None else home / "lovia.db")
    question_channel = HumanChannel()
    runtime = webconfig.ConfigRuntime(
        deployment=deployment,
        loaded=loaded,
        store=store,
        question_channel=question_channel,
        customize=customize,
    )
    workspaces: WorkspaceSwitch | None = None
    if deployment.workspace is not None:
        roots = deployment.roots()
        workspaces = WorkspaceSwitch(
            roots=roots,
            modes=cast("tuple[WorkspaceMode, ...]", deployment.modes()),
            build=runtime.workspace_for,
            agent_for=runtime.agent_for,
            general=deployment.general_workspace() if roots == "any" else None,
            remotes=deployment.remotes(),
            agent_for_remote=runtime.agent_for_remote,
        )
    agents: dict[str, Agent[Any]] = {}
    aux_model = None
    if runtime.configured:
        # The same build a save in Settings uses, so the two can't drift.
        # Eager, so a vendor-prefix typo surfaces now, not on the first chat.
        try:
            agents, aux_model = runtime.build()
        except ValueError as exc:
            raise UserError(str(exc)) from exc
    return create_app(
        agents,
        title=title,
        store=store,
        max_turns=runtime.max_turns(),
        token=token,
        auth=auth,
        followups=runtime.followups,
        title_model=aux_model,
        followup_model=aux_model,
        suspend_after=SUSPEND_AFTER,
        question_channel=question_channel,
        question_timeout=ANSWER_TIMEOUT,
        ui=ui,
        cors_origins=cors_origins,
        lifespan=lifespan,
        run_deps=run_deps,
        tracer=tracer,
        config_runtime=runtime,
        workspaces=workspaces,
    )
