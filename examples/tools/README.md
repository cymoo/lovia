# `lovia.tools` examples

Each file is a small runnable script for one built-in tool family. Tools are
never added to an agent implicitly; these examples show the explicit opt-in
style lovia uses everywhere.

| File | Tool | Notes |
| --- | --- | --- |
| `01_fetch.py` | `lovia.tools.web_fetch` | Read a page as Markdown (with its images); read a JSON API |
| `02_time.py` | `lovia.tools.now` | Give the model the current time |
| `03_search.py` | `lovia.tools.duckduckgo_search` | Requires `pip install "lovia[ddg]"`; swap in `tavily_search()` (`TAVILY_API_KEY`) for the keyed backend |
| `04_human.py` | `HumanChannel` + `ask_human` | Let the model ask an operator for missing information |

For filesystem and shell tools (`read_file`, `write_file`, `edit_file`,
`list_files`, `grep_files`, `shell`), see `examples/19_workspace.py` and
`examples/20_workspace_agent.py`. They are wired automatically when an agent
has `workspace=Workspace.local(...)` configured, so path and command policy
live in one place.

```bash
python examples/tools/01_fetch.py
```
