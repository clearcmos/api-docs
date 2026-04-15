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

### Finding the source

Prefer machine-readable specs over scraping HTML, in this order:

1. **OpenAPI/Swagger specs** -- best option. Check the vendor's GitHub org for `api-schemas`, `openapi-spec`, or similar repos.
2. **Discovery documents** -- Google uses this pattern. A single index URL returns a list of all API specs.
3. **llms.txt / llms-full.txt** -- increasingly common. A single text file listing all doc pages, sometimes with a companion file containing the full content. Auth0 and Anthropic use this pattern.
4. **Sitemaps** -- fetch `sitemap.xml` / `sitemap-index.xml` to discover all doc pages. Filter to the relevant subtree (e.g. `/api/resources/` vs SDK-specific duplicates).
5. **Embedded data in HTML** -- some SPAs embed spec data in the page (e.g. Bitwarden stores its OpenAPI spec in an Inertia.js `data-page` attribute). Extract with regex, don't try to render JS.
6. **HTML scraping** -- last resort. Some sites serve markdown alternates via `<link rel="alternate" type="text/markdown">`.

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

The one exception is YAML-only specs. If a vendor only publishes YAML (no JSON alternative), `pyyaml` is acceptable. Oracle is the current example of this.

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

When creating a new fetcher, copy these patterns from an existing one (cloudflare is a clean OpenAPI example, auth0 is a clean llms.txt example) rather than inventing new approaches.

## Gitignore

The `.gitignore` uses `**` glob patterns to recursively ignore generated artifacts across all vendor directories. When adding a new fetcher that saves source artifacts with a non-standard filename (anything other than `openapi.json`/`openapi.yaml`), add a matching pattern to `.gitignore` and verify it works with `git check-ignore -v <file>`.

If a file is already tracked by git, gitignore won't apply to it. Use `git rm --cached <file>` to untrack it first without deleting the file on disk.

Current patterns for source artifacts:

```
**/openapi*.json
**/openapi*.yaml
**/google-api-discovery.json
**/spec-index.json
```
