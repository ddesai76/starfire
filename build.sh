#!/usr/bin/env bash
#
# build.sh:       LINUX EXECUTABLE GENERATOR
# AUTHOR:         DANIEL DESAI
# UPDATED:        2026-10-11
# VERSION:        0.3.0


# Builds two Linux binaries into dist/: "starfire" (the app, via PyInstaller)
# and "starfire-hw" (the SCPI interface, from starfire.cpp via g++).
#
# Directory structure (pre-build):
# ├── assets
# │   └── icon.png
# ├── build.sh
# ├── jira_ticket.py
# ├── starfire.cpp
# ├── starfire.py
# └── static
#   └── starfire.css
#
# Usage: ./build.sh
#        ./build.sh --install-desktop                   (uses assets/icon.png)
#        ./build.sh --install-desktop path/to/icon.png   (overrides it)
# Output: dist/ containing starfire, starfire-hw, and (if you have one next to
#         build.sh) jira_config.json, which build.sh copies over. Modules you
#         have put in dist/ are left alone.

set -euo pipefail
cd "$(dirname "$0")"

if [ "$(id -u)" -eq 0 ]; then
  echo "WARNING: running as root -- PyInstaller 7.0 will refuse this." >&2
fi

ICON=""
if [ "${1:-}" = "--install-desktop" ]; then
  ICON="${2:-assets/icon.png}"
  if [ ! -f "$ICON" ]; then
    echo "ERROR: icon not found at $ICON" >&2
    echo "Usage: $0 --install-desktop [path-to-icon-image]" >&2
    echo "Defaults to assets/icon.png if no path is given." >&2
    exit 1
  fi
fi

missing=()
for f in starfire.py starfire.cpp jira_ticket.py static/starfire.css; do
  [ -f "$f" ] || missing+=("$f")
done
if [ "${#missing[@]}" -gt 0 ]; then
  echo "ERROR: missing required file(s) relative to build.sh:" >&2
  printf '  %s\n' "${missing[@]}" >&2
  exit 1
fi

if { [ -e dist/starfire ] && [ ! -w dist/starfire ]; } || { [ -d dist ] && [ ! -w dist ]; }; then
  echo "ERROR: dist/ (or dist/starfire) exists but isn't writable by $(whoami)." >&2
  echo "Usually means an earlier build ran as a different user (e.g. root)." >&2
  echo "Fix: sudo rm -rf dist   (then rebuild)" >&2
  exit 1
fi

if ! command -v g++ >/dev/null 2>&1; then
  echo "ERROR: g++ not found (needed to build starfire-hw). On Debian/Ubuntu: sudo apt install g++" >&2
  exit 1
fi

pip install --break-system-packages --quiet pyinstaller python-docx

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

pyinstaller --onefile --name starfire \
  --add-data "$(pwd)/static:static" \
  --workpath "$WORKDIR" \
  --specpath "$WORKDIR" \
  starfire.py

g++ -O2 -Wall -Wextra -std=c++17 -o dist/starfire-hw starfire.cpp

# Jira is optional: copied in only if you have a config next to build.sh.
if [ -f jira_config.json ]; then
  cp jira_config.json dist/jira_config.json
  JIRA_NOTE="copied jira_config.json"
else
  JIRA_NOTE="no jira_config.json here -- Jira ticketing stays off"
fi

echo
echo "Built: dist/starfire, dist/starfire-hw ($JIRA_NOTE)"
echo "starfire finds starfire-hw next to itself, so keep the two together."

if [ -n "$ICON" ]; then
  BINARY="$(realpath dist/starfire)"
  RUNDIR="$HOME/Starfire"
  mkdir -p "$RUNDIR"

  # Jira reads jira_config.json from the working directory, which for the
  # menu entry is RUNDIR; starfire-hw is found beside the binary, so stays put.
  if [ -f dist/jira_config.json ]; then
    cp dist/jira_config.json "$RUNDIR/"
    echo "Copied dist/jira_config.json -> $RUNDIR/jira_config.json"
  fi

  ICON_DIR="$HOME/.local/share/icons"
  mkdir -p "$ICON_DIR"
  ICON_EXT="${ICON##*.}"
  ICON_DEST="$ICON_DIR/starfire.$ICON_EXT"
  cp "$ICON" "$ICON_DEST"

  # Linux binaries can't embed an icon, so this is the closest equivalent:
  # file managers that honor it (GNOME Files, Nemo, Caja) show it on the file.
  if command -v gio >/dev/null 2>&1; then
    gio set "$BINARY" metadata::custom-icon "file://$ICON_DEST" 2>/dev/null || true
  fi

  DESKTOP_DIR="$HOME/.local/share/applications"
  mkdir -p "$DESKTOP_DIR"
  DESKTOP_FILE="$DESKTOP_DIR/starfire.desktop"

  cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=STARFIRE
Comment=Test campaign and test card designer with hardware measure/activate
Exec=$BINARY
Path=$RUNDIR
Icon=$ICON_DEST
Terminal=false
Categories=Development;Engineering;
EOF
  chmod +x "$DESKTOP_FILE"

  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
  fi

  echo
  echo "Installed. STARFIRE should now appear in your application menu/search --"
  echo "right-click it there to pin it to the dash/sidebar."
  echo "Working directory: $RUNDIR"
  echo "Binary:            $BINARY"
fi
