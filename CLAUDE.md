# api-docs

Collection of fetcher scripts that pull API documentation from vendor sources and convert to local markdown. Each vendor lives in its own directory.

## Fetcher conventions

### Directory layout

```
{vendor}/
  fetch.py          # standalone fetcher, no third-party deps (stdlib only)
  docs/             # generated markdown output (gitignored)
  .cache.json       # SHA256 content hashes for incremental updates (gitignored)
  openapi.json      # or similar raw source artifact, when worth keeping
```

### CLI interface

Every fetch.py supports the same argparse flags:

- `--dry-run` -- show what would change, write nothing
- `--force` -- ignore cache, regenerate everything
- `--verbose` -- per-file logging

No other flags are required. Run with no args for a normal incremental sync.

### Finding the source

Prefer machine-readable specs over scraping HTML:

1. **OpenAPI/Swagger specs** -- best option. Check the vendor's GitHub org for `api-schemas`, `openapi-spec`, or similar repos. Cloudflare publishes theirs at `cloudflare/api-schemas`.
2. **Discovery documents** -- Google uses this pattern. A single index URL returns a list of all API specs.
3. **Sitemaps** -- if no spec exists, fetch `sitemap.xml` / `sitemap-index.xml` to discover all doc pages. Filter to the relevant subtree (e.g. `/api/resources/` vs SDK-specific duplicates).
4. **HTML scraping** -- last resort. Some sites serve markdown alternates via `<link rel="alternate" type="text/markdown">`.

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

If a vendor publishes YAML-only specs (like Okta does in the iss-utils repo), that's the one exception where `pyyaml` or `requests` is acceptable -- but prefer JSON sources when available.
