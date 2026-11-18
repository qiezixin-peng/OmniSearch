# Provider-by-provider stability policy

OmniSearch does not treat one generic scraper as a stability solution for every source. Each
channel has its own upstream contract, client strategy and failure boundary.

Reliability classes exposed by `list_sources`:

- `public_api`: an official or source-operated public API. This is the stable core, but it is
  still rate-limited and monitored.
- `credentialed_api`: an official API that is usable only after its key/token is configured.
- `browser_best_effort`: direct browser discovery. It is useful and retained, but CAPTCHA,
  exit-IP reputation and markup changes mean it cannot honestly be guaranteed.

`list_sources` reports only policy-enabled sources and adds `runtime.configured`,
`runtime.healthy`, `runtime.status`, and `auto_selectable`. A disabled or unconfigured source is
not presented as part of the active pool merely because its code exists.

## Channel decisions

| Channel | Source-specific implementation/project | Decision |
|---|---|---|
| Google Search | Patchright + Scrapling + Ego Browser + Camoufox | Keep as required direct-browser discovery. No free project, including SearXNG or CloakBrowser, removes CAPTCHA/IP risk; failures remain visible. |
| DuckDuckGo | Direct browser adapters | Keep as a second browser index. DDGS was removed after real runs showed provenance mismatch and unstable completion; SearXNG also records DDG CAPTCHA failures. |
| Brave Search | Official Brave Search API when a key exists; browser stack otherwise | Prefer the official API. The no-key browser route is best-effort, not a stable API substitute. |
| GitHub | Official REST API; PyGithub/GithubKit are optional wrappers | Keep direct REST. Wrappers do not improve upstream availability; token support, retry, rate-limit handling and browser corroboration are retained. |
| GitLab | Official REST API; `python-gitlab` is the mature wrapper | Keep official API. A token improves limits, but public project search remains usable without one. |
| Hugging Face | Official Hub API; `huggingface_hub` is the official client | Keep the official endpoint. Retry transient connection failures; the client library is useful for repository operations but does not create a second index. |
| arXiv | Official Atom API; `lukasschwab/arxiv.py` is the mature wrapper | Keep official API with arXiv HTML/OpenAlex recovery. Respect the source's slow request cadence. |
| Crossref | Official REST API; `habanero` is the mature Python client | Keep as stable academic core. The direct adapter already implements the required endpoint and metadata normalization. |
| OpenAlex | Official REST API; `pyalex` is the mature wrapper | Keep with one-at-a-time pacing and rate-limit backoff. Configure a free OpenAlex key when available. |
| Semantic Scholar | Official Academic Graph API; `semanticscholar`/AllenAI clients | Registered but not auto-selected without a key. It is not allowed to silently use the low-limit anonymous path. |
| CORE | Official API | Keep only while its configured key is healthy. It is excluded automatically when unconfigured. |
| Europe PMC | Official REST API; `europepmc`/`pyEuropePMC` clients | Keep as stable biomedical specialist source. |
| Google Scholar | Direct browser + Crossref live-index recovery; `scholarly` is an alternative scraper | Keep as best-effort discovery. `scholarly` itself needs proxies and cannot guarantee CAPTCHA-free operation, so it is not a stability replacement. |
| Google Patents | Direct XHR/browser query; community patent scrapers | Keep only for patent-specific plans. Community scrapers use the same Google surface and do not remove blocking risk. |
| Stack Exchange | Official API; `StackAPI` is the mature wrapper | Keep as stable community API. |
| Hacker News | Algolia HN Search API | Keep as the search path. The Firebase HN API is useful for item retrieval but is not a full-text search replacement. |
| Reddit | PRAW + Reddit OAuth is the maintainable API path | Browser-only access is not auto-selected because real diagnostics were poor. Keep explicit browser access for logged-in/private-context research; enable automatic use only after OAuth is configured. |
| X | Official X API is the stable path but requires paid/credentialed access | Do not auto-select while the project is configured for no paid providers. Browser search remains explicit best-effort only. |
| Bluesky | Public AT Protocol/AppView API; `atproto` is the maintained SDK | Keep as a stable public social source. |
| YouTube | Official Data API v3 is the stable search path | Do not auto-select without a key. Browser search remains explicit for video-specific work. |
| RSSHub | Self-hosted RSSHub | Keep as a local specialist service. Stability is route-specific; it is not a general web-search replacement. |
| Internet Archive | Official Advanced Search API; `internetarchive` is the maintained client | Keep as the stable historical/archive source. |

## Rejected universal replacements

- SearXNG is valuable as a metasearch frontend, but its Google/DDG engines inherit upstream
  blocking and have open CAPTCHA/rate-limit reports.
- CloakBrowser, Camoufox, Patchright and Scrapling improve browser diversity and acquisition.
  They do not provide their own index and therefore cannot guarantee a search engine route.
- Paid SERP proxy services can buy a higher completion rate, but they are outside the project's
  no-paid-provider policy and are not silently enabled.

The operational rule is: official APIs form the stable core; direct browser engines provide
independent discovery and must report degradation; credentialed-only sources do not enter
automatic plans until configured.
