# `lovia.tools` examples

One self-contained snippet per tool. Each is a runnable Python file
that depends only on a model API key (most use OpenAI by default).

| File | Tool |
| --- | --- |
| `01_http.py`   | `lovia.tools.http.http_fetch` |
| `02_time.py`   | `lovia.tools.time.now`, `sleep` |
| `03_think.py`  | `lovia.tools.think.think` |
| `07_search.py` | `lovia.tools.search.web_search` (DuckDuckGo) |
| `08_todo.py`   | `lovia.tools.todo.TodoList` + `todo_tools` |
| `09_human.py`  | `lovia.tools.human.HumanChannel` + `ask_human` |

For filesystem and shell tools, see `examples/22_sandbox.py` and
`examples/23_sandbox_agent.py` — these use :mod:`lovia.sandbox`.

Run any of them with::

    python examples/tools/01_http.py
