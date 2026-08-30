#!/usr/bin/env bash
# Build and verify a distributable Unified Inference disk image.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
APP_PATH="$REPO_ROOT/macos/app/build/Stage/Unified Inference.app"
OUTPUT_PATH=""
VOLUME_NAME="Unified Inference"
CODESIGN_IDENTITY="${CODESIGN_IDENTITY:-}"
NOTARYTOOL_PROFILE="${NOTARYTOOL_PROFILE:-}"
NOTARYTOOL_NO_S3_ACCELERATION="${NOTARYTOOL_NO_S3_ACCELERATION:-0}"
PACKAGING_PYTHON="$(command -v python3)"
PACKAGING_UV="$(command -v uv)"
PACKAGING_TOOL_PATH="$(dirname "$PACKAGING_PYTHON"):$(dirname "$PACKAGING_UV"):/usr/bin:/bin:/usr/sbin:/sbin"

run_isolated_verify_release() {
    /usr/bin/env -i \
        "LC_ALL=C" \
        "PATH=$PACKAGING_TOOL_PATH" \
        "TMPDIR=/private/tmp" \
        "PYTHONDONTWRITEBYTECODE=1" \
        "PYTHONNOUSERSITE=1" \
        "PYTHONPATH=$SCRIPT_DIR" \
        "$PACKAGING_PYTHON" -B -s \
        "$SCRIPT_DIR/verify_release.py" "$@"
}

usage() {
    cat <<'EOF'
Usage: macos/packaging/build_dmg.sh [options]

Options:
  --app PATH          App bundle to package.
  --output PATH       Final DMG path.
  --volume-name NAME  Mounted volume name.
  --notary-profile P  Submit with a notarytool Keychain profile and staple.
  -h, --help          Show this help.

Set CODESIGN_IDENTITY to sign the DMG. The app itself must already carry a
valid signature; use macos/packaging/build_app.sh to stage it. Setting
NOTARYTOOL_PROFILE is equivalent to --notary-profile.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --app)
            shift
            [[ $# -gt 0 ]] || { echo "--app requires a path" >&2; exit 2; }
            APP_PATH="$1"
            ;;
        --output)
            shift
            [[ $# -gt 0 ]] || { echo "--output requires a path" >&2; exit 2; }
            OUTPUT_PATH="$1"
            ;;
        --volume-name)
            shift
            [[ $# -gt 0 ]] || { echo "--volume-name requires a value" >&2; exit 2; }
            VOLUME_NAME="$1"
            ;;
        --notary-profile)
            shift
            [[ $# -gt 0 ]] || { echo "--notary-profile requires a value" >&2; exit 2; }
            NOTARYTOOL_PROFILE="$1"
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [[ ! -d "$APP_PATH" ]]; then
    echo "App bundle not found: $APP_PATH" >&2
    echo "Run macos/packaging/build_app.sh release first." >&2
    exit 1
fi

INFO_PLIST="$APP_PATH/Contents/Info.plist"
APP_EXECUTABLE="$APP_PATH/Contents/MacOS/UnifiedInference"
if [[ ! -f "$INFO_PLIST" || ! -x "$APP_EXECUTABLE" ]]; then
    echo "App bundle is missing its Info.plist or executable: $APP_PATH" >&2
    exit 1
fi

VERSION="$(/usr/libexec/PlistBuddy \
    -c 'Print :CFBundleShortVersionString' \
    "$INFO_PLIST")"
if [[ ! "$VERSION" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "App version cannot be used in an artifact name: $VERSION" >&2
    exit 1
fi

ARCHS="$(lipo -archs "$APP_EXECUTABLE")"
if [[ "$ARCHS" == *"arm64"* && "$ARCHS" == *"x86_64"* ]]; then
    ARCH_LABEL="universal"
elif [[ "$ARCHS" == *"arm64"* ]]; then
    ARCH_LABEL="arm64"
elif [[ "$ARCHS" == *"x86_64"* ]]; then
    ARCH_LABEL="x86_64"
else
    echo "Unsupported application architecture: $ARCHS" >&2
    exit 1
fi

if [[ -z "$OUTPUT_PATH" ]]; then
    OUTPUT_PATH="$REPO_ROOT/macos/app/build/Distribution/Unified-Inference-${VERSION}-macos-${ARCH_LABEL}.dmg"
fi
OUTPUT_DIR="$(dirname "$OUTPUT_PATH")"
OUTPUT_NAME="$(basename "$OUTPUT_PATH")"
if [[ "$OUTPUT_NAME" != *.dmg ]]; then
    echo "Output path must end in .dmg: $OUTPUT_PATH" >&2
    exit 1
fi

codesign --verify --deep --strict --verbose=2 "$APP_PATH"
run_isolated_verify_release --app "$APP_PATH"
if [[ -n "$NOTARYTOOL_PROFILE" ]]; then
    if [[ -z "$CODESIGN_IDENTITY" || "$CODESIGN_IDENTITY" == "-" ]]; then
        echo "Notarization requires a Developer ID CODESIGN_IDENTITY." >&2
        exit 1
    fi
    APP_SIGNING_INFO="$(codesign -d --verbose=4 "$APP_PATH" 2>&1)"
    if [[ "$APP_SIGNING_INFO" != *"flags="*"runtime"* ]]; then
        echo "Notarization requires a hardened-runtime app signature." >&2
        echo "Rebuild the app with a non-ad-hoc CODESIGN_IDENTITY." >&2
        exit 1
    fi
    if [[ "$APP_SIGNING_INFO" != *"Timestamp="* ]]; then
        echo "Notarization requires a secure timestamp on the app." >&2
        echo "Rebuild the app with a non-ad-hoc CODESIGN_IDENTITY." >&2
        exit 1
    fi
    if [[ "$APP_SIGNING_INFO" != *"Authority=Developer ID Application:"* ]]; then
        echo "Notarization releases require a Developer ID Application signature." >&2
        exit 1
    fi
    if [[ "$APP_SIGNING_INFO" == *"TeamIdentifier=not set"* ]]; then
        echo "Notarization releases require an Apple Developer team identity." >&2
        exit 1
    fi
    SPARKLE_PUBLIC_ED_KEY="$(/usr/libexec/PlistBuddy \
        -c 'Print :SUPublicEDKey' \
        "$INFO_PLIST" 2>/dev/null || true)"
    SPARKLE_FEED_URL="$(/usr/libexec/PlistBuddy \
        -c 'Print :SUFeedURL' \
        "$INFO_PLIST" 2>/dev/null || true)"
    if [[ -z "$SPARKLE_PUBLIC_ED_KEY" ]]; then
        echo "Notarization releases require an embedded Sparkle public key." >&2
        exit 1
    fi
    if [[ "$SPARKLE_FEED_URL" != https://* ]]; then
        echo "Notarization releases require an HTTPS Sparkle feed." >&2
        exit 1
    fi
fi

NOTARYTOOL_ARGS=(
    --keychain-profile "$NOTARYTOOL_PROFILE"
    --wait
)
if [[ "$NOTARYTOOL_NO_S3_ACCELERATION" == "1" ]]; then
    NOTARYTOOL_ARGS+=(--no-s3-acceleration)
fi

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/unified-inference-dmg.XXXXXX")"
SOURCE_DIR="$WORK_DIR/source"
MOUNT_DIR="$WORK_DIR/mount"
TEMP_DMG="$WORK_DIR/$OUTPUT_NAME"
MOUNTED=0

cleanup() {
    if [[ "$MOUNTED" -eq 1 ]]; then
        hdiutil detach "$MOUNT_DIR" -quiet || true
    fi
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT

mkdir -p "$SOURCE_DIR" "$MOUNT_DIR"

if [[ -n "$NOTARYTOOL_PROFILE" ]]; then
    APP_ZIP="$WORK_DIR/Unified-Inference-app.zip"
    ditto -c -k --keepParent "$APP_PATH" "$APP_ZIP"
    xcrun notarytool submit \
        "$APP_ZIP" \
        "${NOTARYTOOL_ARGS[@]}"
    xcrun stapler staple "$APP_PATH"
    xcrun stapler validate "$APP_PATH"
    spctl \
        --assess \
        --type execute \
        --verbose=2 \
        "$APP_PATH"
    codesign --verify --deep --strict --verbose=2 "$APP_PATH"
fi

ditto "$APP_PATH" "$SOURCE_DIR/Unified Inference.app"
ln -s /Applications "$SOURCE_DIR/Applications"
install -m 755 \
    "$SCRIPT_DIR/pilot_install_or_upgrade.command" \
    "$SOURCE_DIR/Install or Upgrade Unified Inference.command"
install -m 755 \
    "$SCRIPT_DIR/pilot_uninstall_preserving_data.command" \
    "$SOURCE_DIR/Uninstall Unified Inference (Preserve Data).command"

hdiutil create \
    -fs HFS+ \
    -format UDZO \
    -imagekey zlib-level=9 \
    -volname "$VOLUME_NAME" \
    -srcfolder "$SOURCE_DIR" \
    "$TEMP_DMG"

if [[ -n "$CODESIGN_IDENTITY" && "$CODESIGN_IDENTITY" != "-" ]]; then
    codesign \
        --force \
        --timestamp \
        --sign "$CODESIGN_IDENTITY" \
        "$TEMP_DMG"
    codesign --verify --verbose=2 "$TEMP_DMG"
fi

if [[ -n "$NOTARYTOOL_PROFILE" ]]; then
    xcrun notarytool submit \
        "$TEMP_DMG" \
        "${NOTARYTOOL_ARGS[@]}"
    xcrun stapler staple "$TEMP_DMG"
    xcrun stapler validate "$TEMP_DMG"
    spctl \
        --assess \
        --type open \
        --context context:primary-signature \
        --verbose=2 \
        "$TEMP_DMG"
fi

hdiutil verify -quiet "$TEMP_DMG"
hdiutil attach \
    -quiet \
    -readonly \
    -nobrowse \
    -mountpoint "$MOUNT_DIR" \
    "$TEMP_DMG"
MOUNTED=1

MOUNTED_APP="$MOUNT_DIR/Unified Inference.app"
if [[ ! -d "$MOUNTED_APP" ]]; then
    echo "Verification failed: mounted DMG does not contain Unified Inference.app" >&2
    exit 1
fi
if [[ "$(readlink "$MOUNT_DIR/Applications")" != "/Applications" ]]; then
    echo "Verification failed: mounted DMG has no Applications shortcut" >&2
    exit 1
fi
MOUNTED_UPGRADE="$MOUNT_DIR/Install or Upgrade Unified Inference.command"
MOUNTED_UNINSTALL="$MOUNT_DIR/Uninstall Unified Inference (Preserve Data).command"
if [[ ! -x "$MOUNTED_UPGRADE" || ! -x "$MOUNTED_UNINSTALL" ]]; then
    echo "Verification failed: mounted DMG is missing its executable lifecycle assistants" >&2
    exit 1
fi
/bin/bash -n "$MOUNTED_UPGRADE"
/bin/bash -n "$MOUNTED_UNINSTALL"
codesign --verify --deep --strict --verbose=2 "$MOUNTED_APP"
run_isolated_verify_release --app "$MOUNTED_APP"
if [[ -n "$NOTARYTOOL_PROFILE" ]]; then
    xcrun stapler validate "$MOUNTED_APP"
    spctl \
        --assess \
        --type execute \
        --verbose=2 \
        "$MOUNTED_APP"
fi

hdiutil detach "$MOUNT_DIR" -quiet
MOUNTED=0

mkdir -p "$OUTPUT_DIR"
mv -f "$TEMP_DMG" "$OUTPUT_PATH"

ACCEPTANCE_REPORT="${OUTPUT_PATH%.dmg}-acceptance.json"
ACCEPTANCE_ARGS=(
    --app "$APP_PATH"
    --dmg "$OUTPUT_PATH"
    --output "$ACCEPTANCE_REPORT"
)
if [[ -n "$NOTARYTOOL_PROFILE" ]]; then
    ACCEPTANCE_ARGS+=(--require-distribution)
fi
python3 "$SCRIPT_DIR/collect_acceptance.py" "${ACCEPTANCE_ARGS[@]}"

echo "Built $OUTPUT_PATH"
echo "Acceptance evidence: $ACCEPTANCE_REPORT"
echo "Volume contents verified: app, Applications shortcut, and lifecycle assistants"
if [[ -n "$NOTARYTOOL_PROFILE" ]]; then
    echo "DMG notarized and stapled with profile: $NOTARYTOOL_PROFILE"
elif [[ -n "$CODESIGN_IDENTITY" && "$CODESIGN_IDENTITY" != "-" ]]; then
    echo "DMG signed with: $CODESIGN_IDENTITY"
    echo "Notarization was not requested."
else
    echo "DMG is unsigned. Set CODESIGN_IDENTITY for distribution signing."
    echo "Notarization was not requested."
fi
