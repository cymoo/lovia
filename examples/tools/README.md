# `lovia.tools` examples

Each file is a small runnable script for one built-in tool family. Tools are
never added to an agent implicitly; these examples show the explicit opt-in
style lovia uses everywhere.

| File | Tool | Notes |
| --- | --- | --- |
| `01_http.py` | `lovia.tools.http.http_fetch` | Fetch a URL and let the model summarize it |
| `02_time.py` | `lovia.tools.time.now`, `sleep` | Give the model controlled access to time |
| `03_search.py` | `lovia.tools.search.duckduckgo_search` | Requires `pip install "lovia[ddg]"` |
| `04_human.py` | `HumanChannel` + `ask_human` | Let the model ask an operator for missing information |

For filesystem and shell tools (`read_file`, `write_file`, `edit_file`,
`list_files`, `grep_files`, `shell`), see `examples/19_workspace.py` and
`examples/20_workspace_agent.py`. They are wired automatically when an agent
has `workspace=Workspace.local(...)` configured, so path and command policy
live in one place.

```bash
python examples/tools/01_http.py
```
