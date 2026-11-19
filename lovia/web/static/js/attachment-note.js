// A turn's attachments survive in stored history only as the note the server
// appends to the user text (mirrors `_attachment_note` in
// lovia/web/attachments.py — keep in sync). Split that text back into the
// visible message and renderable attachment views (image vs file inferred from
// the extension, like the Files panel), so a reloaded turn shows previews
// instead of the raw "[Attached: …]" lines. Works whether the image
// round-tripped inline (vision) or the turn flattened to text.

import { parseReport } from './subagent-report.js';
import { isImagePath } from './util.js';

// One path a line: a data directory may hold ", " (C:\Users\Li, Wei).
const NOTE_RE = /\n*\[Attached:\n([^]+?)\nRead with your file tools when relevant\.\]\s*$/;
// Before 0.9.239 uploads went to the workspace's uploads/, ", "-separated —
// safe there: a sanitized name never holds a comma.
const LEGACY_NOTE_RE =
  /\n*\[Attached in the workspace: (.+?)\. Read with your file tools when relevant\.\]\s*$/;
// An absolute path is an upload in the chat's folder (served by name), a
// relative one a workspace file. Windows spells drive and UNC paths too.
const ABSOLUTE_PATH_RE = /^(\/|[A-Za-z]:[\\/]|\\\\)/;

/**
 * @param {unknown} text A user turn's text.
 * @returns {{ text: string, attachments: { path?: string, name: string, kind: string }[] }}
 *   Uploads come back as `{ name, kind }` (served from the chat's folder),
 *   workspace files as `{ path, name, kind }`.
 */
export function splitAttachmentNote(text) {
  if (typeof text !== 'string') return { text: '', attachments: [] };
  // A task's report carries no uploads; a note-shaped tail is its own text.
  if (parseReport(text)) return { text, attachments: [] };
  const m = text.match(NOTE_RE);
  const hit = m || text.match(LEGACY_NOTE_RE);
  if (!hit) return { text, attachments: [] };
  const attachments = hit[1]
    .split(m ? '\n' : ', ')
    .map((p) => p.trim())
    .filter(Boolean)
    .map((path) => {
      const name = path.split(/[\\/]/).pop() || path;
      const kind = isImagePath(path) ? 'image' : 'file';
      return ABSOLUTE_PATH_RE.test(path) ? { name, kind } : { path, name, kind };
    });
  return { text: text.slice(0, hit.index).trimEnd(), attachments };
}
