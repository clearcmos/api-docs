# external-docs

Local cache of vendor API and product documentation, converted to Markdown for local search and reference. Each vendor fetcher is a standalone Python script and generated documentation remains untracked.

## Requirements

- Python 3.12 or newer
- `uv` for the reproducible development environment
- Network access to the selected vendor documentation source
- Vendor credentials only for authenticated sources such as SentinelOne

Clone the repository and install the locked runtime and development dependencies:

```bash
git clone https://github.com/clearcmos/external-docs.git
cd external-docs
uv sync --frozen --all-groups
```

PyYAML is the only non-standard runtime dependency. It is needed by fetchers whose vendors publish YAML-only specifications.

## Usage

List the available fetchers:

```bash
uv run python run.py --list
```

Run one or more vendors, or run every fetcher that does not require interactive configuration:

```bash
uv run python run.py cloudflare google
uv run python run.py --all
uv run python run.py --all --dry-run
uv run python run.py terraform -- --provider hashicorp/aws
```

Each vendor directory also contains a directly executable `fetch.py`:

```bash
uv run python cloudflare/fetch.py
uv run python oracle/fetch.py
```

All fetchers support `--dry-run`, `--force`, and `--verbose`. Authenticated fetchers document their additional arguments in `--help`. Generated Markdown, caches, downloaded specifications, and credential files are excluded from Git.

## Adding a vendor

1. Create `{vendor}/fetch.py` with the common command-line flags.
2. Prefer an authoritative machine-readable source before scraping HTML.
3. Write deterministic Markdown under `{vendor}/docs/`.
4. Preserve last-known-good output when discovery or retrieval is incomplete.
5. Add offline behavioral tests for parsing, cache transitions, and removal safety.
6. Update `CLAUDE.md` when the fetcher establishes a reusable pattern.

See `CLAUDE.md` for the complete fetcher contract and reference implementations.

## Development

```bash
uv run ruff check .
uv run ruff format --check .
for source in run.py */fetch.py; do uv run mypy "$source" || exit; done
uv run coverage run -m pytest
uv run coverage report --fail-under=85
uv run python -m py_compile run.py */fetch.py
for fetcher in */fetch.py; do uv run python "$fetcher" --help >/dev/null || exit; done
git diff --check
```

Tests use local fixtures and mocks rather than live vendor services. Dependency updates are deliberate and manual: update constraints when needed, run `uv lock --upgrade`, and verify the full suite before committing the new lockfile.

Git history serves as the changelog. This repository does not publish a Python package or release artifact; cloning the repository is the distribution method.

## License

MIT. See `LICENSE`.
