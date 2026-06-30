# AGENTS.md

Onboarding for AI coding assistants. Kept deliberately tight — deep internal
architecture lives in [docs/architecture.md](docs/architecture.md); read the
relevant section there before modifying a subsystem.

## What this is

**lovia** — a lightweight, provider-neutral agent framework for Python. Minimal hard-dep
core (`httpx`, `pydantic`, `pyyaml`); every other capability (MCP, web, search, …) is an opt-in extra.

## Setup & commands

```bash
pip install -e .[dev]      # core + dev tooling
pip install -e .[mcp]      # optional: MCP support
pip install -e .[web]      # optional: FastAPI + chat UI
```

Python 3.10+. Run every Python command through the repo virtualenv: `.venv/bin/python -m …`.

| Task | Command |
| --- | --- |
| Test (all) | `pytest` |
| Test (one) | `pytest tests/test_runner.py::test_plain_text_run` |
| Coverage | `pytest --cov=lovia --cov-report=term-missing` |
| Lint / format | `ruff check .` / `ruff format .` |
| Type-check | `mypy lovia` |

`asyncio_mode = auto` — async tests need no `@pytest.mark.asyncio`. Tests use `ScriptedProvider`
(`tests/scripted_provider.py`): deterministic, no network. Live-endpoint tests are gated behind
`@pytest.mark.live_provider`; run with `LOVIA_LIVE_TESTS=1 pytest -m live_provider`.

## Architecture (orientation — full map in docs/architecture.md)

- **Three layers, each strictly downstream**: core (everything outside `workspace/` + `web/`) →
  `workspace/` (fs + exec) → `web/` (HTTP/SSE/UI). Core never imports workspace or web.
- **`runner.py` is a thin, stateless facade.** Real orchestration is `runtime/loop.py`; `RunLoop`
  is the *only* module that owns mutable state. Trace a run from `RunLoop.stream()`, not `Runner.run()`.
- **Two transcript forms**: `TranscriptEntry` (`transcript.py`) is canonical; `Message` (`messages.py`)
  is a lossy chat-shaped view derived from it. Providers receive `list[TranscriptEntry]`, never `Message`.
- **`Plugin` (`plugins/base.py`) is the one extension axis** — MCP, Skills, Todo, and Memory are all plugins.
- **Compaction is view-only**: it shapes the per-call view sent to the provider, never the transcript or Session.

## Things that will bite you

- `Agent` is **immutable by convention** — use `clone(**overrides)`, never mutate in place.
- A compaction stage must never mutate `state.transcript`/the `Session`, nor *undo* a sticky decision —
  monotonic decisions are what keep the prompt prefix byte-stable and provider caches warm.
- `Session` is **append-only** and distinct from the checkpointer (session = *finished* runs — a
  run that completed, or one the caller finalized, e.g. the web UI on stop; checkpoint = the run
  that may still resume). The runner auto-appends only on success; finalizing an interrupted run
  into the session is a caller decision. Don't add a `replace`.
- Dropping middle transcript entries? Route through `safe_window()` (`transcript.py`) or you'll orphan
  tool-call/result pairs.
- Public-behavior change → sync **both** READMEs (`README.md` + `README-zh.md`).

## Conventions (non-standard only)

- **Async-only** public API; `run_sync()` only wraps `asyncio.run()` — never fork logic across sync/async paths.
- **Pydantic v2** data models (`model_fields`, `model_validate`, …). Type-annotate every public function and class.
- **Errors carry `.hint`** — every `LoviaError` accepts `hint=`, rendered in `str(exc)`. Bury no context.
- Provider adapters live in `lovia/providers/` (translate lovia transcript ⇄ vendor API); register
  third-party ones via the `lovia.providers` entry-point group.
- `ruff` governs format + lint. No `# noqa` without an inline reason.

## Don't

- Don't import `workspace/` or `web/` from core (type-only imports excepted).
- Don't add a hard dependency beyond the current core set — every other capability is an opt-in extra.
- Don't rename or remove public API without a deprecation shim kept for ≥ 1 minor release.
- Don't build a framework abstraction for what fits in a ~10-line user-side recipe. **Decline before designing.**

## Commit convention

[Conventional Commits](https://www.conventionalcommits.org/): `type(scope): imperative summary`
(`feat` / `fix` / `docs` / `refactor` / `perf` / `test` / `chore`).

## Design philosophy

Four words, in priority order — when in doubt, optimise for the earlier one:

1. **Concise** — every piece fits one screen of mental model; new features justify their line cost.
2. **Lightweight** — a minimal hard-dep core; everything else is opt-in and imported only on demand.
3. **Extensible** — public surfaces are dataclasses, Protocols, and `@decorator` hooks, never subclassing.
4. **General-purpose** — practical, framework-agnostic tools / plugins / workspace, batteries behind extras.
