# OmniDesk `plugin-arr-imdb`

IMDb / TMDB charts and metadata detail source (`MediaSource`) paired with `media.local_match.v1` verification and fail-closed, dry-run-gated Radarr / Sonarr management.

## Configuration

All endpoints and credentials are explicitly user-configured; no hardcoded IPs or secrets exist:

- `imdb_api_base` / `tmdb_api_base`: User-configured `http`/`https` metadata API base URL.
- `imdb_provider`: `"auto"` | `"imdb"` | `"tmdb"`.
- `imdb_api_key` / `tmdb_api_key`: SecretVault keys (`0600` `vault.json`).
- `radarr_url` / `radarr_api_key`: User-configured Radarr base URL and SecretVault API key.
- `sonarr_url` / `sonarr_api_key`: User-configured Sonarr base URL and SecretVault API key.
- `quality_profile`, `root_folder_path`: Optional defaults for Radarr/Sonarr additions.
- `dry_run`: Defaults to `true`. `radarr_add` and `sonarr_add` only execute HTTP `POST` when `confirm=true` is explicitly supplied (and `dry_run` is not `true`).

## Capabilities & Actions

- Registers `MediaSource` (`source_id="arr-imdb"`) in `DomainRegistry` (`domain="media"`).
- Registers `WorkspaceTabSpec` (`tab_id="arr-imdb"`) in `domain="media"`.
- Registers `media.source.v1` capability (with graceful fallback on registry conflict) supporting actions:
  - `categories`, `fetch_items`, `fetch_detail`
  - `local_status` (consumes `media.local_match.v1`; returns `capability_unavailable` when absent)
  - `radarr_lookup`, `radarr_add`, `radarr_queue`
  - `sonarr_lookup`, `sonarr_add`, `sonarr_queue`

## Security

- Enforces `http`/`https` URLs only, rejects path-traversal (`..`, `/`, `\`, encoded `%2e`/`%2f`) in IDs and remote paths.
- Uses `ProxyHandler({})` (`trust_env=False`) and `_NoRedirectHandler` (`follow_redirects=False`) with finite timeouts.
- Scrubs all SecretVault secrets and query/header credentials from error messages and UI schemas.
