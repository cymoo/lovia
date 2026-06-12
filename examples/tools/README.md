# `lovia.tools` examples

One self-contained snippet per tool. Each is a runnable Python file
that depends only on a model API key (most use OpenAI by default).

| File | Tool |
| --- | --- |
| `01_http.py`   | `lovia.tools.http.http_fetch` |
| `02_time.py`   | `lovia.tools.time.now`, `sleep` |
| `07_search.py` | `lovia.tools.search.web_search` (DuckDuckGo) |
| `09_human.py`  | `lovia.tools.human.HumanChannel` + `ask_human` |

For the filesystem and shell tools (`read_file`, `write_file`, `edit_file`,
`list_files`, `grep_files`, `shell`), see `examples/22_workspace.py` and
`examples/23_workspace_agent.py` — they are wired automatically when an
agent has `workspace=Workspace.local(...)` configured.

Run any of them with::

    python examples/tools/01_http.py
