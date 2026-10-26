#!/usr/bin/env bash
#
# starfi.sh:      BUILD AND RUN LAUNCHER (LINUX)
# AUTHOR:         DANIEL DESAI
# UPDATED:        2026-10-23
# VERSION:        0.3.0

# Builds the Linux binary `starfire` (via PyInstaller) into a dist folder, and/or runs it.
# The default dist folder is dist/.
#
# Directory structure (pre-build):
# ├── starfi.sh
# ├── starfire.py              (builds to starfire)
# ├── signet.py                (optional: Signet, built into starfire; without it Signals are off)
# └── static
#     ├── assets
#     │   └── icon.png          (optional; only for --install-desktop)
#     ├── campaign.html
#     ├── starfire.css
#     ├── system.html
#     └── test.html
#
# Flags:
#   (none)            Run only, don't build
#   --build           Build only, don't run
#   --all             Build and run
#   --clean           Delete the dist folder first; on its own it only deletes it
#   --new NAME        Build into a new dist folder called NAME; needs --build or --all
#                     (alone it stops with "New dist requires build"); --clean has no effect with it
#   --install-desktop [ICON]   Add an application-menu entry (with --build or --all; default static/assets/icon.png, optional)
#
# Typical usages:
#   * Run from an existing dist:               bash starfi.sh
#   * Build in an existing dist:               bash starfi.sh --clean --build
#   * Rebuild and run in an existing dist:     bash starfi.sh --clean --all
#   * Delete the dist folder:                  bash starfi.sh --clean
#   * Build as a new dist:                     bash starfi.sh --build --new dist2
#   * Build and run as a new dist:             bash starfi.sh --all --new dist2
#
# Output: a dist folder containing starfire and (if placed next to starfi.sh) jira_config.json.
# Modules already in the dist folder are unaffected, unless --clean removes the folder.

set -euo pipefail
cd "$(dirname "$0")"

USAGE="Usage: $0 [--build | --all] [--clean] [--new NAME] [--install-desktop [path-to-icon-image]]"

DIST="dist"
BUILD=0
RUN=0
CLEAN=0
NEW=0
ICON=""
INSTALL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --build) BUILD=1; shift ;;
    --all) BUILD=1; RUN=1; shift ;;
    --clean) CLEAN=1; shift ;;
    --new)
      shift
      if [ $# -eq 0 ] || [ "${1#--}" != "$1" ]; then
        echo "ERROR: --new needs a name, e.g. --new dist2" >&2; exit 2
      fi
      DIST="$1"; NEW=1; shift ;;
    --install-desktop)
      INSTALL=1; ICON="static/assets/icon.png"; shift
      if [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; then
        ICON="$1"; shift
        if [ ! -f "$ICON" ]; then echo "ERROR: icon not found at $ICON" >&2; exit 1; fi
      elif [ ! -f "$ICON" ]; then
        echo "NOTE: no icon at $ICON -- the menu entry will have no icon." >&2
        ICON=""
      fi ;;
    *) echo "$USAGE" >&2; exit 2 ;;
  esac
done

case "$DIST" in
  ""|.|..|*/*|-*) echo "ERROR: dist name must be a plain folder name, not '$DIST'" >&2; exit 2 ;;
esac

if [ "$NEW" -eq 1 ] && [ "$BUILD" -eq 0 ]; then
  echo "New dist requires build" >&2
  exit 1
fi

# --clean on its own just deletes the dist folder.
if [ "$CLEAN" -eq 1 ] && [ "$BUILD" -eq 0 ]; then
  rm -rf "$DIST"
  echo "Cleaned $DIST/"
  exit 0
fi

# Nothing to run from a new dist yet, so nothing to clean either.
if [ "$NEW" -eq 1 ]; then
  if [ -e "$DIST" ]; then
    echo "ERROR: $DIST already exists. Pick another name, or build into it without --new." >&2
    exit 1
  fi
  CLEAN=0
fi

if [ "$BUILD" -eq 1 ]; then
  if [ "$(id -u)" -eq 0 ]; then
    echo "WARNING: running as root -- PyInstaller 7.0 will refuse this." >&2
  fi

  missing=()
  for f in starfire.py static/starfire.css static/test.html static/campaign.html static/system.html; do
    [ -f "$f" ] || missing+=("$f")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    echo "ERROR: missing required file(s) relative to starfi.sh:" >&2
    printf '  %s\n' "${missing[@]}" >&2
    exit 1
  fi

  if [ ! -f signet.py ]; then
    echo "NOTE: no signet.py next to starfi.sh -- building without Signet, so Signals are off." >&2
  fi

  if [ "$CLEAN" -eq 1 ]; then
    rm -rf "$DIST"
    echo "Cleaned $DIST/"
  fi

  if { [ -e "$DIST/starfire" ] && [ ! -w "$DIST/starfire" ]; } || { [ -d "$DIST" ] && [ ! -w "$DIST" ]; }; then
    echo "ERROR: $DIST/ (or $DIST/starfire) exists but is not writable by $(whoami)." >&2
    echo "Usually means an earlier build ran as a different user (e.g. root)." >&2
    echo "Fix: sudo rm -rf $DIST   (then rebuild)" >&2
    exit 1
  fi

  pip install --break-system-packages --quiet pyinstaller python-docx numpy

  WORKDIR="$(mktemp -d)"
  trap 'rm -rf "$WORKDIR"' EXIT

  mkdir -p "$DIST"
  pyinstaller --onefile --name starfire \
    --add-data "$(pwd)/static:static" \
    --distpath "$(pwd)/$DIST" \
    --workpath "$WORKDIR" \
    --specpath "$WORKDIR" \
    starfire.py

  # Jira is optional: copied in only if you have a config next to starfi.sh.
  if [ -f jira_config.json ]; then
    cp jira_config.json "$DIST/jira_config.json"
    JIRA_NOTE="copied jira_config.json"
  else
    JIRA_NOTE="no jira_config.json here -- Jira ticketing stays off"
  fi

  echo
  echo "Built: $DIST/starfire ($JIRA_NOTE)"

  if [ "$INSTALL" -eq 1 ]; then
    BINARY="$(realpath "$DIST/starfire")"
    RUNDIR="$HOME/Starfire"
    mkdir -p "$RUNDIR"

    # Jira reads jira_config.json from the working directory, which for the
    # menu entry is RUNDIR.
    if [ -f "$DIST/jira_config.json" ]; then
      cp "$DIST/jira_config.json" "$RUNDIR/"
      echo "Copied $DIST/jira_config.json -> $RUNDIR/jira_config.json"
    fi

    ICON_DEST=""
    if [ -n "$ICON" ]; then
      ICON_DIR="$HOME/.local/share/icons"
      mkdir -p "$ICON_DIR"
      ICON_DEST="$ICON_DIR/starfire.${ICON##*.}"
      cp "$ICON" "$ICON_DEST"

      # Linux binaries can't embed an icon, so this is the closest equivalent;
      # file managers that honor it (GNOME Files, Nemo, Caja) show it on the file.
      if command -v gio >/dev/null 2>&1; then
        gio set "$BINARY" metadata::custom-icon "file://$ICON_DEST" 2>/dev/null || true
      fi
    fi

    DESKTOP_DIR="$HOME/.local/share/applications"
    mkdir -p "$DESKTOP_DIR"
    DESKTOP_FILE="$DESKTOP_DIR/starfire.desktop"

    cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=STARFIRE
Comment=Test campaign and test card designer with modules and signals
Exec=$BINARY
Path=$RUNDIR
${ICON_DEST:+Icon=$ICON_DEST}
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
elif [ "$INSTALL" -eq 1 ]; then
  echo "--install-desktop only works with --build or --all" >&2
  exit 1
fi

# The default action, and the second half of --all. Runs from the dist folder,
# wherein the cards and modules of a source checkout.
if [ "$BUILD" -eq 0 ] || [ "$RUN" -eq 1 ]; then
  if [ ! -x "$DIST/starfire" ]; then
    echo "ERROR: $DIST/starfire not found. Build first: bash $0 --build" >&2
    exit 1
  fi
  if (exec 3<>/dev/tcp/127.0.0.1/5791) 2>/dev/null; then
    echo "ERROR: port 5791 is already in use (Starfire may already be running)." >&2
    echo "Free it with: sudo fuser -k 5791/tcp" >&2
    exit 1
  fi
  echo
  cd "$DIST"
  exec ./starfire
fi
