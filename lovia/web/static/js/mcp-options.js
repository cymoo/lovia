// Settings → MCP: lovia's own options of one server — `defer` and a url's
// `trustEnv` — as controls over the editor's JSON. The JSON stays the truth:
// a control rewrites its key (a default removes it), and an edit by hand
// moves the control. Read the way lovia/web/config/mcp.py reads a document.

/** @param {unknown} v @returns {v is Record<string, any>} */
const isObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

/**
 * The entry of the one server `doc` names — inside `mcpServers`, or the
 * object itself as that — or null: no server, several, or not an object.
 * @param {unknown} doc
 * @returns {Record<string, any> | null}
 */
export function soleServer(doc) {
  if (!isObject(doc) || 'command' in doc || 'url' in doc) return null;
  const servers = 'mcpServers' in doc ? doc.mcpServers : doc;
  if (!isObject(servers)) return null;
  const entries = Object.values(servers);
  return entries.length === 1 && isObject(entries[0]) ? entries[0] : null;
}

/**
 * @typedef {{ defer: boolean, proxy: 'default' | 'on' | 'off', http: boolean }} McpOptions
 * `proxy` is `trustEnv`: unset, true or false. Only a url has one.
 */

/**
 * What a server entry says of the options. A value that isn't a boolean
 * reads as the default; saving it is refused, with why, by the server.
 * @param {Record<string, any>} spec
 * @returns {McpOptions}
 */
export function optionsOf(spec) {
  return {
    defer: spec.defer === true,
    proxy: spec.trustEnv === true ? 'on' : spec.trustEnv === false ? 'off' : 'default',
    // httpUrl / serverUrl: how two popular clients spell it.
    http: !!(spec.url || spec.httpUrl || spec.serverUrl),
  };
}

/**
 * `text` with one option of its one server set, pretty-printed; null when
 * `text` isn't a document naming one server. `defer` takes a boolean,
 * `proxy` one of {@link McpOptions}' values.
 * @param {string} text
 * @param {'defer' | 'proxy'} option
 * @param {boolean | McpOptions['proxy']} value
 * @returns {string | null}
 */
export function withOption(text, option, value) {
  let doc;
  try {
    doc = JSON.parse(text);
  } catch {
    return null;
  }
  const spec = soleServer(doc);
  if (!spec) return null;
  if (option === 'defer') {
    if (value === true) spec.defer = true;
    else delete spec.defer;
  } else if (value === 'default') {
    delete spec.trustEnv;
  } else {
    spec.trustEnv = value === 'on';
  }
  return JSON.stringify(doc, null, 2);
}

/** @typedef {{ env?: Record<string, string>, headers?: Record<string, string> }} Secrets */

/**
 * `text` with its one server's env and header values swapped: each value
 * still equal to `from`'s under the same key becomes `to`'s, pretty-printed.
 * How the editor shows the saved secrets (masked → stored) and hides them
 * again (stored → masked); a value typed over is left as typed. Null when
 * `text` isn't a document naming one server.
 * @param {string} text
 * @param {Secrets} from
 * @param {Secrets} to
 * @returns {string | null}
 */
export function swapSecrets(text, from, to) {
  let doc;
  try {
    doc = JSON.parse(text);
  } catch {
    return null;
  }
  const spec = soleServer(doc);
  if (!spec) return null;
  for (const field of /** @type {const} */ (['env', 'headers'])) {
    const now = spec[field];
    if (!isObject(now)) continue;
    for (const [key, value] of Object.entries(now)) {
      const a = from[field]?.[key];
      const b = to[field]?.[key];
      if (typeof a === 'string' && typeof b === 'string' && value === a) now[key] = b;
    }
  }
  return JSON.stringify(doc, null, 2);
}
