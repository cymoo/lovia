# Effective Agent Patterns

Examples demonstrating the agentic-system patterns from Anthropic's article
[Building effective agents](https://www.anthropic.com/engineering/building-effective-agents).

## Patterns

| File | Pattern | Description |
|------|---------|-------------|
| `01_prompt_chaining.py` | Prompt Chaining | Sequential LLM calls with a programmatic gate |
| `02_routing.py` | Routing | Classify input, dispatch to a specialist agent |
| `03_parallelization.py` | Parallelization | Sectioning (parallel analysis) + Voting (majority verdict) |
| `04_orchestrator_workers.py` | Orchestrator-Workers | Orchestrator dynamically plans subtasks, workers execute them |
| `05_evaluator_optimizer.py` | Evaluator-Optimizer | Generator + evaluator feedback loop |
| `06_autonomous_agent.py` | Autonomous Agent | Tool-using agent that self-directs until task completion |

## Quick start

```bash
# From the repo root
python examples/workflows/01_prompt_chaining.py
```

Each script reads configuration from the `.env` file at the project root.
Set `DEFAULT_MODEL` to override the model used (default: `deepseek-v4-pro`).
