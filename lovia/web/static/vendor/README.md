# Vendored front-end libraries

Self-hosted so the bundled UI makes no CDN requests — it must work on
intranets, offline, and in regions where public CDNs are unreachable. All
four are optional at runtime: if one fails to load, the UI falls back to
escaped plain text and skips highlighting/diagrams.

| File | Package | Version | License | Source |
| --- | --- | --- | --- | --- |
| `purify.min.js` | DOMPurify | 3.2.6 | Apache-2.0 OR MPL-2.0 | <https://cdn.jsdelivr.net/npm/dompurify@3.2.6/dist/purify.min.js> |
| `marked.min.js` | marked | 4.3.0 | MIT | <https://cdn.jsdelivr.net/npm/marked@4.3.0/marked.min.js> |
| `highlight.min.js` | highlight.js | 11.9.0 | BSD-3-Clause | <https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js> |
| `mermaid.min.js` | mermaid | 11.6.0 | MIT | <https://cdn.jsdelivr.net/npm/mermaid@11.6.0/dist/mermaid.min.js> |

Integrity (sha384 — the same values pinned as `integrity=` attributes on
the script tags in `templates/index.html`):

```
purify.min.js     sha384-JEyTNhjM6R1ElGoJns4U2Ln4ofPcqzSsynQkmEc/KGy6336qAZl70tDLufbkla+3
marked.min.js     sha384-QsSpx6a0USazT7nK7w8qXDgpSAPhFsb2XtpoLFQ5+X2yFN6hvCKnwEzN8M5FWaJb
highlight.min.js  sha384-F/bZzf7p3Joyp5psL90p/p89AZJsndkSoGwRpXcZhleCWhd8SnRuoYo4d0yirjJp
mermaid.min.js    sha384-zkWMJO4sgpPUzyuOgDx8HB/K55glbAwajEpk1Go2NWRuPkPA/wIhoEJTuSkmOYrV
```

To upgrade one: download the new pinned file, compute its hash
(`openssl dgst -sha384 -binary <file> | openssl base64 -A`), update **both**
this file and the `integrity=` attribute in `templates/index.html`, then
re-test a chat with markdown, code blocks, and a mermaid diagram.
`tests/web/test_vendor_integrity.py` fails with the correct hash in its
message if any of the three drift apart.
