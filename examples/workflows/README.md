# Workflow examples

These scripts show how to build agentic systems without giving up ordinary
Python control flow. They are inspired by Anthropic's
[Building effective agents](https://www.anthropic.com/engineering/building-effective-agents),
but each pattern is implemented with lovia primitives: `Agent`, `Runner`,
structured output, tools, and small orchestration functions.

| File | Pattern | When to reach for it |
|------|---------|----------------------|
| `01_prompt_chaining.py` | Prompt chaining | A task has clear sequential stages and each stage can validate the previous one |
| `02_routing.py` | Routing | You can classify work up front and send it to a specialist |
| `03_parallelization.py` | Parallelization | Independent analyses can run side by side, then be combined or voted on |
| `04_orchestrator_workers.py` | Orchestrator-workers | One model should plan subtasks and delegate them dynamically |
| `05_evaluator_optimizer.py` | Evaluator-optimizer | You want a generator to improve against explicit feedback |
| `06_autonomous_agent.py` | Autonomous agent | The agent should use tools repeatedly until it decides the job is done |

Start with prompt chaining if you are new to agent workflows; it is the most
predictable pattern. Reach for the autonomous agent last, after the task really
needs open-ended tool use.

```bash
# From the repo root
python examples/workflows/01_prompt_chaining.py
```

Each script reads configuration from the `.env` file at the project root.
Set `LOVIA_MODEL` to choose the model used by the examples (required), e.g. `LOVIA_MODEL="openai:gpt-5.5"`.
