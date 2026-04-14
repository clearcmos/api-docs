# api-docs

Local cache of vendor API documentation, converted to markdown for grep-friendly reference.

## Usage

Each vendor directory contains a `fetch.py` that pulls the latest docs:

```bash
cd cloudflare && python3 fetch.py
cd google && python3 fetch.py
```

All fetchers support `--dry-run`, `--force`, and `--verbose`. Run periodically to stay current.

## Adding a new vendor

1. Create `{vendor}/fetch.py` -- stdlib-only Python, same CLI flags.
2. Find the best machine-readable source (OpenAPI spec > discovery doc > sitemap > HTML scraping).
3. Output markdown to `{vendor}/docs/`, one file per endpoint, grouped by category.
4. Use `.cache.json` with SHA256 hashes for incremental updates.

See `CLAUDE.md` for detailed conventions.
