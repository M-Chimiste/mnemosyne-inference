#!/usr/bin/env bash
# Retire the previous token sidecar and hand port 1240 to Unified Inference.

set -euo pipefail

LEGACY_LABEL="com.athena.token-sidecar"
UNIFIED_LABEL="com.mnemosyne.inference.agent"
LEGACY_PLIST="${LEGACY_SIDECAR_PLIST:-$HOME/Library/LaunchAgents/$LEGACY_LABEL.plist}"
APP_PATH="/Applications/Unified Inference.app"
USER_ID="$(id -u)"
DOMAIN="gui/$USER_ID"
LEGACY_TARGET="$DOMAIN/$LEGACY_LABEL"
UNIFIED_TARGET="$DOMAIN/$UNIFIED_LABEL"

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

if [[ ! -d "$APP_PATH" ]]; then
    fail "Install Unified Inference.app in /Applications before running this script."
fi

if [[ -L "$LEGACY_PLIST" ]]; then
    fail "Refusing to use a symlinked legacy LaunchAgent: $LEGACY_PLIST"
fi

if [[ -f "$LEGACY_PLIST" ]]; then
    PLIST_OWNER="$(stat -f '%u' "$LEGACY_PLIST")"
    [[ "$PLIST_OWNER" == "$USER_ID" ]] || fail \
        "Legacy LaunchAgent is not owned by the current user: $LEGACY_PLIST"
    PLIST_LABEL="$(/usr/libexec/PlistBuddy -c 'Print :Label' "$LEGACY_PLIST" 2>/dev/null || true)"
    [[ "$PLIST_LABEL" == "$LEGACY_LABEL" ]] || fail \
        "Expected LaunchAgent label $LEGACY_LABEL, found ${PLIST_LABEL:-none}."
    printf 'Validated legacy LaunchAgent: %s\n' "$LEGACY_PLIST"
else
    printf 'Legacy plist is absent; checking the exact launchd label anyway.\n'
fi

printf 'Permanently disabling %s...\n' "$LEGACY_LABEL"
launchctl disable "$LEGACY_TARGET"

if launchctl print "$LEGACY_TARGET" >/dev/null 2>&1; then
    printf 'Stopping the running legacy sidecar...\n'
    launchctl bootout "$LEGACY_TARGET"
else
    printf 'Legacy sidecar is not currently loaded.\n'
fi

if launchctl print "$LEGACY_TARGET" >/dev/null 2>&1; then
    fail "The legacy sidecar is still loaded; no other process was touched."
fi

printf 'Restarting Unified Inference...\n'
if ! launchctl kickstart -k "$UNIFIED_TARGET"; then
    fail "Unified Inference is not registered. Open the app and enable Background service, then rerun this script."
fi

CONTROL_READY=0
INFERENCE_READY=0
for _attempt in {1..30}; do
    if curl --silent --show-error --output /dev/null --max-time 1 \
        http://127.0.0.1:17321/manager/status; then
        CONTROL_READY=1
    fi
    if curl --silent --show-error --output /dev/null --max-time 1 \
        http://127.0.0.1:1240/health; then
        INFERENCE_READY=1
    fi
    if [[ "$CONTROL_READY" -eq 1 && "$INFERENCE_READY" -eq 1 ]]; then
        break
    fi
    sleep 1
done

if [[ "$CONTROL_READY" -ne 1 || "$INFERENCE_READY" -ne 1 ]]; then
    printf '\nThe legacy sidecar is disabled, but Unified Inference did not become ready.\n' >&2
    printf 'The migration plist was retained at: %s\n' "$LEGACY_PLIST" >&2
    printf 'Open Unified Inference → Open Logs, then inspect service.log.\n' >&2
    exit 1
fi

if [[ -f "$LEGACY_PLIST" ]]; then
    RETIRED_PLIST="${LEGACY_PLIST}.retired-$(date '+%Y%m%d-%H%M%S')"
    mv "$LEGACY_PLIST" "$RETIRED_PLIST"
    printf 'Archived the inactive legacy plist at:\n  %s\n' "$RETIRED_PLIST"
fi

printf '\nUnified Inference is ready:\n'
printf '  Control:   http://127.0.0.1:17321\n'
printf '  Inference: http://127.0.0.1:1240\n'
printf 'The previous token sidecar is disabled and cannot return at login.\n'
