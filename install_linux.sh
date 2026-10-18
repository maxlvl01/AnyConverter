#!/usr/bin/env bash
# Add AnyConverter to your Linux app menu (current user only, no sudo):
#   ./install_linux.sh             install
#   ./install_linux.sh --uninstall remove it again
#
# Uses dist/AnyConverter (or an AnyConverter binary next to this script) if
# there is one, otherwise runs anyconverter.py with python3.
set -euo pipefail
cd "$(dirname "$0")"

BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
DESKTOP="$APP_DIR/anyconverter.desktop"
TARGET="$BIN_DIR/anyconverter"

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -f "$TARGET" "$DESKTOP"
    rm -rf "$HOME/.local/share/anyconverter"
    echo "AnyConverter removed."
    exit 0
fi

mkdir -p "$BIN_DIR" "$APP_DIR"

if [[ -x dist/AnyConverter ]]; then
    install -m 755 dist/AnyConverter "$TARGET"
elif [[ -x AnyConverter ]]; then
    install -m 755 AnyConverter "$TARGET"
else
    # No binary: keep a copy of the script and a small launcher.
    mkdir -p "$HOME/.local/share/anyconverter"
    cp anyconverter.py "$HOME/.local/share/anyconverter/"
    printf '#!/bin/sh\nexec python3 "%s" "$@"\n' \
        "$HOME/.local/share/anyconverter/anyconverter.py" > "$TARGET"
    chmod 755 "$TARGET"
fi

cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=AnyConverter
Comment=Convert audio and video files into any format
Exec=$TARGET %F
Icon=multimedia-video-player
Terminal=false
Categories=AudioVideo;Audio;Video;Utility;
MimeType=video/mp4;video/x-matroska;video/webm;video/quicktime;video/x-msvideo;video/ogg;audio/mpeg;audio/x-wav;audio/flac;audio/ogg;audio/mp4;audio/aac;
EOF

command -v update-desktop-database >/dev/null && update-desktop-database "$APP_DIR" || true

echo "Installed. Find AnyConverter in your app menu, or run: anyconverter"
command -v ffmpeg >/dev/null || echo "Note: FFmpeg isn't installed yet. AnyConverter will show you how on first start."
