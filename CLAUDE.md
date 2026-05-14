# api-docs

Collection of fetcher scripts that pull API documentation from vendor sources and convert to local markdown. Each vendor lives in its own directory.

## Maintaining this file

Keep CLAUDE.md up to date as the repo evolves. When adding a new fetcher, changing conventions, or deprecating a pattern, update the relevant sections here. Remove anything that no longer applies. This file should always reflect the current state of the project, not its history.

## Fetcher conventions

### Directory layout

```
{vendor}/
  fetch.py          # standalone fetcher, no third-party deps (stdlib only)
  docs/             # generated markdown output (gitignored)
  .cache.json       # SHA256 content hashes for incremental updates (gitignored)
```

Some fetchers also save an intermediate source artifact (e.g. an OpenAPI spec or index JSON) next to `fetch.py`. These are fetched at runtime and must be gitignored -- see the gitignore section below.

### CLI interface

Every fetch.py supports the same argparse flags:

- `--dry-run` -- show what would change, write nothing
- `--force` -- ignore cache, regenerate everything
- `--verbose` -- per-file logging

No other flags are required. Run with no args for a normal incremental sync.

Some fetchers have additional flags for vendor-specific needs:

- `sentinelone` requires `--base-url` and `--cookie`/`--cookie-file` (authenticated console).
- `terraform` accepts `--provider org/name` (interactive picker if omitted with a TTY).

### Finding the source

Prefer machine-readable specs over scraping HTML, in this order:

1. **OpenAPI/Swagger specs** -- best option. Check the vendor's GitHub org for `api-schemas`, `openapi-spec`, or similar repos. Self-hosted apps often serve a Swagger spec from the running server itself (e.g. Vikunja exposes it at `/api/v1/docs.json` on any instance); point the fetcher at a public demo deployment with a `--spec-url` flag.
2. **Discovery documents** -- Google uses this pattern. A single index URL returns a list of all API specs.
3. **llms.txt / llms-full.txt** -- increasingly common. A single text file listing all doc pages, sometimes with a companion file containing the full content. Auth0 and Anthropic use this pattern.
4. **Sitemaps** -- fetch `sitemap.xml` / `sitemap-index.xml` to discover all doc pages. Filter to the relevant subtree (e.g. `/api/resources/` vs SDK-specific duplicates).
5. **Embedded data in HTML** -- some SPAs embed spec data in the page (e.g. Bitwarden stores its OpenAPI spec in an Inertia.js `data-page` attribute). Extract with regex, don't try to render JS.
6. **Postman collections** -- some vendors publish API docs via Postman. Fetch the collection JSON and parse the folder/endpoint structure. Kandji uses this pattern.
7. **Terraform Registry API** -- for Terraform provider docs. The v1/v2 registry API returns provider metadata and doc content. Provider-agnostic with `--provider` flag.
8. **Webpack bundle extraction** -- some Docusaurus SPAs embed content in webpack chunks. Extract route mappings from main.js, fetch chunks, decompress base64+zlib OpenAPI specs. Rippling uses this pattern.
9. **Authenticated APIs** -- some vendors require auth to access their API spec (e.g. SentinelOne management console). Accept credentials via CLI flags, never hardcode tenant URLs.
10. **HTML scraping** -- last resort. Some sites serve markdown alternates via `<link rel="alternate" type="text/markdown">`.

### Caching and incremental updates

`.cache.json` maps a cache key to `{"sha256": ..., "last_updated": ...}`. On each run:

- Fetch the source, compute SHA256 of the content that would be written.
- If the hash matches cache and the file exists on disk, skip it.
- Track new cache keys. After writing, diff old vs new keys to detect removals.
- `--force` skips cache loading so everything regenerates.

### Markdown output

- One directory per logical grouping (tag, API name, resource category).
- Each directory gets a `README.md` index listing its endpoints with relative links.
- Each endpoint gets its own `.md` file named by method and path: `{method}-{path-slugified}.md`.
- Top-level `docs/README.md` indexes all groups.

### OpenAPI-to-markdown conversion

When working from an OpenAPI spec:

- Resolve `$ref` pointers on the fly. Track seen refs to avoid infinite recursion on circular schemas.
- `allOf`: merge properties and required fields from all sub-schemas.
- `oneOf`/`anyOf`: list variants inline, cap at ~5 to keep output readable.
- Nested objects: expand one level of properties, then summarize deeper nesting as `object (N properties)`.
- Parameters go in a markdown table (name, in, type, required, description).
- Group endpoints by tag. An endpoint with multiple tags appears in each tag's directory.

### stdlib-only policy

Fetchers use only Python standard library (`urllib`, `json`, `hashlib`, `argparse`, etc.). No `requests`, no `pyyaml`, no `beautifulsoup`. This keeps the scripts zero-dependency and runnable anywhere Python 3.12+ is available.

The one exception is YAML-only specs. If a vendor only publishes YAML (no JSON alternative), `pyyaml` is acceptable. Current examples: clickup (V3 spec), langfuse, okta, oracle.

### Concurrency

For vendors with many specs to download (Google, Oracle), use `concurrent.futures.ThreadPoolExecutor` from stdlib. Cap `max_workers` to a reasonable number (8-30) and add brief sleeps for single-server targets that might rate-limit.

### Common helpers across fetchers

Every fetcher independently implements these patterns (not shared code, but consistent logic):

- `sha256(content)` -- hash content for cache comparison
- `fetch_url(url)` -- stdlib `urlopen` with User-Agent header and error handling returning `None` on failure
- `load_cache()` / `save_cache()` -- read/write `.cache.json`
- `sanitize_filename(name)` -- regex cleanup to safe directory/file names
- `resolve_ref(ref, spec)` -- walk `$ref` pointers in OpenAPI specs
- `schema_to_markdown(schema, spec, depth, seen)` -- recursive schema renderer with circular-ref protection

When creating a new fetcher, copy these patterns from an existing one rather than inventing new approaches:

- **cloudflare** -- clean single OpenAPI spec example.
- **auth0** -- clean llms.txt example.
- **1password** -- sitemap + llms.txt union example; each page has a `.md` alternate fetched directly.
- **immich** -- dual-source example (OpenAPI spec + HTML scraping via sitemap).
- **clickup** -- dual-source example (two OpenAPI specs + sitemap HTML guides).
- **kandji** -- Postman collection parsing example.
- **vikunja** -- Swagger 2.0 example (spec fetched live from a running instance; handles `definitions`, body params, and `responses[code].schema`).
- **keycloak** -- sitemap-filtered AsciiDoc HTML scraping example. No machine-readable spec is published; each guide page is an AsciiDoc-rendered HTML with content in `<div id="guide-body">`, and the monolithic reference manuals live under `/docs/latest/*/index.html` with content in `<div id="content">`. Converts to markdown via a custom `html.parser.HTMLParser` subclass that handles AsciiDoc-specific patterns (admonition blocks, listing blocks, heading anchors).
- **nixos** -- DocBook-rendered XHTML scraping example for the NixOS stable manual. Fetches three monolithic pages (main manual, options reference, release notes) and slices each into per-section markdown using a two-stage `html.parser.HTMLParser`: a `StructureSlicer` records byte ranges of `<div class="part|chapter|section|preface|appendix">` chunks (chapters in DocBook are descendants of their part, but close before the part does -- so two passes are needed), and an `OptionsParser` walks the variablelist `<dt>/<dd>` pairs (handling nested `<dl>` blocks inside option descriptions). Options are grouped by namespace (services / programs split into per-name files; everything else collected per top-level segment). Heading levels are derived from structural `<div>` nesting depth rather than the raw `<h2>` tags, since DocBook uses `<h2 class="title">` at every section level. Each chapter / option group / release version becomes its own markdown file, so `.cache.json` per-file hashes give granular "what changed" reporting in the run summary.
- **ollama** -- Mintlify llms.txt example. Discovers all doc pages from `/llms.txt`, then fetches each page's `.md` alternate directly. Each page already serves clean markdown including embedded OpenAPI code blocks for endpoints, so no further conversion is needed - just strip Mintlify's "Documentation Index" preamble banner and prepend a Source link. The Mintlify `/index.md` welcome page lives at `docs/index.md` (not `docs/README.md`) so it does not collide with the auto-generated catalogue.
- **qwen-code** -- GitHub Trees API + raw markdown example. The rendered Nextra site does not serve markdown alternates, so the fetcher goes to the source repo (`QwenLM/qwen-code-docs`) and pulls `website/content/en/**/*.{md,mdx}` directly from `raw.githubusercontent.com`. The GitHub Trees API at `?recursive=1` returns the full repo tree in one request; results were small enough not to truncate but the code checks for and warns on truncation. Per-file `section`/`link` tracking keeps `<section>/index.mdx` files grouped under their section's auto-generated README (rendered as `_index.md` inside that directory, since `README.md` is reserved for the catalogue). MDX frontmatter (`title`, `description`, `author`, `date`, `tags`) is parsed and surfaced in the page header.
- **arch** -- MediaWiki API + wikitext-to-markdown example for the Arch Linux Wiki. Discovers titles via `list=allpages&apnamespace=0&apfilterredir=nonredirects` (paginated with `apcontinue`), then bulk-fetches wikitext + categories together 50 pages at a time via `prop=revisions|categories&rvprop=content&rvslots=main&titles=A|B|...` (cllimit continuations followed transparently). Translations are filtered out by detecting the standard "Title (Italiano)" / "Title (Русский)" / etc. suffix pattern, and the same check filters out localized category names. The wikitext converter is a focused regex+line-based renderer (no external deps): handles headings (`== ... ==`), bold/italic, internal/external links (with cross-wiki redirects to en.wikipedia.org for `[[Wikipedia:...]]`), lists, `<pre>`/`<syntaxhighlight>`/`{{bc}}`/`{{hc}}`/`{{File}}` code blocks, admonitions (`{{Note|...}}` / `{{Warning|...}}` / `{{Tip|...}}`), inline-code via `{{ic|...}}` (with `{{!}}` pipe-escape preservation), `{{Pkg|...}}` / `{{AUR|...}}` / `{{man|N|name}}` / `{{Wikipedia|...}}` link templates, and `{{Related articles start}}...{{Related articles end}}` blocks. Unknown templates are left as-is so they remain visible. **Layout:** articles are flat at `docs/{Title_with_underscores}.md` (matching the [tsgates/arch-wiki-markdown](https://github.com/tsgates/arch-wiki-markdown) convention -- subpages flatten via `_`, so `Pacman/Tips and tricks` → `Pacman_Tips_and_tricks.md`); each article carries a `**Categories:** [Cat1](./_categories/Cat1.md), ...` line under its source URL; per-category index files live under `docs/_categories/`, mirroring the wiki's category graph without file duplication. Tracking categories like "Pages or sections flagged with Template:..." are filtered out. **Why flat over letter-buckets:** the established mirror precedent (tsgates) is flat, the [llms.txt convention](https://llmstxt.org/) puts content files flat with a separate index, and [TreeRAG/CORPUS2SKILL hierarchical retrieval](https://www.infoq.com/articles/building-hierarchical-agentic-rag-systems/) only beats flat at much larger scales than ~2k articles.

## Gitignore

The `.gitignore` uses `**` glob patterns to recursively ignore generated artifacts across all vendor directories. When adding a new fetcher that saves source artifacts with a non-standard filename (anything other than `openapi.json`/`openapi.yaml`), add a matching pattern to `.gitignore` and verify it works with `git check-ignore -v <file>`.

If a file is already tracked by git, gitignore won't apply to it. Use `git rm --cached <file>` to untrack it first without deleting the file on disk.

Current patterns for source artifacts:

```
**/openapi*.json
**/openapi*.yaml
**/google-api-discovery.json
**/spec-index.json
**/collection.json
**/api-spec.json
**/provider-docs.json
**/provider-index.json
```

## Unified runner

`run.py` at the repo root discovers all `{vendor}/fetch.py` scripts dynamically and provides a single entry point:

```
python run.py                                    # interactive fzf picker (multi-select with Tab)
python run.py cloudflare okta                    # run specific vendors
python run.py --all                              # run all (skips vendors needing extra args)
python run.py --all --dry-run                    # dry-run everything
python run.py --list                             # list discovered vendors
python run.py terraform -- --provider org/name   # vendor-specific args after --
```

Vendors marked with `# requires-interactive` or having `required=True` argparse args are skipped in `--all` mode. The `--` separator forwards everything after it to the vendor script.

When adding a new fetcher, `run.py` picks it up automatically -- no registration needed.
