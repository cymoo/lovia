# Calibration-ratio convergence study

A live illustration of how `CompactionState.ratio` — the calibration
multiplier in `lovia/context/compaction.py` — walks from its `1.0` seed to the
steady-state estimator error for a given content type, turn by turn.

- **`run.py`** — the driver. Grows a homogeneous transcript per scenario,
  drives the real `Compaction.compact()` for the estimate, and reads the real
  `prompt_tokens` back from the live DeepSeek endpoint for ground truth.
- **`samples.py`** — deterministic, fabricated conversation content for the five
  scenarios (English, Chinese, Chinese+English, Chinese+code, English+code).

This is a study script, not a pytest test (it makes real API calls and is not
collected by `test_*.py` discovery). Run it explicitly:

```bash
LOVIA_LIVE_TESTS=1 .venv/bin/python tests/context/ratio_calibration/run.py --turns 18
```

It writes the report to **`docs/ratio-calibration.md`**. Because content is
fabricated (no live model output feeds back) and `prompt_tokens` is a pure
function of the input text, a re-run reproduces the same tables.
