// Loads a deployer's extension modules (ChatUI.scripts) before the app starts,
// so whatever they register is in place for the first render. No DOM access:
// unit-tested under node with a fake importer.

/**
 * Import every module in `urls` in parallel. One that fails is logged and
 * reported; one still loading after `timeoutMs` stops holding up the start —
 * it keeps loading, and registers whenever it lands (the registries redraw on
 * registration, so late is fine).
 * @param {readonly string[]} urls
 * @param {{ timeoutMs?: number, importer?: (url: string) => Promise<unknown> }} [opts]
 * @returns {Promise<string[]>} The URLs that failed to load before the timeout.
 */
export async function loadExtensions(
  urls,
  { timeoutMs = 5000, importer = (url) => import(url) } = {},
) {
  /** @type {string[]} */
  const failed = [];
  await Promise.all(urls.map(async (url) => {
    const load = Promise.resolve()
      .then(() => importer(url))
      .then(
        () => 'loaded',
        (err) => {
          console.error(`lovia: extension ${url} failed to load:`, err);
          return 'failed';
        },
      );
    /** @type {ReturnType<typeof setTimeout> | undefined} */
    let timer;
    const late = new Promise((resolve) => {
      timer = setTimeout(resolve, timeoutMs, 'late');
    });
    const outcome = await Promise.race([load, late]);
    clearTimeout(timer);
    if (outcome === 'failed') failed.push(url);
    else if (outcome === 'late') {
      console.warn(`lovia: extension ${url} is still loading after ${timeoutMs} ms; starting without it`);
    }
  }));
  return failed;
}
