#!/usr/bin/env bash
# Stage a signed Unified Inference.app for local development.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
APP_PACKAGE="$REPO_ROOT/macos/app"
VERSION_FILE="$REPO_ROOT/macos/VERSION"
CONFIG="release"
BARE=0
PYTHON_EXPORT="${MNEMOSYNE_PYTHON_EXPORT:-$SCRIPT_DIR/_export}"
SWIFTPM_DISABLE_SANDBOX="${MNEMOSYNE_SWIFTPM_DISABLE_SANDBOX:-0}"
CODESIGN_IDENTITY="${CODESIGN_IDENTITY:--}"
SPARKLE_PUBLIC_ED_KEY="${SPARKLE_PUBLIC_ED_KEY:-}"
SPARKLE_FEED_URL="${SPARKLE_FEED_URL:-https://github.com/M-Chimiste/mnemosyne-inference/releases/latest/download/appcast.xml}"
APP_VERSION="$(tr -d '[:space:]' < "$VERSION_FILE")"
BUILD_NUMBER="${MNEMOSYNE_BUILD_NUMBER:-}"

if [[ ! "$APP_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Invalid app version in $VERSION_FILE: $APP_VERSION" >&2
    exit 1
fi
if [[ -z "$BUILD_NUMBER" ]]; then
    BUILD_NUMBER="$(git -C "$REPO_ROOT" rev-list --count HEAD 2>/dev/null || true)"
    BUILD_NUMBER="${BUILD_NUMBER:-1}"
fi
if [[ ! "$BUILD_NUMBER" =~ ^[1-9][0-9]*$ ]]; then
    echo "MNEMOSYNE_BUILD_NUMBER must be a positive integer: $BUILD_NUMBER" >&2
    exit 1
fi
if [[ "$CODESIGN_IDENTITY" == *"Developer ID Application"* \
      && -z "$SPARKLE_PUBLIC_ED_KEY" ]]; then
    echo "Developer ID builds require SPARKLE_PUBLIC_ED_KEY." >&2
    exit 1
fi
if [[ "$SPARKLE_FEED_URL" != https://* ]]; then
    echo "SPARKLE_FEED_URL must use HTTPS: $SPARKLE_FEED_URL" >&2
    exit 1
fi

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
FRAMEWORKS="$CONTENTS/Frameworks"
SERVICE_BOOTSTRAP="$CONTENTS/MacOS/mnemosyne-service-bootstrap"
MENU_EXECUTABLE="$CONTENTS/MacOS/UnifiedInference"

rm -rf "$APP_DIR" "$LEGACY_APP_DIR"
mkdir -p \
    "$CONTENTS/MacOS" \
    "$CONTENTS/Library/LaunchAgents" \
    "$FRAMEWORKS" \
    "$RESOURCES/Service" \
    "$RESOURCES/ImageWorker"

install -m 644 "$SCRIPT_DIR/Info.plist" "$CONTENTS/Info.plist"
/usr/libexec/PlistBuddy \
    -c "Set :CFBundleShortVersionString $APP_VERSION" \
    -c "Set :CFBundleVersion $BUILD_NUMBER" \
    -c "Set :SUFeedURL $SPARKLE_FEED_URL" \
    "$CONTENTS/Info.plist"
if [[ -n "$SPARKLE_PUBLIC_ED_KEY" ]]; then
    /usr/libexec/PlistBuddy \
        -c "Add :SUPublicEDKey string $SPARKLE_PUBLIC_ED_KEY" \
        "$CONTENTS/Info.plist"
fi
install -m 644 "$SCRIPT_DIR/AppIcon.icns" "$RESOURCES/AppIcon.icns"
install -m 755 "$BIN_DIR/MnemosyneMenu" "$MENU_EXECUTABLE"
install -m 755 \
    "$BIN_DIR/mnemosyne-service-bootstrap" \
    "$SERVICE_BOOTSTRAP"
if [[ ! -d "$BIN_DIR/Sparkle.framework" ]]; then
    echo "Sparkle.framework was not produced beside the release executable." >&2
    exit 1
fi
ditto "$BIN_DIR/Sparkle.framework" "$FRAMEWORKS/Sparkle.framework"
if [[ "$(/usr/bin/otool -l "$MENU_EXECUTABLE")" != \
      *"path @executable_path/../Frameworks "* ]]; then
    /usr/bin/install_name_tool \
        -add_rpath "@executable_path/../Frameworks" \
        "$MENU_EXECUTABLE"
fi
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

# Source and runtime trees may contain local test bytecode. Never ship it or
# include it in the signed resource seal; the service bootstrap also disables
# runtime bytecode writes so ordinary launches cannot mutate the app bundle.
find "$APP_DIR" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
find "$APP_DIR" -depth -type d -name '__pycache__' -empty -delete

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
SPARKLE_FRAMEWORK="$FRAMEWORKS/Sparkle.framework"
SPARKLE_VERSION="$SPARKLE_FRAMEWORK/Versions/B"
codesign \
    "${CODESIGN_ARGS[@]}" \
    "$SPARKLE_VERSION/XPCServices/Installer.xpc"
codesign \
    "${CODESIGN_ARGS[@]}" \
    --preserve-metadata=entitlements \
    "$SPARKLE_VERSION/XPCServices/Downloader.xpc"
codesign "${CODESIGN_ARGS[@]}" "$SPARKLE_VERSION/Autoupdate"
codesign "${CODESIGN_ARGS[@]}" "$SPARKLE_VERSION/Updater.app"
codesign "${CODESIGN_ARGS[@]}" "$SPARKLE_FRAMEWORK"
codesign \
    "${CODESIGN_ARGS[@]}" \
    --identifier com.mnemosyne.inference.service \
    "$SERVICE_BOOTSTRAP"
codesign "${CODESIGN_ARGS[@]}" "$MENU_EXECUTABLE"
codesign "${CODESIGN_ARGS[@]}" "$APP_DIR"

plutil -lint \
    "$CONTENTS/Info.plist" \
    "$CONTENTS/Library/LaunchAgents/com.mnemosyne.inference.agent.plist"
codesign --verify --deep --strict "$APP_DIR"
python3 "$SCRIPT_DIR/verify_release.py" --app "$APP_DIR"

echo "Staged Unified Inference $APP_VERSION ($BUILD_NUMBER) at $APP_DIR"
if [[ "$CODESIGN_IDENTITY" == "-" ]]; then
    echo "Ad-hoc signature: protected-folder permission may need to be selected again after rebuilding."
else
    echo "Hardened and timestamped with stable identity: $CODESIGN_IDENTITY"
fi
if [[ "$BARE" -eq 1 ]]; then
    echo "Bare build: background service registration will fail until Python is bundled."
fi
