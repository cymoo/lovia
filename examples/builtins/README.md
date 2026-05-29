# `lovia.builtins` examples

One self-contained snippet per built-in. Each is a runnable Python file
that depends only on a model API key (most use OpenAI by default).

| File | Tool |
| --- | --- |
| `01_http.py`   | `lovia.builtins.http.http_fetch` |
| `02_time.py`   | `lovia.builtins.time.now`, `sleep` |
| `03_think.py`  | `lovia.builtins.think.think` |
| `07_search.py` | `lovia.builtins.search.web_search` (DuckDuckGo) |
| `08_todo.py`   | `lovia.builtins.todo.TodoList` + `todo_tools` |
| `09_human.py`  | `lovia.builtins.human.HumanChannel` + `ask_human` |

For filesystem and shell tools, see `examples/22_workspace.py` and
`examples/23_workspace_agent.py` — these use :mod:`lovia.workspace`.

Run any of them with::

    python examples/builtins/01_http.py
