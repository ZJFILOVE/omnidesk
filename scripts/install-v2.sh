#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DST_DIR="${OMNIDESK_INSTALL_DIR:-$HOME/.local/share/omnidesk/v2}"

cd "$REPO_ROOT/ui"
npm run build

cd "$REPO_ROOT/shell-tauri"
cargo build --release --features custom-protocol

mkdir -p "$DST_DIR"
SRC="$REPO_ROOT/shell-tauri/target/release/omnidesk"
DST="$DST_DIR/omnidesk"
BACKUP="$DST_DIR/omnidesk.previous"

if [[ -f "$DST" ]]; then
  cp -a "$DST" "$BACKUP"
fi

TMP="$DST_DIR/omnidesk.new.$$"
install -m 0755 "$SRC" "$TMP"
mv -f "$TMP" "$DST"

SHA="$(sha256sum "$DST" | awk '{print $1}')"
COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
INSTALLED_AT="$(date +%Y%m%d-%H%M%S)"

cat > "$DST_DIR/build-info.json" <<EOF
{
  "commit": "$COMMIT",
  "sha256": "$SHA",
  "installed_at": "$INSTALLED_AT",
  "backup": "$BACKUP"
}
EOF

echo "Installed $DST (commit=$COMMIT, sha256=$SHA)"
