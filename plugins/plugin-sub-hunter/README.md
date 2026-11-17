# plugin-sub-hunter

OmniDesk v2 media provider plugin (`subtitle.fetch.v1`, `subtitle.sync.v1`).

## Features
- **Search (`subtitle.fetch.v1`)**: SubHD, ASSRT (token via 0600 `SecretVault`), and user-configured `custom` HTTPS provider with title / IMDb ID / season / episode / language query derivation.
- **Secure Download**: HTTPS-only enforcement, cross-origin credential stripping, 16 MB bounded downloads & archive extraction, zip-bomb / path-traversal / symlink entry rejection, UTF-8 / UTF-16 / GB18030 / Big5 decoding to UTF-8, and atomic writes.
- **Synchronization (`subtitle.sync.v1`)**: Linear `offset_ms` / `scale` correction for `.srt`/`.vtt`/`.ass`/`.ssa`, plus `ffsubsync` VAD alignment via arg-list `ctx.exec_subprocess` (`shell=False`) with manual subtitle overwrite protection (`overwrite_policy="protect_manual"`).
