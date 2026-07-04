"""Orchestrator-Workers workflow.

A central orchestrator LLM dynamically breaks down a complex task into
subtasks, dispatches each one to a worker agent, and finally synthesizes
all worker outputs into a cohesive result.

Unlike the Parallelization pattern (where subtasks are hard-coded), the
subtasks here are *determined at runtime* by the orchestrator based on the
specific input. This makes it suitable for open-ended tasks such as:

  • Multi-file code changes
  • Research tasks requiring information from multiple sources

Demo task: Research report on a technology topic — the orchestrator decides
which aspects to investigate; workers research each aspect in parallel.

Reference:
  https://www.anthropic.com/engineering/building-effective-agents#orchestrator-workers

Run::

    python examples/workflows/04_orchestrator_workers.py
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from lovia import Agent, Runner, model_from_env
from dotenv import load_dotenv

load_dotenv()

MODEL = model_from_env()  # LOVIA_MODEL etc.; raises with a hint if unset


# ---------------------------------------------------------------------------
# Output schemas
# ---------------------------------------------------------------------------


class Subtask(BaseModel):
    id: str
    title: str
    research_prompt: str


class SubtaskPlan(BaseModel):
    topic: str
    subtasks: list[Subtask]


class WorkerResult(BaseModel):
    subtask_id: str
    findings: str


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

orchestrator = Agent(
    name="Orchestrator",
    instructions=(
        "You are a research director. Given a broad topic, break it down into "
        "3–5 focused, non-overlapping research subtasks. For each subtask provide:\n"
        "  • id: a short slug (e.g. 'use-cases')\n"
        "  • title: a concise label\n"
        "  • research_prompt: the exact question a research analyst should answer\n\n"
        "Return a structured JSON plan."
    ),
    model=MODEL,
    output_type=SubtaskPlan,
)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

worker = Agent(
    name="ResearchWorker",
    instructions=(
        "You are a senior research analyst. Answer the research question thoroughly "
        "but concisely (2–4 paragraphs). Use your knowledge; do not fabricate citations."
    ),
    model=MODEL,
)


# ---------------------------------------------------------------------------
# Synthesizer
# ---------------------------------------------------------------------------

synthesizer = Agent(
    name="Synthesizer",
    instructions=(
        "You are a technical writer. Given a collection of research findings on "
        "different aspects of a topic, synthesize them into a single well-structured "
        "report with an executive summary followed by clearly labeled sections."
    ),
    model=MODEL,
)


# ---------------------------------------------------------------------------
# Orchestration logic
# ---------------------------------------------------------------------------


async def run_worker(subtask: Subtask) -> WorkerResult:
    result = await Runner.run(worker, subtask.research_prompt)
    return WorkerResult(subtask_id=subtask.id, findings=result.output)


async def orchestrate(topic: str) -> str:
    print(f"[Orchestrator] Planning research on: {topic!r}")
    plan_result = await Runner.run(orchestrator, f"Research topic: {topic}")
    plan: SubtaskPlan = plan_result.output

    print(f"[Orchestrator] Created {len(plan.subtasks)} subtasks:")
    for st in plan.subtasks:
        print(f"  • [{st.id}] {st.title}")

    print("\n[Workers] Executing subtasks in parallel …")
    worker_results = await asyncio.gather(*(run_worker(st) for st in plan.subtasks))

    id_to_title = {st.id: st.title for st in plan.subtasks}
    synthesis_prompt = f"Topic: {plan.topic}\n\n"
    for wr in worker_results:
        synthesis_prompt += f"### {id_to_title[wr.subtask_id]}\n{wr.findings}\n\n"
    synthesis_prompt += "Write the final report."

    print("[Synthesizer] Combining results …\n")
    final_result = await Runner.run(synthesizer, synthesis_prompt)
    return final_result.output


async def main() -> None:
    report = await orchestrate("WebAssembly (WASM) in server-side applications")
    print(report)


if __name__ == "__main__":
    asyncio.run(main())
