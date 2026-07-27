#!/usr/bin/env bash
# Enable the configured oMLX adapter without changing any model profiles.

set -euo pipefail

CONFIG_PATH="${MNEMOSYNE_CONFIG_PATH:-$HOME/Library/Application Support/Mnemosyne/config.yaml}"
LAUNCHCTL_BIN="${MNEMOSYNE_LAUNCHCTL_BIN:-/bin/launchctl}"
CURL_BIN="${MNEMOSYNE_CURL_BIN:-/usr/bin/curl}"
UNIFIED_LABEL="com.mnemosyne.inference.agent"
USER_ID="$(id -u)"

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

[[ -f "$CONFIG_PATH" ]] || fail "Config not found: $CONFIG_PATH"
[[ ! -L "$CONFIG_PATH" ]] || fail "Refusing to edit a symlinked config: $CONFIG_PATH"
[[ "$(stat -f '%u' "$CONFIG_PATH")" == "$USER_ID" ]] || fail \
    "Config is not owned by the current user: $CONFIG_PATH"

BACKUP_PATH="${CONFIG_PATH}.backup-before-omlx-$(date '+%Y%m%d-%H%M%S')"
TEMP_PATH="$(mktemp "${CONFIG_PATH}.omlx.XXXXXX")"
cleanup() {
    rm -f "$TEMP_PATH"
}
trap cleanup EXIT

cp -p "$CONFIG_PATH" "$BACKUP_PATH"

awk '
    BEGIN {
        in_omlx = 0
        saw_omlx = 0
        enabled_omlx = 0
    }
    /^  omlx:[[:space:]]*$/ {
        in_omlx = 1
        saw_omlx = 1
        print
        next
    }
    in_omlx && /^    enabled:[[:space:]]*false[[:space:]]*$/ {
        print "    enabled: true"
        enabled_omlx = 1
        in_omlx = 0
        next
    }
    in_omlx && /^    enabled:[[:space:]]*true[[:space:]]*$/ {
        enabled_omlx = 1
        in_omlx = 0
        print
        next
    }
    in_omlx && /^  [a-zA-Z0-9_-]+:[[:space:]]*$/ {
        in_omlx = 0
    }
    { print }
    END {
        if (!saw_omlx || !enabled_omlx) {
            exit 42
        }
    }
' "$CONFIG_PATH" > "$TEMP_PATH" || fail \
    "Could not find a normal engines.omlx.enabled setting. Backup: $BACKUP_PATH"

chmod 600 "$TEMP_PATH"
mv "$TEMP_PATH" "$CONFIG_PATH"
trap - EXIT

printf 'Enabled oMLX without disabling glm-5-2.\n'
printf 'Backup: %s\n' "$BACKUP_PATH"
printf 'Restarting Unified Inference...\n'
"$LAUNCHCTL_BIN" kickstart -k "gui/$USER_ID/$UNIFIED_LABEL"

for _attempt in {1..30}; do
    if "$CURL_BIN" --silent --show-error --output /dev/null --max-time 1 \
        http://127.0.0.1:17321/manager/status; then
        printf 'Unified Inference control service is reachable.\n'
        printf 'Open Settings → Engines to inspect oMLX status.\n'
        exit 0
    fi
    sleep 1
done

fail "The config was repaired, but the control service did not start. Open Logs and inspect service.log."
