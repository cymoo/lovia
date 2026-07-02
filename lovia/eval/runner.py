"""The eval engine: :class:`Case` in, :class:`Report` out.

Non-determinism is faced head-on: a case runs ``samples`` times and passes
when the observed pass rate reaches ``pass_threshold``. There is no
retry-until-green — that only hides flakiness.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence, Union

from ..agent import Agent
from ..messages import Message, Usage
from ..parts import text_of
from ..runner import Runner
from .checks import Check, run_check
from .report import CaseResult, Report, SampleResult

# Either a ready agent or a zero-arg factory. Prefer a factory whenever the
# agent holds per-run state — a ScriptedProvider script, samples > 1 with
# stateful tools — so every sample starts fresh.
AgentSource = Union[Agent[Any], Callable[[], Agent[Any]]]


@dataclass
class Case:
    """One eval scenario: an input plus the checks its run must satisfy.

    ``name`` defaults to a snippet of the input. ``samples`` reruns the case
    to measure non-deterministic behavior; the case passes when at least
    ``pass_threshold`` of its samples pass. The remaining fields are forwarded
    to :meth:`~lovia.Runner.run` per sample.
    """

    input: str | list[Message]
    checks: Sequence[Check] = ()
    name: str = ""
    samples: int = 1
    pass_threshold: float = 1.0
    context: Any = None
    output_type: Any = None
    max_turns: int = 50
    timeout: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.samples < 1:
            raise ValueError("Case.samples must be >= 1")
        if not 0.0 <= self.pass_threshold <= 1.0:
            raise ValueError("Case.pass_threshold must be within 0..1")
        if not self.name:
            self.name = _derive_name(self.input)


async def evaluate(
    agent: AgentSource,
    cases: Case | Iterable[Case],
    *,
    concurrency: int = 4,
    fail_fast: bool = False,
    price: Callable[[Usage], float] | None = None,
) -> Report:
    """Run every case against ``agent`` and aggregate a :class:`Report`.

    Cases run concurrently up to ``concurrency``; a case's samples run
    sequentially. With ``fail_fast`` the suite instead runs case by case and
    stops at the first failure (the report contains only executed cases).
    A sample that raises is recorded as that sample's ``error`` — one broken
    case never aborts the suite. ``price`` maps a sample's :class:`Usage` to
    a cost figure, e.g. ``lambda u: u.input_tokens * 3e-6 + u.output_tokens * 15e-6``.
    """
    suite = [cases] if isinstance(cases, Case) else list(cases)

    if fail_fast:
        results: list[CaseResult] = []
        for case in suite:
            result = await _run_case(agent, case, price)
            results.append(result)
            if not result.passed:
                break
        return Report(cases=results)

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def bounded(case: Case) -> CaseResult:
        async with semaphore:
            return await _run_case(agent, case, price)

    return Report(cases=list(await asyncio.gather(*(bounded(c) for c in suite))))


async def _run_case(
    agent: AgentSource,
    case: Case,
    price: Callable[[Usage], float] | None,
) -> CaseResult:
    samples = [await _run_sample(agent, case, price) for _ in range(case.samples)]
    return CaseResult(
        name=case.name, samples=samples, pass_threshold=case.pass_threshold
    )


async def _run_sample(
    agent: AgentSource,
    case: Case,
    price: Callable[[Usage], float] | None,
) -> SampleResult:
    resolved = agent if isinstance(agent, Agent) else agent()
    sample = SampleResult()
    started = time.monotonic()
    try:
        coro = Runner.run(
            resolved,
            case.input,
            context=case.context,
            output_type=case.output_type,
            max_turns=case.max_turns,
        )
        result = await (asyncio.wait_for(coro, case.timeout) if case.timeout else coro)
    except asyncio.TimeoutError:
        sample.error = f"timeout: sample exceeded {case.timeout}s"
    except Exception as exc:
        sample.error = f"{type(exc).__name__}: {exc}"
    else:
        sample.output = result.output
        sample.turns = result.turns
        sample.usage = result.usage
        sample.checks = [await run_check(check, result) for check in case.checks]
    finally:
        sample.latency = time.monotonic() - started
    if price is not None:
        try:
            sample.cost = price(sample.usage)
        except Exception:
            sample.cost = None
    return sample


def _derive_name(input: str | list[Message]) -> str:
    if isinstance(input, str):
        text = input
    else:
        text = (
            next((text_of(m.content) for m in input if m.role == "user"), "") or "case"
        )
    text = " ".join(text.split())
    return text if len(text) <= 48 else text[:47] + "…"


__all__ = ["AgentSource", "Case", "evaluate"]
