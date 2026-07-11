# lovia test suite

[中文文档](./README-zh.md)

The suite is plain `pytest`, rooted here (`testpaths = tests` in
[`pytest.ini`](../pytest.ini)). Default tests are deterministic and never touch
the network; a small, opt-in set of **live** tests calls real provider
endpoints and is fenced off behind a marker.

## Layout

```
tests/
├── conftest.py            shared fixtures
├── scripted_provider.py   re-export of lovia.testing.ScriptedProvider (compat shim)
├── test_*.py              top-level suites (runner, transcript, hooks, schema, …)
├── context/               compaction: pipeline, stages, tokens, render, state, …
│   ├── test_live_context.py         live: summarize / clear / offload / recall / overflow
│   ├── test_ratio_convergence.py    live: calibration-ratio convergence
│   └── ratio_calibration/           study + report generator (not a test — see its README)
├── providers/             openai_chat / anthropic adapters (+ test_live.py)
├── runtime/               run loop, checkpoint, steering, budgets
├── plugins/               mcp, skills, todo, memory (memory has live tests)
├── eval/                  lovia.eval framework (+ test_live.py)
├── stores/ · tools/ · web/ · workspace/
```

Support code (`conftest.py`, `scripted_provider.py`) lives beside the tests and
is **not** itself collected — pytest's default discovery matches `test_*.py` and
`*_test.py`, which neither support file does (`conftest.py` is loaded for its
fixtures, not collected as a test).

## Running

| Task | Command |
| --- | --- |
| Everything (fast, offline) | `pytest` |
| One test | `pytest tests/test_runner.py::test_plain_text_run` |
| One directory | `pytest tests/context` |
| Coverage | `pytest --cov=lovia --cov-report=term-missing` |
| Lint / format | `ruff check .` · `ruff format .` |

Notes:

- **`asyncio_mode = auto`** — `async def test_…` runs without an
  `@pytest.mark.asyncio` decorator.
- Default tests drive the model through
  **`lovia.testing.ScriptedProvider`** (deterministic, no network); reach for it
  instead of hitting a real endpoint.
- Use `uv run pytest …` if you prefer the project venv resolved for you.

## Live (provider) tests

These are **opt-in** and call whatever endpoint your `.env` points at (the repo
is wired for DeepSeek's OpenAI- and Anthropic-compatible APIs). Every live file
loads `.env` itself, so you don't need to `export` anything first.

### They're selected by a marker — applied two ways

If you grepped for `@pytest.mark.live_provider` on each test function and came up
empty, that's expected: most live files set the marker **once, at module level**,
which applies it to every test in the file:

```python
# tests/context/test_live_context.py, providers/test_live.py, eval/test_live.py,
# context/test_ratio_convergence.py
pytestmark = pytest.mark.live_provider          # marks the whole module
```

The two `plugins/` files mix live and offline tests, so there the marker is a
per-function decorator instead (`@pytest.mark.live_provider`). Either way pytest
sees it — verify the partition yourself:

```bash
pytest -m live_provider     --collect-only -q | grep -c ::   # the live set (~30)
pytest -m "not live_provider" --collect-only -q | grep -c ::   # everything else
```

### The one command

```bash
LOVIA_LIVE_TESTS=1 pytest -m live_provider
```

This runs the standard live set across all six files (context, providers, eval,
memory, ratio-convergence).

### Opt-in gates

`LOVIA_LIVE_TESTS=1` unlocks the bulk. A few tests are **double-gated** and will
still skip until you also opt in:

| Env var | Unlocks | Note |
| --- | --- | --- |
| `LOVIA_LIVE_TESTS=1` | the whole live set | required; plus the relevant API key |
| `LOVIA_LIVE_OVERFLOW_TESTS=1` | the real-overflow probe | sends a deliberately huge prompt |
| `LOVIA_LIVE_OPENAI_CONTENT_TESTS=1` | OpenAI image/file content parts | else runs only against the official host |
| `LOVIA_LIVE_ANTHROPIC_CONTENT_TESTS=1` | Anthropic image/PDF/file blocks | else runs only against the official host |
| `LOVIA_ANTHROPIC_PDF_OK` / `LOVIA_ANTHROPIC_TEXT_FILE_OK` / `LOVIA_OPENAI_FILE_OK` | specific file-capability assertions | endpoint must actually support the modality |

Run absolutely everything:

```bash
LOVIA_LIVE_TESTS=1 \
LOVIA_LIVE_OVERFLOW_TESTS=1 \
LOVIA_LIVE_OPENAI_CONTENT_TESTS=1 \
LOVIA_LIVE_ANTHROPIC_CONTENT_TESTS=1 \
pytest -m live_provider
```

> ⚠️ Forcing the content flags makes the image/PDF/file tests actually run. If
> your endpoint (e.g. DeepSeek) doesn't support that modality they **fail**,
> they don't skip. Against a non-official endpoint the first, un-flagged command
> is usually what you want.

### The calibration study

`context/ratio_calibration/` is a standalone driver + report generator, not a
collected test (it makes real calls and writes
[`docs/ratio-calibration.md`](../docs/ratio-calibration.md)). Its collected
entry point — the invariants, asserted on a small run — is
`context/test_ratio_convergence.py`. See that folder's `README.md` to run the
full study.
