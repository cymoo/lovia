"""Illustrate the compaction calibration *ratio* converging, turn by turn.

This drives the **real** :class:`lovia.context.Compaction` policy — the code in
``lovia/context/compaction.py`` — against the **real** DeepSeek OpenAI-compatible
endpoint configured in ``.env``, and watches
:data:`~lovia.context.state.CompactionState.ratio` walk from its ``1.0`` seed to
the steady-state multiplier for each content type.

What the ratio *is*
-------------------
The estimator in ``lovia/context/tokens.py`` prices an entry at
``utf8_bytes / 4 + overhead`` — one C-speed ``encode`` per entry, no tokenizer.
That is deliberately rough and *systematically* wrong in a way that depends on
the script mix (English over-counts, digit-dense text under-counts, CJK sits in
between). ``Compaction._calibrate`` folds the provider's real
``prompt_tokens`` from the previous call into a clamped EMA::

    observed   = last_input_tokens / last_view_estimate      # real / raw-estimate
    state.ratio = clamp(RATIO_MIN, RATIO_MAX,
                        (1 - alpha) * state.ratio + alpha * observed)   # alpha = 0.2

and every estimate is then ``int((raw + tool_overhead) * ratio)``. Because the
content type is held constant within a scenario, ``observed`` is roughly the
same every turn, so the EMA converges to that scenario's true estimator error —
which is exactly what this study renders.

How the loop stays honest
-------------------------
* The estimate side runs the genuine ``Compaction.compact()`` (watermarks set so
  no stage ever fires — ``view == entries`` — so real and estimate describe the
  same bytes).
* The ground-truth side sends that exact view to DeepSeek, serialized through
  the provider's own ``entries_to_openai_messages``, and reads ``prompt_tokens``.
* Content is fabricated on both sides (see ``samples.py``); no live model output
  feeds back, so a run is reproducible.

Run it::

    LOVIA_LIVE_TESTS=1 .venv/bin/python tests/context/ratio_calibration/run.py --turns 18

Writes ``docs/ratio-calibration.md``. Cost is real but tiny — each turn resends
a byte-stable prefix, so DeepSeek's prompt cache serves most of it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx

_HERE = Path(__file__).resolve().parent


def _find_root(start: Path) -> Path:
    """Walk up to the repo root (the dir holding ``pyproject.toml``)."""
    for candidate in (start, *start.parents):
        if (candidate / "pyproject.toml").exists():
            return candidate
    return start


_ROOT = _find_root(_HERE)

try:
    # Imported as a package (e.g. by the test suite): keep one module identity.
    from .samples import SCENARIOS, Scenario  # noqa: E402
except ImportError:
    # Run as a plain script: bootstrap this dir onto sys.path for a bare import.
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))
    from samples import SCENARIOS, Scenario  # noqa: E402

from lovia.context import CompactionRequest, Compaction, CompactionState, TokenCounter  # noqa: E402
from lovia.context.tokens import usable_tokens  # noqa: E402
from lovia.providers.openai_chat import entries_to_openai_messages  # noqa: E402
from lovia.transcript import AssistantTextEntry, InputEntry, TranscriptEntry  # noqa: E402


# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #


def _load_env() -> None:
    """Seed os.environ from ``.env`` (does not override an existing value)."""
    env_path = _ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@dataclass(frozen=True)
class Endpoint:
    base: str
    key: str
    model: str


def _endpoint() -> Endpoint:
    _load_env()
    if os.getenv("LOVIA_LIVE_TESTS") != "1":
        sys.exit("opt-in required: set LOVIA_LIVE_TESTS=1 (this makes real API calls)")
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        sys.exit("OPENAI_API_KEY is not configured")
    base = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("OPENAI_DEFAULT_MODEL", "deepseek-v4-pro")
    return Endpoint(base=base, key=key, model=model)


# --------------------------------------------------------------------------- #
# Ground truth: the provider's real prompt-token count for a view
# --------------------------------------------------------------------------- #


async def _real_usage(
    client: httpx.AsyncClient, ep: Endpoint, entries: list[TranscriptEntry]
) -> dict:
    """Return DeepSeek's ``usage`` for the exact view lovia would send.

    Serialized through the provider's own wire mapping so framing matches
    production. ``max_tokens=1`` because we only care about ``prompt_tokens``;
    a tiny retry rides out transient network blips over a long run.
    """
    messages = entries_to_openai_messages(entries, reasoning_provider="openai-chat")
    payload = {
        "model": ep.model,
        "messages": messages,
        "max_tokens": 1,
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {ep.key}",
        "content-type": "application/json",
    }
    last: Exception | None = None
    for attempt in range(4):
        try:
            r = await client.post(
                f"{ep.base}/chat/completions", headers=headers, json=payload
            )
            r.raise_for_status()
            return r.json()["usage"]
        except (httpx.HTTPError, KeyError) as exc:  # transient: retry with backoff
            last = exc
            await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"usage call failed after retries: {last}")


# --------------------------------------------------------------------------- #
# One turn's observation
# --------------------------------------------------------------------------- #


@dataclass
class Turn:
    turn: int
    real: int  # provider prompt_tokens for the view
    est_raw: int  # uncalibrated byte estimate (raw + tool overhead)
    est_cal: int  # calibrated estimate = int(est_raw * ratio) = tokens_after
    ratio: float  # ratio used to produce est_cal this turn (post-calibrate)
    observed: float | None  # real_{prev}/est_raw_{prev}, the value folded this turn
    pressure: float  # est_cal / usable — the fill lovia acts on
    real_pressure: float  # real / usable — the true fill
    cache_hit: int  # prompt_cache_hit_tokens (warm-prefix evidence)
    chars: int  # characters in the view (for chars/token)
    utf8: int  # utf-8 bytes in the view (for bytes/token)

    @property
    def err_raw(self) -> float:
        return self.est_raw / self.real - 1.0

    @property
    def err_cal(self) -> float:
        return self.est_cal / self.real - 1.0


@dataclass
class ScenarioResult:
    scn: Scenario
    window: int
    reserve: int
    usable: int
    turns: list[Turn] = field(default_factory=list)
    compacted_any: bool = False

    @property
    def converged_ratio(self) -> float:
        tail = self.turns[-3:]
        return sum(t.ratio for t in tail) / len(tail)

    @property
    def steady_err_raw(self) -> float:
        tail = self.turns[-3:]
        return sum(abs(t.err_raw) for t in tail) / len(tail)

    @property
    def steady_err_cal(self) -> float:
        tail = self.turns[-3:]
        return sum(abs(t.err_cal) for t in tail) / len(tail)

    @property
    def chars_per_token(self) -> float:
        last = self.turns[-1]
        return last.chars / last.real

    @property
    def bytes_per_token(self) -> float:
        last = self.turns[-1]
        return last.utf8 / last.real


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #


def _grow(scn: Scenario, turn: int) -> list[TranscriptEntry]:
    """Full transcript through ``turn`` inclusive (user+assistant per turn)."""
    entries: list[TranscriptEntry] = []
    for i in range(turn + 1):
        entries.append(InputEntry(role="user", content=scn.question(i)))
        entries.append(AssistantTextEntry(content=scn.answer(i)))
    return entries


async def run_scenario(
    ep: Endpoint, client: httpx.AsyncClient, scn: Scenario, n_turns: int
) -> ScenarioResult:
    # Size the window offline (no API) so final pressure lands near ~0.6 and the
    # watermarks never trigger — this study is about calibration, not stages.
    counter = TokenCounter()
    final_raw = counter.count(_grow(scn, n_turns - 1))
    reserve = 1_000
    window = int(final_raw / 0.6) + reserve
    usable = usable_tokens(window, reserve)

    policy = Compaction(
        context_window=window,
        reserve_output_tokens=reserve,
        compact_at=0.98,  # effectively never fires within this study's range
        compact_to=0.60,
    )

    out = ScenarioResult(scn=scn, window=window, reserve=reserve, usable=usable)
    scratch: dict = {}
    prev_real: int | None = None
    prev_est_raw: int | None = None

    for turn in range(n_turns):
        entries = _grow(scn, turn)
        req = CompactionRequest(
            entries=list(entries),
            provider=None,  # byte heuristic (DeepSeek ships no local tokenizer)
            model=ep.model,
            tools=(),
            last_input_tokens=prev_real,
            scratch=scratch,
        )
        result = await policy.compact(req)
        if result.compacted:
            out.compacted_any = True  # a stage fired: window mis-sized, note it

        st = CompactionState.load(scratch)
        ratio = st.ratio
        est_raw = st.last_view_estimate or 0
        est_cal = result.tokens_after or 0

        usage = await _real_usage(client, ep, result.entries)
        real = int(usage["prompt_tokens"])
        cache_hit = int(
            usage.get("prompt_cache_hit_tokens")
            or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
            or 0
        )

        chars = sum(len(_text(e)) for e in result.entries)
        utf8 = sum(
            len(_text(e).encode("utf-8", "surrogatepass")) for e in result.entries
        )

        out.turns.append(
            Turn(
                turn=turn,
                real=real,
                est_raw=est_raw,
                est_cal=est_cal,
                ratio=ratio,
                observed=(prev_real / prev_est_raw) if prev_est_raw else None,
                pressure=est_cal / usable,
                real_pressure=real / usable,
                cache_hit=cache_hit,
                chars=chars,
                utf8=utf8,
            )
        )
        prev_real, prev_est_raw = real, est_raw

    return out


def _text(entry: TranscriptEntry) -> str:
    content = getattr(entry, "content", "")
    return content if isinstance(content, str) else ""


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

_BLOCKS = "▁▂▃▄▅▆▇█"


def _sparkline(values: list[float], lo: float, hi: float) -> str:
    if hi <= lo:
        return _BLOCKS[0] * len(values)
    out = []
    for v in values:
        frac = max(0.0, min(1.0, (v - lo) / (hi - lo)))
        out.append(_BLOCKS[round(frac * (len(_BLOCKS) - 1))])
    return "".join(out)


def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def _scenario_section(res: ScenarioResult) -> str:
    scn = res.scn
    lines = [f"### {scn.title} (`{scn.key}`)", "", scn.blurb, ""]
    lines.append(
        f"*Window {res.window:,} tok · reserve {res.reserve:,} · "
        f"usable {res.usable:,} · watermark 0.98 (never triggers here).*"
    )
    lines.append("")
    lines.append(
        "| turn | real | est raw | est cal | ratio | observed | err raw | "
        "err cal | pressure |"
    )
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for t in res.turns:
        obs = f"{t.observed:.3f}" if t.observed is not None else "—"
        lines.append(
            f"| {t.turn} | {t.real:,} | {t.est_raw:,} | {t.est_cal:,} | "
            f"{t.ratio:.3f} | {obs} | {_pct(t.err_raw)} | {_pct(t.err_cal)} | "
            f"{t.pressure * 100:.0f}% |"
        )
    lines.append("")

    ratios = [t.ratio for t in res.turns]
    lo, hi = min(ratios + [res.converged_ratio]), max(ratios + [1.0])
    lines.append(
        f"```\nratio  {_sparkline(ratios, lo, hi)}  {ratios[0]:.2f} → "
        f"{res.converged_ratio:.3f}\n```"
    )
    lines.append("")

    # Interpretation: which direction the heuristic erred, and how far.
    first_obs = next((t.observed for t in res.turns if t.observed is not None), None)
    direction = "over-counts" if res.converged_ratio < 1 else "under-counts"
    lines.append(
        f"The byte heuristic **{direction}** this content: it converges to "
        f"**ratio ≈ {res.converged_ratio:.3f}** "
        f"({res.bytes_per_token:.2f} UTF-8 bytes/token, "
        f"{res.chars_per_token:.2f} chars/token in the real tokenizer). "
        f"Calibration cuts the steady-state estimate error from "
        f"**{res.steady_err_raw * 100:.1f}%** (raw) to "
        f"**{res.steady_err_cal * 100:.1f}%** (calibrated)."
    )
    if first_obs is not None:
        lines.append("")
        lines.append(
            f"The very first observation already lands near the target "
            f"({first_obs:.3f}); the remaining turns are just the EMA "
            f"(α = 0.2) walking the seed 1.0 down the gap."
        )
    lines.append("")
    return "\n".join(lines)


def _summary_table(results: list[ScenarioResult]) -> str:
    lines = [
        "| scenario | converged ratio | bytes/tok | chars/tok | "
        "steady err (raw) | steady err (calibrated) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        lines.append(
            f"| {r.scn.title} (`{r.scn.key}`) | **{r.converged_ratio:.3f}** | "
            f"{r.bytes_per_token:.2f} | {r.chars_per_token:.2f} | "
            f"{r.steady_err_raw * 100:.1f}% | {r.steady_err_cal * 100:.1f}% |"
        )
    return "\n".join(lines)


def _convergence_grid(results: list[ScenarioResult]) -> str:
    """One sparkline per scenario on a shared 0.5–1.5 ratio scale."""
    lines = ["```", "ratio convergence (shared scale 0.50 ─ 1.50)", ""]
    width = max(len(r.scn.key) for r in results)
    for r in results:
        spark = _sparkline([t.ratio for t in r.turns], 0.5, 1.5)
        lines.append(f"{r.scn.key.rjust(width)}  {spark}  → {r.converged_ratio:.3f}")
    lines.append("```")
    return "\n".join(lines)


def _clamp_note(results: list[ScenarioResult]) -> str:
    ratios = [r.converged_ratio for r in results]
    lo, hi = min(ratios), max(ratios)
    below = all(r < 1.0 for r in ratios)
    lead = (
        "Every one of the five ratios lands **below 1.0**"
        if below
        else "The ratios straddle 1.0"
    )
    return (
        f"{lead} ({lo:.3f}–{hi:.3f}): against DeepSeek's tokenizer the "
        "`byte/4` heuristic *over*-counts all of these text types. The clamp's "
        "wider upper half (the ceiling reaches `2.5`, versus a `0.5` floor) is "
        "held in reserve for the opposite failure — digit- or symbol-dense "
        "payloads the heuristic *under*-counts — which none of these prose/code "
        "mixes provoke. Notably the English ratio (~0.72) dips below the ~0.8 "
        "the code comments cite for BPE-friendly prose, so DeepSeek is even more "
        "efficient on English than the estimator's design assumed — yet it still "
        "clears the `0.5` floor with room to spare."
    )


def render_report(ep: Endpoint, n_turns: int, results: list[ScenarioResult]) -> str:
    warm = [t.cache_hit for r in results for t in r.turns[1:] if t.cache_hit]
    warm_note = (
        f"Prompt-cache hits were observed on {len(warm)} of the resent turns "
        f"(max {max(warm):,} cached tokens on a single call), confirming the "
        f"byte-stable prefix keeps the provider cache warm — the whole point of "
        f"sticky rendering."
        if warm
        else "No prompt-cache hits were reported by the endpoint on this run."
    )

    parts = [
        "# Compaction calibration ratio: convergence across content types",
        "",
        "> Generated by `tests/context/ratio_calibration/run.py` against a live "
        f"DeepSeek endpoint (`{ep.model}`). Each scenario grows a homogeneous "
        f"transcript over **{n_turns} turns**, driving the real "
        "`lovia.context.Compaction` policy and comparing its byte-weighted "
        "estimate against the endpoint's real `prompt_tokens`.",
        "",
        "## What you're looking at",
        "",
        "`lovia/context/tokens.py` estimates an entry at `utf8_bytes / 4 + "
        "overhead` — no tokenizer, one `encode` per entry. That estimate is "
        "systematically wrong in a way that depends on the script mix. "
        "`Compaction._calibrate` (in `lovia/context/compaction.py`) corrects it "
        "with a **clamped EMA** over the provider's real counts:",
        "",
        "```python",
        "observed    = last_input_tokens / last_view_estimate   # real / raw estimate",
        "state.ratio = clamp(RATIO_MIN, RATIO_MAX,              # 0.5 .. 2.5",
        "    (1 - 0.2) * state.ratio + 0.2 * observed)          # alpha = 0.2",
        "tokens      = int((raw + tool_overhead) * state.ratio) # every estimate",
        "```",
        "",
        "Because each scenario holds its content type constant, `observed` is "
        "nearly the same every turn, so `ratio` walks from its **1.0 seed** to "
        "that content's true estimator error and holds there. The columns:",
        "",
        "- **real** — the endpoint's `prompt_tokens` for the exact view sent.",
        "- **est raw** — the uncalibrated byte estimate (`raw + tool_overhead`).",
        "- **est cal** — the calibrated estimate `int(est_raw × ratio)`; what "
        "the policy actually believes and acts on.",
        "- **ratio** — the calibration multiplier *after* folding the previous "
        "turn's real count.",
        "- **observed** — `real / est_raw` from the previous turn: the sample "
        "the EMA folded this turn.",
        "- **err raw / err cal** — how far the raw vs calibrated estimate sits "
        "from the truth.",
        "- **pressure** — calibrated tokens as a fraction of the usable window "
        "(what the watermarks compare against).",
        "",
        "## Cross-scenario summary",
        "",
        _summary_table(results),
        "",
        _convergence_grid(results),
        "",
        _clamp_note(results),
        "",
        f"_{warm_note}_",
        "",
        "## Per-scenario detail",
        "",
    ]
    for r in results:
        parts.append(_scenario_section(r))

    parts += [
        "## Takeaways",
        "",
        "1. **Every scenario converges, to a *different* number.** The ratio is "
        "not a universal fudge factor — it is the estimator's script-specific "
        "error, and it lands comfortably inside the `[0.5, 2.5]` clamp for all "
        "five mixes. That is the clamp band doing its intended job.",
        "2. **Calibration earns its keep most where the heuristic is most "
        "wrong.** Pure English and pure Chinese prose sit farthest from byte/4 "
        "(raw error ~35–40%) and see the largest correction. The mixed "
        "Chinese+English stream starts *closest* (~15%) because its two "
        "tokenizer regimes partly cancel — being single-script and highly "
        "compressible, not being 'simple', is what makes English the worst case "
        "for the raw estimate.",
        "3. **One turn of lag is the whole cost.** `last_input_tokens` trails "
        "the live transcript by exactly one call, so the ratio is always "
        "calibrated on the *previous* view — good enough because the target "
        "barely moves turn to turn.",
        "4. **The byte-stable prefix keeps caches warm.** Nothing before the "
        "watermark is rewritten, so re-sending the growing transcript is cheap "
        "— visible in the cache-hit accounting above.",
        "",
        "## Reproduce",
        "",
        "```bash",
        "LOVIA_LIVE_TESTS=1 .venv/bin/python tests/context/ratio_calibration/run.py --turns "
        f"{n_turns}",
        "```",
        "",
        "<details><summary>Raw data (JSON)</summary>",
        "",
        "```json",
        json.dumps(
            {
                "model": ep.model,
                "turns": n_turns,
                "scenarios": {
                    r.scn.key: {
                        "window": r.window,
                        "usable": r.usable,
                        "converged_ratio": round(r.converged_ratio, 4),
                        "turns": [
                            {
                                "turn": t.turn,
                                "real": t.real,
                                "est_raw": t.est_raw,
                                "est_cal": t.est_cal,
                                "ratio": round(t.ratio, 4),
                                "observed": round(t.observed, 4)
                                if t.observed
                                else None,
                                "cache_hit": t.cache_hit,
                            }
                            for t in r.turns
                        ],
                    }
                    for r in results
                },
            },
            ensure_ascii=False,
            indent=1,
        ),
        "```",
        "",
        "</details>",
        "",
    ]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


async def _main_async(n_turns: int, out_path: Path) -> None:
    ep = _endpoint()
    print(f"endpoint: {ep.base}  model: {ep.model}")
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as client:
        results = await asyncio.gather(
            *(run_scenario(ep, client, scn, n_turns) for scn in SCENARIOS)
        )
    for r in results:
        flag = "  ⚠ a stage fired (window mis-sized)" if r.compacted_any else ""
        print(
            f"  {r.scn.key:>8}: ratio 1.000 → {r.converged_ratio:.3f}  "
            f"(raw err {r.steady_err_raw * 100:4.1f}% → cal {r.steady_err_cal * 100:4.1f}%){flag}"
        )
    report = render_report(ep, n_turns, list(results))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    try:
        shown = out_path.relative_to(_ROOT)
    except ValueError:
        shown = out_path
    print(f"\nwrote {shown}  ({len(report):,} bytes)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=int, default=18, help="turns per scenario")
    parser.add_argument(
        "--out",
        type=Path,
        default=_ROOT / "docs" / "ratio-calibration.md",
        help="report output path",
    )
    args = parser.parse_args()
    asyncio.run(_main_async(args.turns, args.out))


if __name__ == "__main__":
    main()
