#!/usr/bin/env bash
# Build a native Linux binary of AnyConverter:  ./build_linux.sh
# Result: dist/AnyConverter  (single file, no Python needed to run it)
#
# Needs: python3, python3-venv, python3-tk
#   Debian/Ubuntu/Mint: sudo apt install python3 python3-venv python3-tk
#   Fedora:             sudo dnf install python3 python3-tkinter
#   Arch:               sudo pacman -S python tk
#
# Tip: build on the OLDEST distro you want to support; the binary runs on
# that distro and newer ones (it depends on the system's glibc version).
set -euo pipefail
cd "$(dirname "$0")"

python3 -c "import tkinter" 2>/dev/null || {
    echo "tkinter is missing. Install python3-tk (see the top of this script)." >&2
    exit 1
}

python3 -m venv .venv-build
.venv-build/bin/pip install --quiet --upgrade pip pyinstaller
.venv-build/bin/pyinstaller --noconfirm --onefile --windowed \
    --name AnyConverter anyconverter.py

echo
echo "Built: $(pwd)/dist/AnyConverter"
echo "Run it with ./dist/AnyConverter, or add it to your app menu with ./install_linux.sh"
