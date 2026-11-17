# OmniDesk AI Curator (`plugin-ai-curator`)

Pure Python Protocol v2 (`schema_version = 2`, `kind = "hybrid"`, `domain = "core"`) plugin providing `recommend.feed.v1` and registering `MediaSource`, `TextSource` (`category = "multi"`), and `BookSource`.

## Responsibilities

- **Dual-Repo Git Mirror & Safe Sync**: Discovers `obsidian-vault` and `music-daily` (`config -> workspace sibling -> <XDG_DATA_HOME>/omnidesk/curation/repos`), clones missing repos via arg-list `git clone --branch main`, checks `git status --porcelain`, and fast-forwards (`git fetch` + `git merge --ff-only`) only when clean. Dirty worktrees are preserved untouched (`dirty = true`).
- **Incremental Full-Archive Rehydration (since `2026-09-04`)**: Uses `<curation_root>/rehydrate_manifest.json` (`rel_path`, `size`, `mtime_ns`, `blob_sha`) to rehydrate Music Daily (`2026-11-06` 4-track anomaly reported explicitly in `history_anomalies`), YouTube Daily, News Daily, Reddit Daily (`original_post_verification`), Journal Daily, and English (`Daily`, `Translation`, `Vocabulary`, `Weekly`, `Weekly/TV`) with idempotent `vocab.sm2.v1` import.
- **Hard Daily vs. Interactive Firewall**:
  - `publish_daily_bundle` (`daily_immutable`): requires `qa_verified == true` and verified `publication_evidence` (`PUBLISHED` + matching `operation_id`/`business_date`/receipt); returns `needs_verified_bundle` otherwise.
  - `push_interactive_set` (`interactive_ephemeral`): requires `record_permanent_history == false`, writes strictly to `<curation_root>/interactive/sessions/`, and performs read-only provenance badges without touching permanent ledgers.
  - `lookup_provenance`: strictly read-only matching up to 4096 items (`read_only = true`).
