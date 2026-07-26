// Per-session cache of the follow-up chips shown after a reply, persisted in
// localStorage so a page refresh — or a round trip through another session —
// doesn't discard suggestions the server already spent a model call to make.
//
// Each entry is stamped with a signature of the exchange it was generated for
// (see chat.js `tailSig`). A read only returns items when the signature still
// matches the current tail, so a stale entry — the conversation moved on while
// we were away — is silently ignored rather than shown against the wrong reply.
// A mismatch is always fail-safe: no chips, never the wrong chips.

const KEY = 'lovia-followups';
// Entries are tiny (three short strings); the cap only bounds growth across a
// long history of sessions. The least-recently-generated fall off first.
const MAX_ENTRIES = 100;

function readAll() {
  try {
    const raw = localStorage.getItem(KEY);
    const map = raw ? JSON.parse(raw) : null;
    return map && typeof map === 'object' ? map : {};
  } catch {
    return {};
  }
}

function writeAll(map) {
  try {
    localStorage.setItem(KEY, JSON.stringify(map));
  } catch {
    /* quota exceeded or storage disabled — the cache is best-effort */
  }
}

/**
 * The cached chips for `sessionId`, but only if they were generated for the
 * exchange `sig` describes. Null on any miss (absent, stale, or malformed).
 * @param {string} sessionId
 * @param {string} sig Signature of the current conversation tail.
 * @returns {string[] | null}
 */
export function readFollowups(sessionId, sig) {
  if (!sessionId || !sig) return null;
  const rec = readAll()[sessionId];
  if (!rec || rec.sig !== sig) return null;
  return Array.isArray(rec.items) && rec.items.length ? rec.items : null;
}

/**
 * Remember `items` as the suggestions for `sessionId`'s current tail (`sig`).
 * No-op for an empty set — nothing to restore, and it would only crowd the cap.
 * @param {string} sessionId
 * @param {string} sig
 * @param {string[]} items
 */
export function writeFollowups(sessionId, sig, items) {
  if (!sessionId || !sig || !items?.length) return;
  const map = readAll();
  map[sessionId] = { sig, items, ts: Date.now() };
  const ids = Object.keys(map);
  if (ids.length > MAX_ENTRIES) {
    // Evict the oldest by generation time down to the cap.
    ids.sort((a, b) => (map[a].ts || 0) - (map[b].ts || 0));
    for (const id of ids.slice(0, ids.length - MAX_ENTRIES)) delete map[id];
  }
  writeAll(map);
}

/** Forget a session's cached chips — called when the session is deleted. */
export function dropFollowups(sessionId) {
  const map = readAll();
  if (sessionId in map) {
    delete map[sessionId];
    writeAll(map);
  }
}
