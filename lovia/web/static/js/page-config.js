// The page settings the server renders into #app-config (lovia/web/ui.py),
// parsed once. Module scripts run after the document is parsed, so it is
// readable at import time — before any module's boot code needs it.

/**
 * @typedef {object} PageConfig
 * @property {string} [base_path] The mount prefix every API call carries.
 * @property {string} [static_base] The versioned prefix of the bundled assets.
 * @property {string | null} [login_url] Where a signed-out user logs in.
 * @property {{ default_lang?: string | null,
 *   messages?: Record<string, Record<string, string>> }} [i18n]
 */

/** @returns {PageConfig} */
function read() {
  if (typeof document === 'undefined') return {}; // under node, in tests
  const text = document.getElementById('app-config')?.textContent;
  if (!text) return {};
  try {
    const cfg = JSON.parse(text);
    return cfg && typeof cfg === 'object' ? cfg : {};
  } catch (err) {
    console.error('app-config:', err);
    return {};
  }
}

/** @type {Readonly<PageConfig>} */
export const pageConfig = Object.freeze(read());
