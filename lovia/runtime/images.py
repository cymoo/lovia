"""Images a provider refused: withheld from the view, remembered per chat.

When a provider refuses the images in a request
(:func:`~lovia.providers._images.image_rejection`), the run loop sends the
same call again with images replaced by their text marker — the
``[image: …]`` a model without vision already gets — instead of failing
the run (#532). Only the per-call view changes: the transcript and the
Session keep every image, as compaction keeps every entry.

Which images to withhold, per retry (``docs/design/vision-images.md``):

* the model takes no images (``"unsupported"``): all of them;
* these images were refused (``"image"``): first only the ones this run
  added since its last successful call — an image already sent is not the
  culprit — then, if that was not enough, all of them.

Every retry withholds at least one more image, so a call retries at most
twice for images. A decision is remembered only once a retried call
succeeds, in the run's carried ``context_state`` under ``"images"`` (the
checkpoint and the session segment keep it, whatever the context policy):
``blind`` holds the models that take no images, ``withheld`` the positions
of images replaced — a position, not the image's content, so the same
picture sent again later is tried again. A position carries a fingerprint
of its image and applies only while the image there still matches it
(edits, rewinds and handoffs move entries).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Any

from ..parts import ContentPart, ImagePart, TextPart, project_parts
from ..providers._images import ImageRejection
from ..transcript import InputEntry, ToolResultEntry, TranscriptEntry

SCRATCH_KEY = "images"

# (position in the session's entries, index of the part, fingerprint)
_Key = tuple[int, int, str]


def fingerprint(part: ImagePart) -> str:
    """A short identity for the image in a part: its URL, or its data's
    length and both ends — enough to tell two images at one position apart
    without hashing megabytes on every call."""
    if part.url is not None:
        source = part.url
    else:
        data = part.data or ""
        source = f"{len(data)}:{data[:64]}:{data[-64:]}"
    return hashlib.sha1(source.encode("utf-8", "replace")).hexdigest()[:16]


def _images(entry: TranscriptEntry) -> list[tuple[int, ImagePart]]:
    """The image parts an entry sends, with their index among its parts."""
    if isinstance(entry, InputEntry) and isinstance(entry.content, list):
        parts: list[ContentPart] = entry.content
    elif isinstance(entry, ToolResultEntry) and entry.parts:
        parts = entry.parts
    else:
        return []
    return [(i, p) for i, p in enumerate(parts) if isinstance(p, ImagePart)]


def _without(entry: TranscriptEntry, drop: set[int]) -> TranscriptEntry:
    """``entry`` with the parts at ``drop`` turned into their text marker, in
    place: a tool result keeps its other parts (``view_image``'s path) and
    its ``output``, a message its text and order."""

    def swap(parts: list[ContentPart]) -> list[ContentPart]:
        return [
            TextPart(project_parts([p])) if i in drop else p
            for i, p in enumerate(parts)
        ]

    if isinstance(entry, InputEntry) and isinstance(entry.content, list):
        return replace(entry, content=swap(entry.content))
    if isinstance(entry, ToolResultEntry) and entry.parts:
        return replace(entry, parts=swap(entry.parts))
    return entry


@dataclass
class _Sent:
    key: _Key | None  # None: not in the transcript (an injected entry)
    at: int  # transcript index; -1 when not in it


class ImageCall:
    """One model call's image decisions: what the chat remembers, plus what
    this call has withheld so far.

    ``transcript`` is the live transcript the call's view was built from;
    ``head`` the runner's own leading system entries (positions count from
    the session's first entry); ``mark`` the transcript index this run's
    last successful call reached (its images were accepted); ``model`` the
    endpoint-and-model key; ``tool_images`` whether the provider puts
    tool-result images on the wire at all.
    """

    def __init__(
        self,
        scratch: dict[str, Any],
        transcript: list[TranscriptEntry],
        *,
        head: int,
        mark: int,
        model: str,
        tool_images: bool,
    ) -> None:
        saved = scratch.get(SCRATCH_KEY)
        saved = saved if isinstance(saved, dict) else {}
        blind = saved.get("blind")
        self._blind: set[str] = (
            {b for b in blind if isinstance(b, str)}
            if isinstance(blind, list)
            else set()
        )
        self._remembered: set[_Key] = set()
        withheld = saved.get("withheld")
        for item in withheld if isinstance(withheld, list) else []:
            if (
                isinstance(item, list)
                and len(item) == 3
                and isinstance(item[0], int)
                and isinstance(item[1], int)
                and isinstance(item[2], str)
            ):
                self._remembered.add((item[0], item[1], item[2]))
        self._model = model
        self._mark = mark
        self._tool_images = tool_images
        # id(part) -> where it sits; parts reach the view by reference.
        self._where: dict[int, tuple[_Key, int]] = {}
        for at, entry in enumerate(transcript):
            for i, part in _images(entry):
                self._where[id(part)] = ((at - head, i, fingerprint(part)), at)
        present = {key for key, _ in self._where.values()}
        self._drop: set[_Key] = self._remembered & present
        self._drop_all = model in self._blind
        self._drop_unplaced = False
        self._added: set[_Key] = set()
        self._unsupported = False
        self._retries = 0

    def _selected(self, part: ImagePart) -> bool:
        if self._drop_all:
            return True
        found = self._where.get(id(part))
        return self._drop_unplaced if found is None else found[0] in self._drop

    def apply(self, view: list[TranscriptEntry]) -> list[TranscriptEntry]:
        """``view`` with every image decided so far turned into its marker;
        the same list when there is none."""
        out: list[TranscriptEntry] | None = None
        for n, entry in enumerate(view):
            drop = {i for i, part in _images(entry) if self._selected(part)}
            if drop:
                if out is None:
                    out = list(view)
                out[n] = _without(entry, drop)
        return view if out is None else out

    def _sent(self, view: list[TranscriptEntry]) -> list[_Sent]:
        sent: list[_Sent] = []
        for entry in view:
            if isinstance(entry, ToolResultEntry) and not self._tool_images:
                continue  # the adapter sends this result's text only
            for _, part in _images(entry):
                found = self._where.get(id(part))
                sent.append(_Sent(None, -1) if found is None else _Sent(*found))
        return sent

    def withhold_more(self, view: list[TranscriptEntry], kind: ImageRejection) -> int:
        """Decide which images of ``view`` (as sent) to withhold after a
        refusal of ``kind``; how many. 0: none left to withhold — the
        refusal stands."""
        sent = self._sent(view)
        if not sent:
            return 0
        if kind == "unsupported":
            self._unsupported = True
            self._drop_all = True
            step = sent
        else:
            new = [s for s in sent if s.key is None or s.at >= self._mark]
            step = new if self._retries == 0 and 0 < len(new) < len(sent) else sent
        for s in step:
            if s.key is None:
                self._drop_unplaced = True
            elif s.key not in self._drop:
                self._drop.add(s.key)
                self._added.add(s.key)
        self._retries += 1
        return len(step)

    def remember(self, scratch: dict[str, Any]) -> None:
        """After the call succeeded: keep this call's decisions for the chat."""
        if not self._unsupported and not self._added:
            return
        if self._unsupported:
            self._blind.add(self._model)  # every image of this model, from now on
        self._remembered |= self._added
        scratch[SCRATCH_KEY] = {
            "blind": sorted(self._blind),
            "withheld": [list(k) for k in sorted(self._remembered)],
        }
