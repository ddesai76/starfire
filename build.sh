#!/usr/bin/env bash
#
# build.sh:       LINUX EXECUTABLE GENERATOR
# AUTHOR:         DANIEL DESAI
# UPDATED:        2026-10-11
# VERSION:        0.2.0


# Builds the single "starfire" Linux binary via PyInstaller.
#
# Directory structure (pre-build):
# ├── assets
# │   └── icon.png
# ├── build.sh
# ├── jira_ticket.py
# ├── starfire.py
# └── static
#   └── starfire.css
#
# Usage: ./build.sh
#        ./build.sh --install-desktop                   (uses assets/icon.png)
#        ./build.sh --install-desktop path/to/icon.png   (overrides it)
# Output: dist/starfire
# Place jira_config.json into dist/ to enable jira ticketing

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
for f in starfire.py jira_ticket.py static/starfire.css; do
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

pip install --break-system-packages --quiet pyinstaller python-docx

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

pyinstaller --onefile --name starfire \
  --add-data "$(pwd)/static:static" \
  --workpath "$WORKDIR" \
  --specpath "$WORKDIR" \
  starfire.py

echo
echo "Built: dist/starfire"
echo "Optional (drop next to the binary before running, not required):"
echo "  jira_config.json  -- enables Jira ticket creation"
echo "  measure           -- enables the Sample / Activate buttons"

if [ -n "$ICON" ]; then
  BINARY="$(realpath dist/starfire)"
  RUNDIR="$HOME/Starfire"
  mkdir -p "$RUNDIR"

  for f in jira_config.json measure; do
    if [ -f "dist/$f" ]; then
      mv "dist/$f" "$RUNDIR/"
      echo "Moved dist/$f -> $RUNDIR/$f"
    fi
  done

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
