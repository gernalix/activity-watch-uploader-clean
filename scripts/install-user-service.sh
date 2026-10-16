#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
EXPECTED="$HOME/projects/activity-watch-uploader"
UNIT_DIR="$HOME/.config/systemd/user"
ENV_DIR="$HOME/.config/activity-watch-uploader"
ENV_FILE="$ENV_DIR/env"
DATA_REPO="$HOME/projects/activity-watch-data"

if [[ "$ROOT" != "$EXPECTED" ]]; then
  printf 'Clone must live at %s (found %s).\n' "$EXPECTED" "$ROOT" >&2
  exit 1
fi

if [[ ! -d "$DATA_REPO/.git" ]]; then
  printf 'Data repository is missing: %s\n' "$DATA_REPO" >&2
  exit 1
fi

: "${AW_DATA_REPO_SLUG:?Configure the private data repository slug explicitly}"
DATA_ORIGIN=$(git -C "$DATA_REPO" config --get remote.origin.url)
case "$DATA_ORIGIN" in
  "https://github.com/$AW_DATA_REPO_SLUG"|"https://github.com/$AW_DATA_REPO_SLUG.git"|"git@github.com:$AW_DATA_REPO_SLUG"|"git@github.com:$AW_DATA_REPO_SLUG.git") ;;
  *) printf 'Data origin does not match the explicitly configured repository.\n' >&2; exit 1 ;;
esac

install -d -m 0700 "$ENV_DIR"
install -d -m 0755 "$UNIT_DIR"
install -m 0644 "$ROOT/systemd/activity-watch-uploader.service" "$UNIT_DIR/"
install -m 0644 "$ROOT/systemd/activity-watch-uploader.timer" "$UNIT_DIR/"

if [[ ! -f "$ENV_FILE" ]]; then
  cat > "$ENV_FILE" <<'ENV'
AW_BASE_URL=http://127.0.0.1:5600
AW_DATA_REPO_PATH=${HOME}/projects/activity-watch-data
AW_REFRESH_DAYS=2
AW_FULL_RECONCILE_HOURS=168
# Secrets belong in Secret Service/libsecret, not this file.
ENV
fi

if grep -q '^AW_DATA_REPO_PATH=' "$ENV_FILE"; then
  sed -i 's|^AW_DATA_REPO_PATH=.*$|AW_DATA_REPO_PATH=${HOME}/projects/activity-watch-data|' "$ENV_FILE"
else
  printf 'AW_DATA_REPO_PATH=${HOME}/projects/activity-watch-data\n' >> "$ENV_FILE"
fi
if ! grep -q '^AW_DATA_REPO_SLUG=' "$ENV_FILE"; then
  printf 'AW_DATA_REPO_SLUG=%s\n' "$AW_DATA_REPO_SLUG" >> "$ENV_FILE"
fi
chmod 0600 "$ENV_FILE"

systemctl --user daemon-reload
systemd-analyze --user verify "$UNIT_DIR/activity-watch-uploader.service" "$UNIT_DIR/activity-watch-uploader.timer"
systemctl --user enable --now activity-watch-uploader.timer
printf 'Installed and enabled activity-watch-uploader.timer\n'
