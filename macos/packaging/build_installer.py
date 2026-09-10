"""Package a user-launched, unprivileged fresh-bundle installer for a signed app.

The complete payload and byte inventory are sealed inside the installer so app
translocation cannot make a sibling payload disappear or substitute a candidate.
This does not enable the service's gated migration/uninstall executor.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
from pathlib import Path

try:
    from .swift_rpaths import normalize
except ImportError:  # Direct packaging invocation.
    from swift_rpaths import normalize

ROOT = Path(__file__).resolve().parent
INSTALLER_NAME = "Install Unified Inference.app"
APP_NAME = "Unified Inference.app"


def inventory(root: Path) -> dict[str, dict]:
    result = {}
    resolved_root = root.resolve(strict=True)
    for folder, directories, files in os.walk(root, followlinks=False):
        for name in sorted(directories + files):
            path = Path(folder) / name
            info = path.lstat()
            mode = stat.S_IMODE(info.st_mode)
            if path.is_symlink():
                value = os.readlink(path)
                if os.path.isabs(value) or not path.resolve(strict=True).is_relative_to(resolved_root):
                    raise ValueError(f"External payload link: {path}")
                kind = "link"
            elif stat.S_ISDIR(info.st_mode):
                kind, value = "directory", ""
            elif stat.S_ISREG(info.st_mode):
                with path.open("rb") as stream:
                    digest = hashlib.sha256()
                    for block in iter(lambda: stream.read(1_048_576), b""):
                        digest.update(block)
                    value = digest.hexdigest()
                kind = "file"
            else:
                raise ValueError(f"Unsupported payload member: {path}")
            result[path.relative_to(root).as_posix()] = {"kind": kind, "value": value, "mode": mode}
    return result


def build(app: Path, output: Path, identity: str) -> Path:
    if output.exists():
        raise ValueError("Installer output must be a new directory")
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    signing = subprocess.run(["/usr/bin/codesign", "-d", "--verbose=4", str(app)], check=True, capture_output=True, text=True).stderr
    match = re.search(r"^TeamIdentifier=([A-Z0-9]{10})$", signing, re.MULTILINE)
    if not match or "Authority=Developer ID Application:" not in signing:
        raise ValueError("The install assistant requires a Developer ID signed payload")
    team = match.group(1)
    app_info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if app_info["CFBundleIdentifier"] != "com.mnemosyne.inference.menu":
        raise ValueError("Unexpected application identity")
    package = ROOT / "installer"
    subprocess.run(["/usr/bin/swift", "build", "--package-path", str(package), "--configuration", "release"], check=True)
    bin_dir = subprocess.run(["/usr/bin/swift", "build", "--package-path", str(package), "--configuration", "release", "--show-bin-path"], check=True, capture_output=True, text=True).stdout.strip()
    executable = output / "Contents/MacOS/InstallUnifiedInference"
    resources = output / "Contents/Resources"
    executable.parent.mkdir(parents=True)
    resources.mkdir()
    shutil.copy2(Path(bin_dir) / "InstallUnifiedInference", executable)
    normalize(executable)
    shutil.copy2(ROOT / "AppIcon.icns", resources / "AppIcon.icns")
    payload = resources / APP_NAME
    subprocess.run(["/usr/bin/ditto", str(app), str(payload)], check=True)
    manifest = {
        "format": 1,
        "version": app_info["CFBundleShortVersionString"],
        "build": app_info["CFBundleVersion"],
        "team": team,
        "entries": inventory(payload),
    }
    (resources / "Release.json").write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
    info = {
        "CFBundleIdentifier": "com.mnemosyne.inference.installer",
        "CFBundleExecutable": "InstallUnifiedInference",
        "CFBundleName": "Install Unified Inference",
        "CFBundleDisplayName": "Install Unified Inference",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": manifest["version"],
        "CFBundleVersion": manifest["build"],
        "CFBundleIconFile": "AppIcon",
        "LSMinimumSystemVersion": "15.0",
        "NSHighResolutionCapable": True,
    }
    (output / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    # No --deep signing: the embedded product's notarized signature stays intact.
    subprocess.run(["/usr/bin/codesign", "--force", "--options", "runtime", "--timestamp", "--sign", identity, str(output)], check=True)
    requirement = f'anchor apple generic and identifier "com.mnemosyne.inference.installer" and certificate leaf[subject.OU] = "{team}"'
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", "-R", "=" + requirement, str(output)], check=True)
    # Exercise the exact Swift inventory/signature reader before notarization.
    # Python's manifest writer alone cannot prove the shipped reader accepts it.
    check_env = dict(os.environ, MNEMOSYNE_INSTALLER_ACCEPTANCE_BUNDLE=str(output.resolve()))
    subprocess.run(["/usr/bin/swift", "test", "--package-path", str(package), "--filter", "testSignedReleasePayloadInventory"], env=check_env, check=True)
    return output


def verify_existing(app: Path, installer: Path) -> Path:
    """Only reuse a signed assistant carrying exactly this product release."""
    app_info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    installer_info = plistlib.loads((installer / "Contents/Info.plist").read_bytes())
    manifest = json.loads((installer / "Contents/Resources/Release.json").read_text())
    signing = subprocess.run(["/usr/bin/codesign", "-d", "--verbose=4", str(app)], check=True, capture_output=True, text=True).stderr
    match = re.search(r"^TeamIdentifier=([A-Z0-9]{10})$", signing, re.MULTILINE)
    if not match or manifest.get("team") != match.group(1):
        raise ValueError("Installer and product signing teams differ")
    if (installer_info.get("CFBundleIdentifier") != "com.mnemosyne.inference.installer"
            or app_info.get("CFBundleIdentifier") != "com.mnemosyne.inference.menu"
            or manifest.get("format") != 1
            or manifest.get("build") != app_info["CFBundleVersion"]
            or manifest.get("version") != app_info["CFBundleShortVersionString"]
            or installer_info.get("CFBundleVersion") != app_info["CFBundleVersion"]
            or installer_info.get("CFBundleShortVersionString") != app_info["CFBundleShortVersionString"]):
        raise ValueError("Installer and product release identities differ")
    requirement = f'=anchor apple generic and identifier "com.mnemosyne.inference.installer" and certificate leaf[subject.OU] = "{match.group(1)}"'
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", "-R", requirement, str(installer)], check=True)
    if inventory(app) != manifest["entries"] or inventory(installer / "Contents/Resources" / APP_NAME) != manifest["entries"]:
        raise ValueError("Installer payload does not exactly match this product")
    return installer


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--output", type=Path)
    destination.add_argument("--verify-existing", type=Path)
    parser.add_argument("--identity")
    args = parser.parse_args()
    if args.verify_existing:
        print(verify_existing(args.app, args.verify_existing))
    else:
        if not args.identity:
            parser.error("--identity is required when building an installer")
        print(build(args.app, args.output, args.identity))
