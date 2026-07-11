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
names, and IDs that later turns may need. When a dropped tool output is \
likely to be needed again, note the ``recall_tool_result`` reference shown \
in its marker with a few words on what it holds (e.g. \
``recall_tool_result("ab12cd34ef567890"): full test log``). List only \
outputs worth recovering — do NOT enumerate every dropped result.

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
- Keep the whole summary compact — well under 2000 words. Prefer tight \
wording over exhaustive detail; drop low-value narration before dropping \
identifiers or decisions.
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
- Keep every still-relevant earlier fact; drop what the new events \
supersede, and prune entries that no longer matter (e.g. artifacts for \
work that has since completed).
- Integrate the new events: facts, decisions, and identifiers that later \
turns will plausibly need must appear verbatim — but do not hoard; \
transient details and dead ends can be compressed to a line or dropped.
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
