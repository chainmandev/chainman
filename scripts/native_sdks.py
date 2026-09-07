"""Fail visibly when an explicitly selected platform lacks its native SDK."""

import argparse
import os
from pathlib import Path
import platform
import subprocess


def main() -> None:
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("platform", choices=["apple", "android"])
    args = parser.parse_args()
    if os.environ.get("TOOLCHAIN_CONTAINER") == "1":
        raise SystemExit(
            "Native SDK lanes require host Nix with the platform SDK explicitly installed."
        )
    if args.platform == "apple":
        if platform.system() != "Darwin":
            raise SystemExit(
                "Apple native verification requires macOS with Xcode and its selected SDK."
            )
        for command in (
            ["xcodebuild", "-version"],
            ["xcrun", "--sdk", "macosx", "--show-sdk-path"],
            ["xcrun", "swift", "--version"],
        ):
            subprocess.run(command, check=True)
    else:
        sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
        if not sdk or not Path(sdk).is_dir():
            raise SystemExit(
                "Set ANDROID_HOME to the explicitly installed Android SDK."
            )
        subprocess.run([str(Path(sdk) / "platform-tools/adb"), "version"], check=True)
        subprocess.run(["flutter", "doctor", "--verbose"], check=True)
        print(
            "SDK discovery completed. This does not qualify licenses, app packaging, an emulator or a device."
        )


if __name__ == "__main__":
    main()
