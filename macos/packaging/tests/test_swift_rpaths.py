import subprocess
from pathlib import Path
from unittest.mock import patch

from macos.packaging.swift_rpaths import nonportable_rpaths, normalize


def test_normalize_removes_toolchain_and_build_paths_before_signing():
    safe = 'path /usr/lib/swift (offset 12)\npath @loader_path (offset 12)\n'
    unsafe = (
        'path /Applications/Xcode.app/Contents/Developer/usr/lib/swift (offset 12)\n'
        'path /Users/build/checkout/.build/release (offset 12)\n'
    )
    executable = Path('/stage/Unified Inference.app/Contents/MacOS/UnifiedInference')
    with patch('macos.packaging.swift_rpaths.subprocess.run', side_effect=[
        subprocess.CompletedProcess([], 0, stdout=safe + unsafe),
        subprocess.CompletedProcess([], 0),
        subprocess.CompletedProcess([], 0),
        subprocess.CompletedProcess([], 0, stdout=safe),
    ]) as run:
        normalize(executable)
    assert [call.args[0][1:3] for call in run.call_args_list[1:3]] == [
        ['-delete_rpath', '/Applications/Xcode.app/Contents/Developer/usr/lib/swift'],
        ['-delete_rpath', '/Users/build/checkout/.build/release'],
    ]
    assert nonportable_rpaths(safe + 'path @executable_path/../Frameworks (offset 12)\n') == ()
