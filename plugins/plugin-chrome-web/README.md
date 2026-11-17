# plugin-chrome-web

OmniDesk v2 managed native Chrome `--app` bridge (`kind=hybrid`, `domain=text`).

## Capabilities Provided

- `web.embed.v1`: `open`, `close`, `status` — launches system Chrome/Chromium via `ctx.exec_subprocess` (`shell=False`) with `--user-data-dir=<XDG_DATA_HOME>/omnidesk/chrome-profile`, `--app=<url>`, `--class=omnidesk-reader`, and optional `--load-extension=resources/mv3-extension`. Rejects non-`http`/`https` schemes and returns `not_configured` if Chrome is missing.
- `web.cookies.v1`: `list`, `get` (`get_cookies`), `import` — reads a temporary read-only copy of Chromium's `Cookies` SQLite DB to avoid locking, filters by domain/subdomains/expiry, stores imported cookies only in `0600` `SecretVault` or `0600` files, and fails closed with `cookie_decryption_unavailable` when encrypted cookies cannot be decrypted.
- `web.extract.v1`: `extract` — connects strictly to `127.0.0.1` CDP (`Runtime.evaluate -> document.documentElement.outerHTML`), parses untrusted HTML safely into `title`/`url`/`html`/`text`/`links`/`paragraphs`, and optionally consumes `llm.translate.v1` (degrading cleanly with `text_only=true`). Never falls back to `urllib`.
- `web.fetch.v1`: `fetch` — runs same-origin `fetch()` inside the matching Chrome page context via CDP `Runtime.evaluate` (`awaitPromise=true`), enforcing `http`/`https`, same-origin, and `max_fetch_bytes`.

## Cookie Decryption Scope

- Supported: plaintext SQLite rows (`value` populated, `encrypted_value` empty), plus Linux `v10` (`peanuts`) only when the optional `cryptography` package is installed.
- Unsupported (fail closed with `cookie_decryption_unavailable`): Linux `v11` (GNOME Keyring/KWallet), Windows DPAPI, macOS Keychain, Chrome `v20` app-bound encryption, or `v10` without `cryptography`.
