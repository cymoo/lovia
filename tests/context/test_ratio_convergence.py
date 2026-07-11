"""Live convergence check for the compaction calibration ratio.

The *static* estimator-accuracy bound lives next door in
``test_live_context.py::test_live_estimator_accuracy_across_content_types`` — it
asserts the byte heuristic lands within the clamp for one snapshot of each
content type. This is its *dynamic* sibling: it drives the real
:class:`~lovia.context.Compaction` calibration loop for a handful of turns and
asserts the ratio actually *converges* toward the provider's real token
accounting, turn by turn.

The full five-scenario study and its written report are produced by
``ratio_calibration/run.py``; this test reuses that harness and only asserts the
invariants the report demonstrates, on a small (cheap) run. Opt in with::

    LOVIA_LIVE_TESTS=1 pytest tests/context/test_ratio_convergence.py -q
"""

from __future__ import annotations

import os

import httpx
import pytest

from lovia.context.state import RATIO_MAX, RATIO_MIN

from .ratio_calibration.run import Endpoint, _load_env, run_scenario
from .ratio_calibration.samples import SCENARIOS

pytestmark = pytest.mark.live_provider

_BY_KEY = {s.key: s for s in SCENARIOS}


def _endpoint() -> Endpoint:
    """Resolve the live endpoint, skipping (not failing) when not opted in."""
    _load_env()
    if os.getenv("LOVIA_LIVE_TESTS") != "1":
        pytest.skip("opt-in: set LOVIA_LIVE_TESTS=1 to run live provider tests")
    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY is not configured")
    base = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    return Endpoint(
        base=base,
        key=os.environ["OPENAI_API_KEY"],
        model=os.getenv("OPENAI_DEFAULT_MODEL", "deepseek-v4-pro"),
    )


# Two ends of the spectrum: ``en`` is where byte/4 over-counts hardest (English
# is the most BPE-compressible, so the raw error is largest), and ``zh_en`` is
# where the two scripts nearly cancel (smallest raw error). Both must converge —
# the assertions below are direction-agnostic, so they'd hold for an
# under-counting content type just as well.
@pytest.mark.parametrize("key", ["en", "zh_en"])
async def test_ratio_converges_toward_real_token_accounting(key: str):
    ep = _endpoint()
    scn = _BY_KEY[key]
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as client:
        res = await run_scenario(ep, client, scn, n_turns=12)

    turns = res.turns
    final = turns[-1]

    # Turn 0 has nothing to calibrate against, so the ratio is still the seed.
    assert turns[0].ratio == 1.0
    # The study sizes the window to stay under-watermark; no stage should fire,
    # or `view != entries` and the real-vs-estimate comparison is apples/oranges.
    assert not res.compacted_any

    # 1) The ratio never leaves the calibration clamp.
    assert all(RATIO_MIN <= t.ratio <= RATIO_MAX for t in turns)

    # 2) Calibration never makes the estimate worse than the raw heuristic, and
    #    is strictly better by the end. (Once seeded, the ratio sits between the
    #    raw estimate and the truth, so |err_cal| <= |err_raw| every turn.)
    assert all(abs(t.err_cal) <= abs(t.err_raw) + 1e-9 for t in turns[1:])
    assert abs(final.err_cal) < abs(final.err_raw)

    # 3) It genuinely converged toward the provider's accounting: the final ratio
    #    sits closer to the observed per-turn target than the 1.0 seed did.
    target = final.observed
    assert target is not None
    assert abs(final.ratio - target) < abs(1.0 - target)

    # 4) Calibration removes most of the bias — a turn-count-robust statement of
    #    "converged" — and the residual is small in absolute terms too.
    assert abs(final.err_cal) < 0.3 * abs(final.err_raw)
    assert abs(final.err_cal) < 0.08

    # 5) Guard against a vacuous pass: the raw heuristic really was biased for
    #    this content, so calibration had a real job to do.
    assert res.steady_err_raw > 0.05
