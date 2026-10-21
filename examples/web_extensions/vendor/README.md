# Vendored modules

The ES module builds `extension.js` draws its docs view with, copied unchanged
from the npm packages. `index.html`'s import map points the bare names
(`preact`, `htm/preact`, …) at them. Renamed from `.mjs` to `.js` so any
static server sends them as JavaScript.

| File | Import name | Package | Version | License | Source (in the npm package) |
| --- | --- | --- | --- | --- | --- |
| `preact.js` | `preact` | preact | 11.0.1 | MIT | `dist/preact.mjs` |
| `preact-hooks.js` | `preact/hooks` | preact | 11.0.1 | MIT | `hooks/dist/hooks.mjs` |
| `htm.js` | `htm` | htm | 3.1.1 | Apache-2.0 | `dist/htm.mjs` |
| `htm-preact.js` | `htm/preact` | htm | 3.1.1 | Apache-2.0 | `preact/index.mjs` |

To upgrade: `npm pack preact@<version>` (or `htm@<version>`), extract, and copy
the same files over.
