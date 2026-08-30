#!/bin/bash
# Remove the pilot app and managed runtimes while retaining reinstall data.

set -euo pipefail

TARGET_APP="/Applications/Unified Inference.app"
SUPPORT_ROOT="$HOME/Library/Application Support/Mnemosyne"
ENV_PATH="$SUPPORT_ROOT/.env"
RUNTIME_ROOT="$SUPPORT_ROOT/runtimes"
BUNDLE_ID="com.mnemosyne.inference.menu"
AGENT_LABEL="com.mnemosyne.inference.agent"

pause_before_exit() {
    if [[ -t 0 ]]; then
        printf '\nPress Return to close this window. '
        read -r _answer
    fi
}

fail() {
    printf 'Uninstall stopped: %s\n' "$1" >&2
    pause_before_exit
    exit 1
}

bundle_value() {
    /usr/libexec/PlistBuddy -c "Print :$2" "$1/Contents/Info.plist" 2>/dev/null
}

[[ -d "$TARGET_APP" && ! -L "$TARGET_APP" ]] \
    || fail "Unified Inference is not installed at the expected Applications path."
[[ "$(bundle_value "$TARGET_APP" CFBundleIdentifier || true)" == "$BUNDLE_ID" ]] \
    || fail "The Applications target belongs to another product."

if /bin/launchctl print "gui/$UID/$AGENT_LABEL" >/dev/null 2>&1; then
    fail "Open Unified Inference, choose Disable Service, turn off Open Unified Inference at login, then Quit. This prevents a registered job from surviving removal."
fi
if /usr/bin/pgrep -x UnifiedInference >/dev/null 2>&1; then
    fail "Quit Unified Inference from its menu-bar menu, then run this assistant again."
fi

if [[ -e "$ENV_PATH" || -L "$ENV_PATH" ]]; then
    [[ -f "$ENV_PATH" && ! -L "$ENV_PATH" ]] \
        || fail "The private .env is not a regular file; nothing was removed."
fi
if [[ -e "$RUNTIME_ROOT" || -L "$RUNTIME_ROOT" ]]; then
    [[ -d "$RUNTIME_ROOT" && ! -L "$RUNTIME_ROOT" ]] \
        || fail "The managed-runtime target is not a regular directory; nothing was removed."
fi

printf 'This recoverable uninstall will move to Trash:\n'
printf '  %s\n' "$TARGET_APP"
if [[ -d "$RUNTIME_ROOT" ]]; then
    printf '  %s\n' "$RUNTIME_ROOT"
fi
printf '\nIt will retain:\n'
printf '  %s\n' "$ENV_PATH"
printf '  %s/config.yaml\n' "$SUPPORT_ROOT"
printf '  %s/state (token ledger, outbox, identity, and receipts)\n' "$SUPPORT_ROOT"
printf '  %s/models and every configured external model location\n' "$SUPPORT_ROOT"
printf '  %s/state/security-scopes and Nyx pairing data\n' "$SUPPORT_ROOT"
printf '\nExternally owned engines such as oMLX are not modified.\n'
printf 'Type UNINSTALL to continue: '
read -r CONFIRMATION
[[ "$CONFIRMATION" == "UNINSTALL" ]] || fail "Confirmation did not match; nothing was removed."

STAMP="$(date +%Y%m%d-%H%M%S)"
APP_TRASH="$HOME/.Trash/Unified Inference uninstalled-$STAMP.app"
[[ ! -e "$APP_TRASH" && ! -L "$APP_TRASH" ]] \
    || fail "The chosen app item already exists in Trash."

RUNTIME_TRASH=""
if [[ -d "$RUNTIME_ROOT" ]]; then
    RUNTIME_TRASH="$HOME/.Trash/Unified Inference managed runtimes-$STAMP"
    [[ ! -e "$RUNTIME_TRASH" && ! -L "$RUNTIME_TRASH" ]] \
        || fail "The chosen runtime item already exists in Trash."
    /bin/mv "$RUNTIME_ROOT" "$RUNTIME_TRASH" \
        || fail "Could not move managed runtimes to Trash; the app was not removed."
fi

if ! /bin/mv "$TARGET_APP" "$APP_TRASH"; then
    if [[ -n "$RUNTIME_TRASH" && -d "$RUNTIME_TRASH" && ! -e "$RUNTIME_ROOT" ]]; then
        /bin/mv "$RUNTIME_TRASH" "$RUNTIME_ROOT" || true
    fi
    fail "Could not move the app to Trash. Managed runtimes were restored when possible."
fi

printf '\nRecoverable uninstall complete.\n'
printf 'Preserved private environment: %s\n' "$ENV_PATH"
printf 'Preserved token accounting state: %s/state\n' "$SUPPORT_ROOT"
printf 'Reinstall with Install or Upgrade Unified Inference.command to resume the same identity and outbox.\n'
printf 'App in Trash: %s\n' "$APP_TRASH"
if [[ -n "$RUNTIME_TRASH" ]]; then
    printf 'Managed runtimes in Trash: %s\n' "$RUNTIME_TRASH"
fi
pause_before_exit
