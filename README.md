# api-docs

Local cache of vendor API documentation, converted to markdown for grep-friendly reference.

## Usage

Each vendor directory contains a `fetch.py` that pulls the latest docs:

```bash
python3 cloudflare/fetch.py
python3 google/fetch.py
python3 oracle/fetch.py    # requires pyyaml
```

All fetchers support `--dry-run`, `--force`, and `--verbose`. Run periodically to stay current.

## Vendors

| Vendor | Source type | Notes |
|--------|-----------|-------|
| anthropic | llms.txt | Claude Code docs from code.claude.com |
| auth0 | llms.txt + llms-full.txt | Authentication, Management, MyAccount, MyOrganization APIs |
| bitwarden | Embedded OpenAPI in SPA | CLI help (GitHub raw) + Vault Management API |
| cloudflare | OpenAPI spec (GitHub) | Single large spec, ~1400 endpoints |
| google | Discovery documents | ~300 APIs fetched concurrently |
| immich | OpenAPI spec + sitemap | API reference + general docs via HTML scraping |
| oracle | YAML OpenAPI specs | ~80 OCI APIs, requires `pyyaml` |

## Adding a new vendor

1. Create `{vendor}/fetch.py` -- stdlib-only Python, same CLI flags (`--dry-run`, `--force`, `--verbose`).
2. Find the best machine-readable source (OpenAPI spec > discovery doc > llms.txt > sitemap > HTML scraping).
3. Output markdown to `{vendor}/docs/`, one file per endpoint, grouped by category.
4. Use `.cache.json` with SHA256 hashes for incremental updates.
5. If the fetcher saves source artifacts locally (spec files, index JSON), add a gitignore pattern for them.

See `CLAUDE.md` for detailed conventions and patterns.
