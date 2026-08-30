#!/bin/bash
# Install or replace the pilot app without touching private state or weights.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CANDIDATE_APP="$SCRIPT_DIR/Unified Inference.app"
TARGET_APP="/Applications/Unified Inference.app"
SUPPORT_ROOT="$HOME/Library/Application Support/Mnemosyne"
ENV_PATH="$SUPPORT_ROOT/.env"
BUNDLE_ID="com.mnemosyne.inference.menu"
AGENT_LABEL="com.mnemosyne.inference.agent"
STAGE_ROOT=""

pause_before_exit() {
    if [[ -t 0 ]]; then
        printf '\nPress Return to close this window. '
        read -r _answer
    fi
}

fail() {
    printf 'Install or upgrade stopped: %s\n' "$1" >&2
    pause_before_exit
    exit 1
}

cleanup() {
    if [[ -n "$STAGE_ROOT" && -d "$STAGE_ROOT" ]]; then
        rm -rf -- "$STAGE_ROOT"
    fi
}
trap cleanup EXIT

bundle_value() {
    /usr/libexec/PlistBuddy -c "Print :$2" "$1/Contents/Info.plist" 2>/dev/null
}

environment_digest() {
    if [[ -f "$ENV_PATH" && ! -L "$ENV_PATH" ]]; then
        /usr/bin/shasum -a 256 "$ENV_PATH" | /usr/bin/awk '{print $1}'
    elif [[ -e "$ENV_PATH" || -L "$ENV_PATH" ]]; then
        printf 'unsafe\n'
    else
        printf 'absent\n'
    fi
}

agent_pid() {
    /bin/launchctl print "gui/$UID/$AGENT_LABEL" 2>/dev/null \
        | /usr/bin/awk '$1 == "pid" && $2 == "=" { print $3; exit }'
}

agent_registered() {
    /bin/launchctl print "gui/$UID/$AGENT_LABEL" >/dev/null 2>&1
}

[[ -d "$CANDIDATE_APP" && ! -L "$CANDIDATE_APP" ]] \
    || fail "The candidate app is missing beside this assistant. Run it from the mounted Unified Inference disk image."

CANDIDATE_ID="$(bundle_value "$CANDIDATE_APP" CFBundleIdentifier || true)"
[[ "$CANDIDATE_ID" == "$BUNDLE_ID" ]] \
    || fail "The candidate has the wrong application identity."
/usr/bin/codesign --verify --deep --strict "$CANDIDATE_APP" 2>/dev/null \
    || fail "The candidate app failed its code-signature verification."

if /usr/bin/pgrep -x UnifiedInference >/dev/null 2>&1; then
    fail "Quit Unified Inference from its menu-bar menu, then run this assistant again. The inference service may remain enabled."
fi

ENV_BEFORE="$(environment_digest)"
[[ "$ENV_BEFORE" != "unsafe" ]] \
    || fail "The private .env is not a regular file. Resolve that before upgrading."
OLD_AGENT_PID="$(agent_pid || true)"
AGENT_WAS_REGISTERED=0
if agent_registered; then
    AGENT_WAS_REGISTERED=1
fi

CANDIDATE_BUILD="$(bundle_value "$CANDIDATE_APP" CFBundleVersion || true)"
[[ "$CANDIDATE_BUILD" =~ ^[0-9]+$ ]] \
    || fail "The candidate build number is invalid."

if [[ -e "$TARGET_APP" || -L "$TARGET_APP" ]]; then
    [[ -d "$TARGET_APP" && ! -L "$TARGET_APP" ]] \
        || fail "The Applications target is not a regular app bundle."
    TARGET_ID="$(bundle_value "$TARGET_APP" CFBundleIdentifier || true)"
    [[ "$TARGET_ID" == "$BUNDLE_ID" ]] \
        || fail "The existing Applications target belongs to another product."
    INSTALLED_BUILD="$(bundle_value "$TARGET_APP" CFBundleVersion || true)"
    [[ "$INSTALLED_BUILD" =~ ^[0-9]+$ ]] \
        || fail "The installed build number is invalid."
    (( CANDIDATE_BUILD >= INSTALLED_BUILD )) \
        || fail "This would downgrade build $INSTALLED_BUILD to $CANDIDATE_BUILD. Use a newer pilot."
fi

STAGE_ROOT="$(/usr/bin/mktemp -d "/Applications/.unified-inference-upgrade.XXXXXX")" \
    || fail "Could not create a private staging folder in Applications."
STAGED_APP="$STAGE_ROOT/Unified Inference.app"
/usr/bin/ditto "$CANDIDATE_APP" "$STAGED_APP"
/usr/bin/codesign --verify --deep --strict "$STAGED_APP" 2>/dev/null \
    || fail "The staged app failed verification."

BACKUP_APP=""
if [[ -d "$TARGET_APP" ]]; then
    BACKUP_APP="$HOME/.Trash/Unified Inference previous build $(bundle_value "$TARGET_APP" CFBundleVersion)-$(date +%Y%m%d-%H%M%S).app"
    [[ ! -e "$BACKUP_APP" && ! -L "$BACKUP_APP" ]] \
        || fail "The chosen rollback item already exists in Trash."
    /bin/mv "$TARGET_APP" "$BACKUP_APP" \
        || fail "Could not move the previous app to Trash."
fi

if ! /bin/mv "$STAGED_APP" "$TARGET_APP"; then
    if [[ -n "$BACKUP_APP" && -d "$BACKUP_APP" && ! -e "$TARGET_APP" ]]; then
        /bin/mv "$BACKUP_APP" "$TARGET_APP" || true
    fi
    fail "Could not activate the candidate app. The prior app was restored when possible."
fi

ENV_AFTER="$(environment_digest)"
[[ "$ENV_AFTER" == "$ENV_BEFORE" ]] \
    || fail "The private .env changed unexpectedly. The app was installed, but token-reporting configuration needs inspection before use."

/usr/bin/open "$TARGET_APP"

if [[ "$AGENT_WAS_REGISTERED" -eq 1 ]]; then
    NEW_AGENT_PID=""
    for _attempt in {1..60}; do
        NEW_AGENT_PID="$(agent_pid || true)"
        if [[ -n "$NEW_AGENT_PID" ]] \
            && [[ -z "$OLD_AGENT_PID" || "$NEW_AGENT_PID" != "$OLD_AGENT_PID" ]] \
            && /usr/bin/curl --fail --silent --max-time 2 \
                "http://127.0.0.1:17321/health" >/dev/null 2>&1; then
            break
        fi
        /bin/sleep 1
    done
    [[ -n "$NEW_AGENT_PID" ]] \
        && [[ -z "$OLD_AGENT_PID" || "$NEW_AGENT_PID" != "$OLD_AGENT_PID" ]] \
        || fail "The app was upgraded and .env was preserved, but the registered service did not restart. Open Unified Inference and use Restart Service; macOS may require Login Items approval."
fi

printf '\nUnified Inference build %s is installed.\n' "$CANDIDATE_BUILD"
printf 'Preserved: %s and all other private state/model locations.\n' "$ENV_PATH"
if [[ -n "$BACKUP_APP" ]]; then
    printf 'Rollback copy: %s\n' "$BACKUP_APP"
fi
printf 'The app refreshes the exact background-service registration on launch.\n'
pause_before_exit
