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

### Performance design order

Optimize in this order. Avoided work is more valuable than parallelized work:

1. **Narrow discovery** -- use the vendor's authoritative index/tree/spec and filter translations, SDK projections, duplicates, and out-of-scope paths before downloading bodies.
2. **Batch metadata and content** -- prefer bulk APIs and request all needed properties together (for example revision IDs during discovery, or wikitext + categories in one query).
3. **Skip unchanged sources** -- prefer ETags, `Last-Modified`, sitemap `lastmod`, revision IDs, Git blob SHAs, immutable document IDs, content-addressed spec paths, or a version + manifest hash.
4. **Verify the fast path** -- a source-level cache hit is valid only when every recorded output still exists. Missing output must force regeneration.
5. **Reduce transfer cost** -- request gzip for every HTTP fetch and deduplicate URLs/chunks before submission.
6. **Parallelize remaining I/O** -- use a bounded thread pool for independent requests and tune it against the actual origin.
7. **Parallelize CPU only when measured** -- large YAML parsing can use a process pool; ordinary HTML/Markdown conversion should stay in the main process.
8. **Write deterministically** -- collect concurrent results, then perform cache decisions, index construction, logging, and filesystem writes in stable order.

Do not replace a current authoritative source with a faster but stale packaged
snapshot. Do not call a run faster if it silently fetches fewer pages, loses
sections, or increases failures.

### HTTP and transport

- Every request sends a vendor-specific `User-Agent` and `Accept-Encoding: gzip`; decode gzip explicitly because `urllib` does not do it automatically.
- Use timeouts on every request. Retry transient connection failures, 429s, and retryable 5xx responses with bounded exponential backoff. Do not retry permanent 404s.
- Prefer a conditional GET when the origin honors `If-None-Match` or `If-Modified-Since`. Preserve the response validator in cache.
- Some CDNs ignore conditional GET but expose a stable ETag on HEAD. In that case, a cheap HEAD comparison may precede GET. Use the same representation-affecting headers (especially `Accept-Encoding`) for both requests.
- Treat `Last-Modified` as weaker than an ETag or revision ID. A sitemap without per-URL `lastmod` cannot prove that page bodies are unchanged.
- A large discovery/sitemap result may be cached separately for resumability or test runs, but normal syncs must validate or refresh it. Never let an arbitrary TTL hide removals indefinitely.
- For very large, single-host corpora, persistent per-thread `http.client.HTTPSConnection` objects can avoid thousands of TLS handshakes. Use this only when profiling justifies the extra complexity; `google-cloud` is the reference.
- Fetch discovery/index endpoints before launching page workers. If discovery is paginated by an opaque continuation token, that pagination is necessarily sequential; optimize the body-fetch phase instead.

### Caching and incremental updates

`.cache.json` normally maps an output-relative cache key to at least
`{"sha256": ..., "last_updated": ...}`. Entries may also carry `url`, `etag`,
`last_modified`, `revision`, `categories`, or other source metadata needed to
rebuild indexes without refetching unchanged bodies.

On each run:

- Fetch the source, compute SHA256 of the content that would be written.
- If the hash matches cache and the file exists on disk, skip it.
- Track new cache keys. After writing, diff old vs new keys to detect removals.
- `--force` skips cache loading so everything regenerates.

Large fetchers may store a source entry such as
`{"fingerprint": ..., "outputs": [...], "page_count": ...}`. The fingerprint
must include every input that can affect output: content files, data files,
navigation metadata, relevant configuration, and, when normal runs must notice
converter changes, a fetcher hash or explicit converter-schema version.
Otherwise a converter change requires `--force`.

Current source-fast-path patterns:

- NixOS: source ETags.
- Arch Wiki: per-article revision IDs plus cached categories.
- Terraform: provider version plus docs-manifest hash.
- Oracle: content-addressed per-API spec manifests.
- Authelia, filebrowser, and qwen-code: relevant Git tree blob SHAs plus the fetcher hash.
- `google-cloud`: per-page sitemap `lastmod`.

Commit a new source snapshot only after all required downloads and parsing
succeed. Save large caches atomically (`temporary file` + `os.replace`) and
checkpoint long-running mirrors so interruption does not discard hours of
work.

### Failure and removal safety

- A failed fetch is not a removal. If discovery still lists a page and a local file exists, carry forward its previous cache entry and catalogue metadata.
- Remove a file only when a complete authoritative discovery succeeds and its key is absent from that discovery.
- If discovery, a required source artifact, or a batch fails incompletely, leave the existing mirror untouched or preserve the affected scope.
- Removal detection must respect scope flags such as `--only`, `--limit`, language filters, and optional SDK trees. Exclusion by the current invocation is not deletion.
- Build READMEs from successfully fetched pages plus preserved last-known-good pages so a transient error cannot erase index entries.
- Deduplicated sources may feed multiple outputs; one failed shared chunk must preserve every dependent cached output.
- `--force` disables unchanged skipping, but fetchers that need the old cache for safe removal detection may still load it separately.

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

Use `concurrent.futures.ThreadPoolExecutor` for independent network requests.
The worker count is a measured per-origin setting, not a universal constant:

- Start around 8-16 workers; test 24 and 30 for origins that respond cleanly.
- Select the fastest count that completes the full corpus with zero unexpected failures. Re-run alone when a concurrent benchmark is distorted by local network contention.
- Back down when latency rises, 429s appear, or connections reset. Arch Wiki intentionally uses only two concurrent batches; higher counts were slower and unreliable.
- Do not add threads to one or two fixed requests unless measurement shows a useful gain.
- Submit each canonical URL once. If several routes share a webpack chunk or source file, fetch it once and fan the result out locally.
- Worker functions return data and metadata; the main thread owns shared cache structures and filesystem writes.
- Catch per-future failures and apply the preservation rules above. One exception must not discard unrelated successful work.
- Avoid unconditional per-request sleeps. Add bounded delay/backoff only when the origin demonstrates rate limiting.

Use `ProcessPoolExecutor` only for measured CPU-heavy conversion, currently
Oracle's large YAML specs. Keep process inputs/results serializable and perform
all writes in the parent process. The very large `google-cloud` fetcher is the
intentional exception: each process owns a persistent thread pool and
keep-alive connections.

### Benchmarking and acceptance

Measure both modes because they exercise different architectures:

- **Bootstrap/force run** -- empty cache or `--force`; measures discovery, all transfers, parsing, and generation.
- **Routine incremental run** -- populated cache and intact outputs; measures validators/manifests and changed-only work.

For each candidate change:

1. Record wall time, discovered page/spec count, fetched count, failure count, and output count before and after.
2. Run representative tests more than once; CDN and shared-network variance can dominate short fetchers.
3. Test worker counts independently rather than benchmarking several heavy fetchers at once.
4. Verify a warm source-cache hit, a missing-output repair, a changed-source refresh, and a transient-fetch preservation path.
5. Reject an optimization that changes corpus coverage or markdown hashes unexpectedly.

Minimum repository-wide verification after fetcher changes:

```sh
python3 -m py_compile */fetch.py
for f in */fetch.py; do python3 "$f" --help >/dev/null; done
git diff --check
```

Also run the changed fetcher with `--dry-run`; exercise a real populated-cache
run when introducing conditional requests or source-level fast paths, because
`--dry-run` intentionally does not save the cache.

### Common helpers across fetchers

Every fetcher independently implements these patterns (not shared code, but consistent logic):

- `sha256(content)` -- hash content for cache comparison
- `fetch_url(url)` -- stdlib `urlopen` with vendor User-Agent, gzip request/decoding, timeout, and error handling returning `None` on failure
- `load_cache()` / `save_cache()` -- read/write `.cache.json`
- `sanitize_filename(name)` -- regex cleanup to safe directory/file names
- `resolve_ref(ref, spec)` -- walk `$ref` pointers in OpenAPI specs
- `schema_to_markdown(schema, spec, depth, seen)` -- recursive schema renderer with circular-ref protection

When creating a new fetcher, copy these patterns from an existing one rather than inventing new approaches:

- **cloudflare** -- clean single OpenAPI spec example.
- **auth0** -- clean llms.txt example.
- **anthropic** -- llms.txt + markdown-alternate example. Fetches independent gzip-enabled pages concurrently with a bounded 30-thread pool, then performs cache decisions and writes deterministically on the main thread.
- **1password** -- sitemap + llms.txt union example for the current `www.1password.dev` root-path site; each page has a `.md` alternate fetched directly. The sitemap is authoritative, stale llms.txt `/overview` URLs are remapped to live parent landing pages, and page downloads use gzip plus a bounded 24-thread pool.
- **immich** -- dual-source example (OpenAPI spec + HTML scraping via sitemap). General-doc HTML pages are gzip-enabled and fetched concurrently with a bounded 16-thread pool; conversion and writes remain on the main thread.
- **clickup** -- dual-source example (two OpenAPI specs + sitemap HTML guides).
- **kandji** -- Postman collection parsing example.
- **okta** -- dual-source example (three YAML OpenAPI specs plus the current Okta Identity Engine DITA help site). Help pages are discovered from the live sitemap, fetched with 30 gzip-enabled threads, and use cheap parallel HEAD ETag checks on routine syncs so unchanged pages do not retransmit their bodies. The previous MadCap `HelpSystem.xml` discovery path is no longer used.
- **vikunja** -- Swagger 2.0 example (spec fetched live from a running instance; handles `definitions`, body params, and `responses[code].schema`).
- **keycloak** -- sitemap-filtered AsciiDoc HTML scraping example. No machine-readable spec is published; each guide page is an AsciiDoc-rendered HTML with content in `<div id="guide-body">`, and the monolithic reference manuals live under `/docs/latest/*/index.html` with content in `<div id="content">`. Converts to markdown via a custom `html.parser.HTMLParser` subclass that handles AsciiDoc-specific patterns (admonition blocks, listing blocks, heading anchors).
- **nixos** -- DocBook-rendered XHTML scraping example for the NixOS stable manual. Fetches the three monolithic sources (main manual, options reference, release notes) concurrently with gzip and conditional ETag requests; when all three return 304 and all recorded outputs exist, conversion is skipped completely. It slices each source into per-section markdown using a two-stage `html.parser.HTMLParser`: a `StructureSlicer` records byte ranges of `<div class="part|chapter|section|preface|appendix">` chunks (chapters in DocBook are descendants of their part, but close before the part does -- so two passes are needed), and an `OptionsParser` walks the variablelist `<dt>/<dd>` pairs (handling nested `<dl>` blocks inside option descriptions). Options are grouped by namespace (services / programs split into per-name files; everything else collected per top-level segment). Heading levels are derived from structural `<div>` nesting depth rather than the raw `<h2>` tags, since DocBook uses `<h2 class="title">` at every section level. Each chapter / option group / release version becomes its own markdown file, so `.cache.json` per-file hashes give granular "what changed" reporting in the run summary.
- **rippling** -- Docusaurus webpack extraction example. Route chunk URLs are deduplicated, gzip-enabled, and fetched with 16 threads; base64+zlib OpenAPI extraction and all writes happen deterministically after the downloads finish.
- **terraform** -- Terraform Registry API example. The provider version and docs-list manifest form a source snapshot; if both match and every cached output exists, the fetcher skips all per-document requests. Changed providers fetch document bodies with 16 threads.
- **oracle** -- OCI YAML OpenAPI example. Content-addressed spec paths provide a per-API source fingerprint, so routine runs download and parse only changed APIs. Changed YAML files download with 16 threads and parse in a bounded process pool before deterministic markdown generation.
- **ollama** -- Mintlify llms.txt example. Discovers all doc pages from `/llms.txt`, then fetches each page's `.md` alternate directly. Each page already serves clean markdown including embedded OpenAPI code blocks for endpoints, so no further conversion is needed - just strip Mintlify's "Documentation Index" preamble banner and prepend a Source link. The Mintlify `/index.md` welcome page lives at `docs/index.md` (not `docs/README.md`) so it does not collide with the auto-generated catalogue.
- **openai** -- llms.txt example with deeply nested URL paths preserved on disk. The top-level `/llms.txt` at `developers.openai.com` indexes hundreds of `.md` pages across five products (api, ads, apps-sdk, codex, commerce) with paths up to 11 segments deep (e.g. `api/reference/resources/audio/subresources/voice_consents/methods/create.md`). URL paths are mirrored verbatim under `docs/`, so the source URL doubles as the on-disk location. File/directory name collisions are intentional and harmless: an overview page like `api/docs/guides/agents.md` lives next to its `agents/` subdirectory of child pages, since `.md` distinguishes them. The index also lists `.txt` sub-llms files in "Documentation sets"; those are skipped via a URL-suffix filter. A page that appears in both "Documentation sets" and a product section (e.g. `apps-sdk/reference.md`) is deduped to keep the product-specific section. Generates one top-level `README.md` (with every page grouped by llms.txt section) plus per-product `README.md` files; no per-subdirectory READMEs given the depth. Page requests use gzip, 30 threads, and cached ETags; transient failures preserve the last known-good cache entry.
- **qwen-code** -- GitHub Trees API + raw markdown example. The rendered Nextra site does not serve markdown alternates, so the fetcher goes to the source repo (`QwenLM/qwen-code-docs`) and pulls `website/content/en/**/*.{md,mdx}` directly from `raw.githubusercontent.com` with 16 threads. The GitHub Trees API at `?recursive=1` returns the full repo tree in one request; results were small enough not to truncate but the code checks for and warns on truncation. Relevant blob SHAs plus the fetcher hash form a source fingerprint, so an unchanged routine sync skips all raw-file downloads and conversion after verifying the recorded outputs. Per-file `section`/`link` tracking keeps `<section>/index.mdx` files grouped under their section's auto-generated README (rendered as `_index.md` inside that directory, since `README.md` is reserved for the catalogue). MDX frontmatter (`title`, `description`, `author`, `date`, `tags`) is parsed and surfaced in the page header.
- **youtube** -- Google Devsite HTML scraping example for the YouTube Data API v3 reference at `/youtube/v3/docs`. Complements `google/docs/youtube-v3.md` (raw discovery doc) by capturing the human-readable narrative (parameter descriptions, examples, errors) the discovery doc omits. Discovers endpoint URLs from the landing page's `<div class="devsite-article-body">` (52 method pages) and derives resource overview pages (`/youtube/v3/docs/{resource}`) since those are not linked from the index. Auxiliary pages (`/youtube/v3/docs/errors`) are listed explicitly. Each page's body lives in `<div class="devsite-article-body">`; a `DevsiteExtractor(html.parser.HTMLParser)` converts to markdown: handles `<section>` nesting, `<devsite-code><pre>` code blocks, `<div class="note|caution|warning|...">` callouts as blockquote prefixes (the HTML already contains its own `<b>Note:</b>` label so we don't add our own), inline-bold-inside-code suppression (`<code><strong>foo</strong></code>` → `` `foo` ``, not `` `**foo**` ``), and `<table class="responsive details">` parameter tables rendered as definition-style lists (divider rows like "Required parameters" / "Optional parameters" / "Filters" become bold subheadings, since description cells contain nested lists/code that break real markdown tables). Decorative `<span class="material-icons">` and `<devsite-iframe>` Try-It panels are skipped. Code-wrapped Markdown links (`` `[name](url)` `` which renders as literal text) are post-processed to `[`name`](url)`. Page title comes from the article-body `<h1>` if present, else the `<title>` tag stripped of the " | Google for Developers" Devsite suffix.
- **arch** -- MediaWiki API + wikitext-to-markdown example for the Arch Linux Wiki. Discovery uses `generator=allpages` with `prop=revisions&rvprop=ids`, collecting every title and current revision ID in the same paginated requests. Revision IDs and categories are cached per article, so routine syncs fetch wikitext only for new or changed revisions. Changed wikitext + categories are bulk-fetched 50 pages at a time via `prop=revisions|categories&rvprop=content&rvslots=main&titles=A|B|...` (cllimit continuations followed transparently). Two gzip-enabled batches run concurrently; higher concurrency caused repeatable connection resets from the public wiki and is intentionally avoided. Translations are filtered out by detecting the standard "Title (Italiano)" / "Title (Русский)" / etc. suffix pattern, and the same check filters out localized category names. The wikitext converter is a focused regex+line-based renderer (no external deps): handles headings (`== ... ==`), bold/italic, internal/external links (with cross-wiki redirects to en.wikipedia.org for `[[Wikipedia:...]]`), lists, `<pre>`/`<syntaxhighlight>`/`{{bc}}`/`{{hc}}`/`{{File}}` code blocks, admonitions (`{{Note|...}}` / `{{Warning|...}}` / `{{Tip|...}}`), inline-code via `{{ic|...}}` (with `{{!}}` pipe-escape preservation), `{{Pkg|...}}` / `{{AUR|...}}` / `{{man|N|name}}` / `{{Wikipedia|...}}` link templates, and `{{Related articles start}}...{{Related articles end}}` blocks. Unknown templates are left as-is so they remain visible. **Layout:** articles are flat at `docs/{Title_with_underscores}.md` (matching the [tsgates/arch-wiki-markdown](https://github.com/tsgates/arch-wiki-markdown) convention -- subpages flatten via `_`, so `Pacman/Tips and tricks` → `Pacman_Tips_and_tricks.md`); each article carries a `**Categories:** [Cat1](./_categories/Cat1.md), ...` line under its source URL; per-category index files live under `docs/_categories/`, mirroring the wiki's category graph without file duplication. Tracking categories like "Pages or sections flagged with Template:..." are filtered out. **Why flat over letter-buckets:** the established mirror precedent (tsgates) is flat, the [llms.txt convention](https://llmstxt.org/) puts content files flat with a separate index, and [TreeRAG/CORPUS2SKILL hierarchical retrieval](https://www.infoq.com/articles/building-hierarchical-agentic-rag-systems/) only beats flat at much larger scales than ~2k articles.
- **radarr** -- Servarr OpenAPI 3.0 example. Fetches the generated `openapi.json` straight from the source tree (`Radarr/Radarr` `develop` branch, `src/Radarr.Api.V3/openapi.json`) rather than scraping the Swagger UI at `radarr.video/docs/api/`. Structurally a clone of **cloudflare** (same `resolve_ref` / `schema_to_markdown` / tag-grouping), with three Servarr-specific tweaks: every operation has `summary`/`operationId`/`description` set to `null` (so `.get(..., default)` is wrong -- use `operation.get("summary") or fallback`, and omit empty fields entirely); there is no top-level `tags` array (tags come only from each operation); and the single server URL is a template (`{protocol}://{hostpath}`) resolved against its `variables` defaults to `http://localhost:7878`. Auth is `X-Api-Key` header or `apikey` query param, surfaced in the top README.
- **sonarr** -- identical Servarr OpenAPI fetcher to **radarr**, pointed at `Sonarr/Sonarr` `develop` (`src/Sonarr.Api.V3/openapi.json`, default host `localhost:8989`). The only diffs from `radarr/fetch.py` are the spec URL, app name, source-docs URL, and User-Agent. The Sonarr v3 spec applies to both Sonarr v3 and v4.
- **sabnzbd** -- single-page HTML wiki scraping example (sabnzbd.org has no machine-readable spec and no MediaWiki API -- it is a custom CMS). The entire reference lives in one `<div class="wiki-content">` on `/wiki/configuration/5.0/api`, structured as `<h1 id>` groups (Introduction / Queue / History / Status / Other functions) containing `<h2 id>` functions, where the `<h2>` id doubles as the api `mode` value. A focused `WikiParser(html.parser.HTMLParser)` walks the body into a flat list of `WikiSection`s (one per heading) which `group_sections` then folds into h1 groups, each h2 becoming its own `{anchor}.md` file (h2s without an id, only in the Introduction group, slug from the title). The parser handles: `<pre>` and `<figure class="highlight">` code blocks (syntax-highlight `<span>`s stripped, fence language taken from `data-lang`/`language-*`); the `<span class="label">` return-type badge on a function heading captured as a **Returns:** line (e.g. True/False); parameter `<table>`s with nested `<ul>`/`<br>`/`<code>` in cells rendered as single-line markdown rows with `<br>`-joined list items (literal newlines collapsed, since md table cells cannot span lines); navigation tables (`Function`/`Description` header) at each group head dropped in favour of the auto-generated index; and malformed unclosed `<tr>` rows (the source has one) committed on the next `<tr>`/`</table>` so no row is lost. Internal `#anchor` links render as plain text (their targets live in sibling files, so a real link would dangle); `/wiki/...` and external links become absolute markdown links. Because all output derives from one fetched page, `.cache.json` stores a per-section SHA256 so the run summary still reports granular adds/updates. The h1 group's own lead-in body (before its first h2) becomes the group README preamble.
- **opencode** -- Astro Starlight `.md` MDX-alternate example. The site is a SolidStart SSR shell with no machine-readable spec, but every doc page is served as raw MDX at a `.md` alternate (`/docs/cli.md`, `/docs/index.md`), so the fetcher pulls those directly rather than scraping the rendered HTML. **Discovery is sidebar-driven:** the `.md` files carry no frontmatter (Starlight strips it), so titles, order, and section grouping come from parsing the sidebar nav (`<ul class="top-level">`) of one rendered docs page -- a single `NAV_TOKEN_RE` alternation captures group labels (`<div class="group-label">`) and doc links (`<a href="/docs/..."><span>Title</span>`) in document order, deduped by slug. `sitemap.xml` is a safety net: it filters out translations (locale prefixes are auto-detected as the segments appearing as bare `/docs/<seg>/` index pages -- ar, de, zh-cn, etc.) and appends any English page missing from the sidebar under an "Other" group. **MDX-to-markdown** is a two-pass, fence-aware converter (everything inside ```` ``` ```` fences is left verbatim, so plist/XML/keybind examples that look like tags survive): pass one drops leading `import`/`export` module statements, unwraps `<Tabs>`/`<TabItem label="X">` (-> `**X**`)/`<Steps>`, turns `<code>` + `{"..."}` JSX string literals into inline code, converts `<a>` tags (literal `href="..."` -> markdown link, `href={expr}` -> plain inner text since the module var is unresolved, e.g. `<a href={typesUrl}><code>Agent[]</code></a>` -> `` `Agent[]` ``), and strips `<nobr>`; pass two converts Starlight `:::note`/`:::tip[Title]`/`:::caution` asides to GitHub alert blockquotes (`> [!NOTE]`), prefixing body lines -- including any fenced code block inside the aside -- with `> `. **Layout:** flat `docs/{slug}.md` (the docs are single-segment, no nesting); the intro page lives at `docs/index.md` (Mintlify/ollama convention, so it does not collide with the auto-generated `docs/README.md` catalogue, which is grouped by sidebar section). English-only; translations are skipped.
- **twitch** -- Jekyll two-shape scraping example for `dev.twitch.tv/docs/api` (no spec, no llms.txt, sitemap.xml 403s, and the twitchdev GitHub org publishes no OpenAPI). **Guide pages** hold content in `<section class="text-content">` and are discovered by BFS-crawling `/docs/api/*` links from the landing page -- the sidebar nav misses `/docs/api/moderation`, so crawling is required; the sidebar is still parsed to order the README's guide list, with crawl-only extras appended. **The reference** is one ~1.4 MB page of 149 endpoints, each a `<section class="doc-content">` holding `left-docs` (an `<h2>` whose id is the endpoint's stable anchor, h3 subsections, parameter/response tables) and `right-code` (example requests/responses); the index table at the top maps each anchor to its Resource group (Ads, Bits, Chat, ...), which becomes the directory layout `docs/reference/{resource}/{anchor}.md` plus per-resource READMEs. One `MarkdownConverter(html.parser.HTMLParser)` handles both shapes: Rouge code blocks in both flavors (`div.language-X.highlighter-rouge` with the language on the outer div, `figure.highlight` with it on `code[data-lang]`), response-body tables that encode field nesting as leading `\xa0` runs in the first cell (the run is preserved so hierarchy survives; all other `\xa0` become spaces), cells with `<ul>`/`<br>`/`<p>` flattened to one physical line with `<br>` separators, CloudCannon `<a class="editor-link" href="cloudcannon:...">` pencil links skipped as a subtree, `<span class="pill">NEW</span>` badges as `**NEW**`, `<details>`/`<summary>` unfolded with a bold summary line, and zero-width spaces (which pollute a few headings) stripped globally. Links are rewritten against the generated tree: `/docs/api/reference#anchor` and bare `#anchor` resolve to the per-endpoint file, `/docs/api/*` to the local page file, and everything else is absolutized to `dev.twitch.tv` (with percent-encoding, since one vendor href contains a literal space). Endpoint h2s shift to per-file h1s via a `heading_offset=-1`.
- **filebrowser** -- MkDocs Material GitHub-source example. filebrowser.org serves no markdown alternates and no llms.txt, so the fetcher pulls `www/docs/**/*.md` from the `filebrowser/filebrowser` repo via the Trees API, plus the four repo-root files the mkdocs nav references (`CHANGELOG.md`, `CONTRIBUTING.md`, `CODE-OF-CONDUCT.md`, `SECURITY.md` -- the site build copies them in as `changelog.md` etc., and the fetcher maps them the same way). Relevant blob SHAs plus the fetcher hash form a source fingerprint, so unchanged routine syncs verify their recorded outputs and skip all raw-file downloads and conversion. The `nav:` block of `mkdocs.yml` is parsed with a small indentation-based parser (constrained subset: `- Title: page`, `- page`, `- Group:`) to supply page titles, ordering, and the top README's nested grouping; pages without a nav title fall back to their first heading. Conversions are minimal: pymdownx content tabs (`=== "Label"`) unwrap into bold labels with the 4-space body dedented (fences inside tabs become valid top-level fences), `md_in_html` div wrappers (grid cards) are dropped since raw HTML blocks suppress GFM rendering of their contents, and admonitions already use GitHub-style `> [!NOTE]` callouts so they pass through. Relative links between pages are left untouched because the on-disk mirror matches the source layout; targets outside the page set become GitHub blob links (e.g. `LICENSE`) and `static/` assets become absolute site URLs.
- **google-cloud** -- full-site Devsite mirror example, and by far the largest fetcher (~120k pages at default scope, ~520k with `--include-sdk-reference`). Mirrors docs.cloud.google.com driven by its 60-shard sitemap index; the shards return HTTP 500 unless the request sends `Accept-Encoding: gzip`, so every request does. Sitemap entries carry per-URL `<lastmod>`, which is stored in `.cache.json` alongside the content sha and drives incremental sync (a page is refetched only when its lastmod changes, it is new, or its file is missing) -- so a routine sync only pays for the sitemap download plus changed pages. Deviations from house conventions, on purpose due to scale: fetching runs in a ProcessPoolExecutor (`--procs`, default half the cores) where each process runs a persistent ThreadPoolExecutor (`--threads`, default 12) and each thread keeps a keep-alive `http.client.HTTPSConnection` (urllib would TLS-handshake per request); `--dry-run` prints the plan without fetching page content; only one top-level `docs/README.md` catalogue is generated (per-directory READMEs would add tens of thousands of files); the cache is compact JSON (~100 MB) saved atomically every two minutes so an interrupted run resumes. Conversion slices each page to its `<div class="devsite-article-body">` before feeding an `html.parser` converter (adapted from **youtube**, same Devsite platform): handles `devsite-selector` tab sections, `aside` callouts as blockquotes, `<pre syntax="X">` code fences, REST-reference "Fields" tables (colspan divider rows) as definition lists vs real GFM tables otherwise, and skips Devsite chrome (`ul.toc` mini-TOCs, version `select`s, `devsite-lightbox` duplicate images, `label`/`devsite-nav-buttons` location choosers). Links inside `<pre>` stay plain text (BigQuery SQL syntax diagrams hyperlink every keyword). Titles come from the JSON-LD `headline` or `<title>` since the `<h1>` sits outside the article body. Output mirrors URL paths (`compute/docs/instances` -> `compute/docs/instances.md`). Scope flags: default is every English page EXCEPT the ~410k per-class client-library reference pages (`{lang}/docs/reference/*` for java/nodejs/ruby/python/dotnet/php/cpp/go -- machine-generated projections of the same API surface the REST reference covers), leaving ~120k pages / ~4 GB of product docs + REST API reference + gcloud CLI reference; `--include-sdk-reference` adds the per-class trees back; `--include-translations` adds `?hl=xx` pages (doubles the corpus); `--only prefix1,prefix2` filters by URL path prefix; `--limit N` and `--sitemap-cache` (reuse `.sitemap-pages.json`) are for testing. Removal detection is scope-aware: it deletes only files whose URL left the sitemap and that are inside the currently configured scope (never files merely excluded by flags), and requires all shards fetched and no `--only`/`--limit`.
- **authelia** -- Hugo-source example with full shortcode expansion. The site serves `.md` alternates and an llms.txt, but both are unsuitable as primary sources: the alternates strip frontmatter and concatenate title+description into the H1, and llms.txt omits every page deeper than two section levels (~300 pages, including all OpenID Connect client guides and the CLI reference). The fetcher instead mirrors `docs/content/` from `authelia/authelia` master via the Trees API. Relevant content/data blob SHAs plus the fetcher hash form a source fingerprint, so unchanged routine syncs verify their recorded outputs and skip all raw-file downloads and conversion. Frontmatter supplies title/description/weight (README ordering) and `aliases` (so `/i/traefik`-style links resolve); `_index.md` files become structure metadata for per-section READMEs, not pages; drafts are skipped. Blog URLs come from the `[permalinks] blog = "/blog/:slug/"` Hugo config, i.e. slugified titles (`4.39: Release Notes` -> `4.39-release-notes`), not directory names -- `hugo_urlize()` reproduces this and natural source paths are kept as aliases so source-relative links still resolve. Hugo shortcodes are expanded inline: `sitevar` -> its `nojs` fallback (globally, including inside fences, and including `{{</* ... */>}}`-escaped ones inside tab inners which Hugo double-renders), `confkey` -> an italic Type/Syntax/Default/Required line (attr values may contain `>`, so attributes are matched as quoted pairs), `callout` -> GitHub alert blockquotes, `envTabs`/`sessionTabs`/`details` -> bold labels, the data-driven tables (`table-config-keys`, `table-i18n-*`, `table-totp-support`, `hashing-pbkdf2-*`, `csp`, `supported-product`, `latest`) rendered from `docs/data/*.json`, the large `oidc-common`/`oidc-escape-hatch-claims-hydration` includes reproduced as markdown (their relative faq/config defaults are emitted as the absolute paths the rendered site produces), `figure`/`picture` (attributes may span multiple lines, hence a DOTALL pre-pass) -> image markdown with site-absolute URLs, `github-link` -> a blob link pinned to `v{latest}` from `misc.json`, and `{{< print >}}` literals sentinel-protected so inner shortcode examples survive verbatim. Unrecognized shortcodes are left as-is and counted in the run summary. Link rewriting resolves relative links against the bundle dir for leaf bundles (`x/index.md`) vs the parent dir for plain pages, then re-relativizes against the on-disk tree; links wholly inside inline code spans are skipped while `` [`code`](target) `` labels are still rewritten; unknown internal targets absolutize to `www.authelia.com`. Sections mirrored: overview, configuration, integration, contributing, blog, roadmap, reference (policies, information, and contributors intentionally skipped). Both this fetcher and filebrowser load the previous cache even under `--force` so file removals are still detected (only the unchanged-skip is disabled).
- **obs** -- Sphinx (sphinx_rtd_theme) reStructuredText-source example for the OBS Studio C API docs at `docs.obsproject.com`. The site publishes no OpenAPI spec, and `sitemap.xml` / `llms.txt` both just return the index HTML (no sphinx-sitemap extension), but two Sphinx artifacts give a clean path in: the authoritative page list is the `docnames` array inside `searchindex.js` (`Search.setIndex({...})`, parsed as JSON -- 44 pages), and each page's raw source is served at `/_sources/{docname}.rst.txt`. The RST source is the primary source rather than the rendered HTML because a C signature like `int astrcmpi(const char *str1, const char *str2)` arrives verbatim in the RST, whereas the rendered page explodes it into dozens of nested syntax-highlight `<span>`s. Output is flat `docs/{docname}.md` (docnames already encode hierarchy via prefixes like `reference-libobs-util-*`, and mirror the extensionless source URL); the intro page is `docs/index.md` (like ollama/opencode, so it does not collide with the generated `docs/README.md`). **The README tree** is built by recursively parsing every `.. toctree::` starting at `index.rst` -- the three index toctrees supply the caption groups (Core Concepts / API Reference / Additional Resources), `reference-core-objects` / `reference-libobs-util` / `reference-libobs-graphics` supply nested children, `Title <docname>` entries carry explicit titles (else the page's H1 is used), external-URL entries pass through as links, and pages in `docnames` reachable from no toctree (e.g. the experimental `reference-canvases`) are collected under "Other". **The RST-to-markdown converter** (`RstConverter`, stdlib only, no docutils) runs two corpus-wide passes: pass 1 collects each page's H1 title and every `.. _label:` anchor mapped to the slug of the heading it precedes (for cross-page `:ref:` resolution); pass 2 converts, with a recursive indentation-aware block walker (`collect_body` dedents a directive/field body, `render_subblock` re-runs the walker on it). Section levels are assigned by first-seen underline character per page (RST's own rule). The C/Python domain object directives (`function`/`member`/`type`/`struct`/`enum`/`macro`/`py:function`) render as a bold-monospace signature (`**`struct dstr`**`, keyword prefixed for type/struct/enum) followed by the body -- deliberately not headings, since 1335 functions would swamp the outline; the signature stays greppable. `:param:`/`:return:`/`:rtype:` field lists (1925 of them) become `- **name**: desc` bullets with continuation lines joined; `.. code:: cpp|lua` become fenced blocks; a paragraph ending in `::` introduces an RST literal block (the marker becomes `:` or is dropped, and the following indented block is emitted verbatim in a plain fence -- this preserves the directory-tree and CMakeLists examples that would otherwise be reflowed into one line); `versionadded`/`versionchanged` become `*New in version X.*` italics, `deprecated` a `> [!WARNING]` alert, and admonitions (`danger` -> CAUTION, etc.) GitHub alert blockquotes; `.. toctree::` bodies are dropped (rendered in the README instead). Inline conversion is placeholder-stashed in order: `` ``literal`` `` -> `` `code` ``, inline external links `` `text <url>`_ ``, phrase references `` `phrase`_ `` and OBS's footnotes `` `[1]`_ `` (both resolve against `.. _name: url` named targets -- whose names can contain slashes, spaces and brackets, so a greedy `NAMED_TARGET_RE` backtracks to the last `: ` before the URL), then cross-ref roles (`:c:func:` -> `` `name()` ``, `:c:member:`/`:c:type:`/`:c:struct:`/`:c:macro:` -> inline code, `:ref:` -> a link to the target heading's `docname.md#slug`, `:doc:` -> `docname.md`, `:wiki:` -> `obsproject.com/wiki/...`, `~`-prefixed targets shown as their last dotted component), then bare `word_` named refs. Per-file `.cache.json` SHA256 gives granular add/update reporting; no source artifact is persisted (searchindex.js and the RST are fetched in-memory), so no gitignore entry is needed.
- **obs-websocket** -- committed-JSON-spec example for the obs-websocket 5.x runtime control protocol (distinct from the `obs` fetcher, which is the libobs C/plugin API). The project commits both a machine-readable `docs/generated/protocol.json` and a rendered `docs/generated/protocol.md` to `master`, so the fetcher pulls both from `raw.githubusercontent.com` (no build, no HTML scraping). `protocol.json` (`{enums, requests, events}`) is the primary source: requests and events are grouped by their `category` field (general, scenes, scene items, inputs, outputs, stream, record, etc.) and rendered one file per item under `docs/requests/{category-slug}/{RequestType}.md` and `docs/events/{category-slug}/{EventType}.md`, with `requestFields`/`responseFields`/`dataFields` as GFM tables (pipe-escaped; `valueRestrictions` and a non-placeholder `valueOptionalBehavior` folded into the description, but the literal `"Unknown"` behavior -- 174 of 214 optional fields -- suppressed as noise). The 7 `enums` render to a single `docs/enums.md` (identifier/value/description/added-in table). The connection/auth/OpCodes narrative lives only in `protocol.md`, so `extract_intro` slices the span from `## General Intro` to `## Enumerations` into `docs/index.md` (ollama/opencode convention, avoids colliding with the generated `docs/README.md` catalogue, whose grouped request/event lists replace protocol.md's own TOC whose anchors would not survive the split). Cache keys are the docs-relative path (uniform across pages and every README), so removal detection and empty-category-dir pruning fall out naturally; both spec files are fetched in-memory with nothing persisted, so no gitignore entry is needed. Standard `--dry-run`/`--force`/`--verbose`.

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
**/.sitemap-pages.json
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
