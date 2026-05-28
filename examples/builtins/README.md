# `lovia.builtins` examples

One self-contained snippet per built-in. Each is a runnable Python file
that depends only on a model API key (most use OpenAI by default).

| File | Tool |
| --- | --- |
| `01_http.py`   | `lovia.builtins.http.http_fetch` |
| `02_time.py`   | `lovia.builtins.time.now`, `sleep` |
| `03_think.py`  | `lovia.builtins.think.think` |
| `04_fs.py`     | `lovia.builtins.fs.FileSystem` |
| `05_shell.py`  | `lovia.builtins.shell.Shell` (+ `allowlist`) |
| `06_code.py`   | `lovia.builtins.code.PythonRunner` |
| `07_search.py` | `lovia.builtins.search.web_search` (DuckDuckGo) |
| `08_todo.py`   | `lovia.builtins.todo.TodoList` + `todo_tools` |
| `09_human.py`  | `lovia.builtins.human.HumanChannel` + `ask_human` |
| `10_all_in_one.py` | A small agent that uses several builtins together. |

Run any of them with::

    python examples/builtins/01_http.py
