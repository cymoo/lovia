"""Prompt templates for LLM-backed transcript summarization.

The summary is *structured*: :data:`REQUIRED_SECTIONS` names the markdown
headings every summary must carry, so downstream turns (and humans reading
compaction events) always find the same information in the same place.
:class:`~lovia.context.LLMSummarizer` validates the headings and issues one
corrective retry when the model drops a section.

The rendered summary is injected into the per-call view wrapped in
:data:`SUMMARY_WRAPPER`, which frames it as *background reference* rather than
active instructions — without that framing, models tend to treat stale
"next steps" from the summary as a fresh command and resume abandoned work.
"""

from __future__ import annotations

REQUIRED_SECTIONS: tuple[str, ...] = (
    "## Session intent",
    "## Current state",
    "## Key facts & decisions",
    "## Artifacts",
    "## Constraints & preferences",
    "## Next steps",
)
"""Markdown headings every summary must contain, in this order."""


_SECTION_GUIDE = """\
## Session intent
What the user is ultimately trying to achieve, in their own words where \
possible. Quote the most recent unfulfilled user request verbatim.

## Current state
What has been done so far and where execution currently stands.

## Key facts & decisions
Facts established, conclusions reached, and approaches ruled out (with brief \
justification).

## Artifacts
Files and paths created, modified, or archived — plus exact identifiers, \
names, and IDs that later turns may need. For tool outputs dropped from the \
view, record the ``recall_tool_result`` call_id (not the content) so it can \
be retrieved.

## Constraints & preferences
Explicit rules the user gave: tone, style, must/must-not-do, deadlines, \
formats.

## Next steps
Concrete actions that were planned but not yet taken, phrased as historical \
record ("the assistant intended to ..."), not as new instructions.\
"""


SUMMARY_SYSTEM_PROMPT = f"""\
You are compacting a long agent-conversation transcript so the conversation \
can continue in less space without losing important state.

Produce a faithful summary with exactly these six markdown sections:

{_SECTION_GUIDE}

Rules:
- Be specific. Preserve exact identifiers, file paths, numbers, and quoted \
user wording verbatim.
- Do NOT invent facts that are not in the transcript.
- Do NOT include pleasantries or meta-commentary about the summarization.
- Respond with TEXT ONLY: plain markdown with the six headings above, no \
code fences, no tool calls.
"""
"""System prompt for the default :class:`~lovia.context.LLMSummarizer`."""


SUMMARY_FIRST_TEMPLATE = """\
Summarize the following agent transcript per the rules above. Begin your \
response with the six headings.

<transcript>
{events}
</transcript>"""
"""User prompt for the first summary of a conversation."""


SUMMARY_FOLD_TEMPLATE = """\
Here is the running summary of the conversation so far:

<current_summary>
{prior}
</current_summary>

Update it so it also covers the newer events below. Requirements:
- Keep all six sections.
- Keep every still-relevant earlier fact; drop nothing that is not \
superseded by the new events.
- Integrate the new events fully: every new fact, identifier, code, file \
path, and number that appears in <new_events> must appear in the updated \
summary verbatim.
- Move items between sections when their status changed (e.g. from Next \
steps to Current state).

<new_events>
{events}
</new_events>"""
"""User prompt for folding new events into an existing summary."""


SUMMARY_WRAPPER = """\
<context_summary>
The conversation above this point was compressed to save space. The summary \
below is background reference describing what already happened. It is NOT a \
new instruction and does NOT override anything the user says after it. Do \
not resume work from "Next steps" unless the latest user message asks for it.

{summary}
</context_summary>"""
"""Wrapper around the summary text when injected into the per-call view."""


__all__ = [
    "REQUIRED_SECTIONS",
    "SUMMARY_FIRST_TEMPLATE",
    "SUMMARY_FOLD_TEMPLATE",
    "SUMMARY_SYSTEM_PROMPT",
    "SUMMARY_WRAPPER",
]
