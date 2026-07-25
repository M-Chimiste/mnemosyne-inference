#!/usr/bin/env bash
# Stage a signed Unified Inference.app for local development.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
APP_PACKAGE="$REPO_ROOT/macos/app"
CONFIG="release"
BARE=0
PYTHON_EXPORT="${MNEMOSYNE_PYTHON_EXPORT:-$SCRIPT_DIR/_export}"
SWIFTPM_DISABLE_SANDBOX="${MNEMOSYNE_SWIFTPM_DISABLE_SANDBOX:-0}"
CODESIGN_IDENTITY="${CODESIGN_IDENTITY:--}"

if [[ "${1:-}" == "debug" || "${1:-}" == "release" ]]; then
    CONFIG="$1"
    shift
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bare)
            BARE=1
            ;;
        --python-export)
            shift
            [[ $# -gt 0 ]] || { echo "--python-export requires a path" >&2; exit 2; }
            PYTHON_EXPORT="$1"
            ;;
        *)
            echo "unknown argument: $1" >&2
            exit 2
            ;;
    esac
    shift
done

if [[ "$SWIFTPM_DISABLE_SANDBOX" == "1" ]]; then
    swift build \
        --package-path "$APP_PACKAGE" \
        --configuration "$CONFIG" \
        --disable-sandbox
    BIN_DIR="$(swift build \
        --package-path "$APP_PACKAGE" \
        --configuration "$CONFIG" \
        --show-bin-path \
        --disable-sandbox)"
else
    swift build \
        --package-path "$APP_PACKAGE" \
        --configuration "$CONFIG"
    BIN_DIR="$(swift build \
        --package-path "$APP_PACKAGE" \
        --configuration "$CONFIG" \
        --show-bin-path)"
fi

STAGE_DIR="$APP_PACKAGE/build/Stage"
APP_DIR="$STAGE_DIR/Unified Inference.app"
LEGACY_APP_DIR="$STAGE_DIR/Mnemosyne.app"
CONTENTS="$APP_DIR/Contents"
RESOURCES="$CONTENTS/Resources"
SERVICE_BOOTSTRAP="$CONTENTS/MacOS/mnemosyne-service-bootstrap"

rm -rf "$APP_DIR" "$LEGACY_APP_DIR"
mkdir -p \
    "$CONTENTS/MacOS" \
    "$CONTENTS/Library/LaunchAgents" \
    "$RESOURCES/Service" \
    "$RESOURCES/ImageWorker"

install -m 644 "$SCRIPT_DIR/Info.plist" "$CONTENTS/Info.plist"
install -m 644 "$SCRIPT_DIR/AppIcon.icns" "$RESOURCES/AppIcon.icns"
install -m 755 "$BIN_DIR/MnemosyneMenu" "$CONTENTS/MacOS/UnifiedInference"
install -m 755 \
    "$BIN_DIR/mnemosyne-service-bootstrap" \
    "$SERVICE_BOOTSTRAP"
install -m 644 \
    "$SCRIPT_DIR/LaunchAgents/com.mnemosyne.inference.agent.plist" \
    "$CONTENTS/Library/LaunchAgents/com.mnemosyne.inference.agent.plist"

ditto "$REPO_ROOT/macos/service/src" "$RESOURCES/Service"
ditto "$REPO_ROOT/macos/image-worker/src" "$RESOURCES/ImageWorker"
install -m 644 \
    "$REPO_ROOT/macos/image-worker/capabilities.json" \
    "$RESOURCES/ImageWorker/capabilities.json"
install -m 644 "$REPO_ROOT/macos/config.yaml.example" "$RESOURCES/config.yaml.example"
install -m 644 "$REPO_ROOT/macos/.env.example" "$RESOURCES/.env.example"

if [[ "$BARE" -eq 0 ]]; then
    if [[ ! -d "$PYTHON_EXPORT" ]]; then
        echo "Python export not found: $PYTHON_EXPORT" >&2
        echo "Run: python3 macos/packaging/build_runtime.py" >&2
        echo "Or use --bare to stage only the menu UI." >&2
        exit 1
    fi
    ditto "$PYTHON_EXPORT" "$RESOURCES/Python"
fi

CODESIGN_ARGS=(--force --sign "$CODESIGN_IDENTITY")
if [[ "$CODESIGN_IDENTITY" != "-" ]]; then
    CODESIGN_ARGS=(
        --force
        --options runtime
        --timestamp
        --sign "$CODESIGN_IDENTITY"
    )
fi

sign_mach_o_tree() {
    local root="$1"
    [[ -d "$root" ]] || return 0
    while IFS= read -r -d '' candidate; do
        if file -b "$candidate" | grep -q "Mach-O"; then
            codesign "${CODESIGN_ARGS[@]}" "$candidate"
        fi
    done < <(
        find "$root" -type f \
            \( -perm -111 -o -name '*.so' -o -name '*.dylib' \) \
            -print0
    )
}

if [[ "$BARE" -eq 0 ]]; then
    sign_mach_o_tree "$RESOURCES/Python"
fi
codesign \
    "${CODESIGN_ARGS[@]}" \
    --identifier com.mnemosyne.inference.service \
    "$SERVICE_BOOTSTRAP"
codesign "${CODESIGN_ARGS[@]}" "$CONTENTS/MacOS/UnifiedInference"
codesign "${CODESIGN_ARGS[@]}" "$APP_DIR"

plutil -lint \
    "$CONTENTS/Info.plist" \
    "$CONTENTS/Library/LaunchAgents/com.mnemosyne.inference.agent.plist"
codesign --verify --deep --strict "$APP_DIR"

echo "Staged $APP_DIR"
if [[ "$CODESIGN_IDENTITY" == "-" ]]; then
    echo "Ad-hoc signature: protected-folder permission may need to be selected again after rebuilding."
else
    echo "Hardened and timestamped with stable identity: $CODESIGN_IDENTITY"
fi
if [[ "$BARE" -eq 1 ]]; then
    echo "Bare build: background service registration will fail until Python is bundled."
fi
