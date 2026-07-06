#!/bin/bash
#
# build_native.command — build a self-contained "PyPDF for Mac.app"
# ---------------------------------------------------------------------------
# Double-click this file (or run it in Terminal). It will:
#   1. Create a clean build virtualenv.
#   2. Install py2app + PyMuPDF + pywebview (+ pyobjc) into it.
#   3. Turn appicon.png into a proper .icns.
#   4. Build dist/"PyPDF for Mac.app" with py2app (bundles its own Python).
#   5. Ad-hoc code-sign it so it runs on THIS Mac with no Developer account.
#   6. Register it as a PDF handler and install it to /Applications.
#
# Needs internet once (to download the build dependencies).
# Re-run any time to rebuild.
# ---------------------------------------------------------------------------
set -euo pipefail

APP_NAME="PyPDF for Mac"
BUNDLE_ID="com.pyedit.pdfeditor"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=================================================="
echo "  Building \"$APP_NAME\" (native, self-contained)"
echo "=================================================="

# --- 0. sanity -------------------------------------------------------------
[ -f pdf_editor_app.py ] || { echo "ERROR: pdf_editor_app.py not found next to this script."; exit 1; }
[ -f setup.py ]          || { echo "ERROR: setup.py not found next to this script."; exit 1; }

# --- 1. pick a python3 (prefer a newer one than macOS system 3.9) ----------
PYTHON=""
for c in \
    /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3 \
    /usr/local/bin/python3.12 /usr/local/bin/python3.11 /usr/local/bin/python3 \
    "$(command -v python3.12 || true)" "$(command -v python3.11 || true)" \
    /usr/bin/python3 "$(command -v python3 || true)"; do
    [ -n "$c" ] && [ -x "$c" ] && PYTHON="$c" && break
done
[ -z "$PYTHON" ] && { echo "ERROR: no python3 found. Install Python 3 first."; exit 1; }
PYVER="$("$PYTHON" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
echo "-> Using python: $PYTHON  (Python $PYVER)"
if [ "$PYVER" = "3.9" ]; then
    echo "   Note: building with the macOS system Python 3.9. This works, but a"
    echo "   newer Python (e.g. 'brew install python@3.12') is more reliable for"
    echo "   building .app bundles. Continuing with 3.9 ..."
fi

# --- 2. clean build venv ---------------------------------------------------
echo "-> Creating build virtualenv ..."
rm -rf build dist .build_venv
"$PYTHON" -m venv .build_venv
# shellcheck disable=SC1091
source .build_venv/bin/activate
python -m pip install --quiet --upgrade pip wheel setuptools

echo "-> Installing build dependencies (PyInstaller, PyMuPDF, pywebview, pyobjc) ..."
# PyInstaller bundles pkg_resources/jaraco/pyobjc automatically via its hooks,
# so none of the setuptools-vendoring problems that broke py2app apply here.
python -m pip install --quiet pyinstaller pymupdf pywebview pyobjc

# --- 3. build the icon -----------------------------------------------------
if [ -f appicon.png ]; then
    echo "-> Building app icon ..."
    ICONSET="$(mktemp -d)/AppIcon.iconset"
    mkdir -p "$ICONSET"
    for sz in 16 32 64 128 256 512; do
        sips -z $sz $sz             appicon.png --out "$ICONSET/icon_${sz}x${sz}.png"     >/dev/null 2>&1 || true
        sips -z $((sz*2)) $((sz*2)) appicon.png --out "$ICONSET/icon_${sz}x${sz}@2x.png" >/dev/null 2>&1 || true
    done
    iconutil -c icns "$ICONSET" -o AppIcon.icns 2>/dev/null \
        && echo "   Icon built." \
        || echo "   WARNING: icon build failed — the app will use a generic icon."
fi

# --- 4. build the .app -----------------------------------------------------
echo "-> Building the app bundle with PyInstaller ..."
pyinstaller --noconfirm --clean PyPDF.spec

APP_PATH="dist/$APP_NAME.app"
[ -d "$APP_PATH" ] || { echo "ERROR: build did not produce $APP_PATH"; exit 1; }

# PyInstaller leaves the intermediate COLLECT folder ("dist/PyPDF for Mac")
# next to the .app, which shows up as a confusing second item in Finder.
# Remove it so only the .app remains.
rm -rf "dist/$APP_NAME"

# --- 5. ad-hoc code sign (personal use, no Developer account) ---------------
echo "-> Ad-hoc signing the app ..."
codesign --force --deep --sign - "$APP_PATH" 2>/dev/null \
    && echo "   Signed (ad-hoc)." \
    || echo "   WARNING: ad-hoc signing failed — the app may still run after a Gatekeeper prompt."

# remove the quarantine attribute so it opens without 'unidentified developer'
xattr -dr com.apple.quarantine "$APP_PATH" 2>/dev/null || true

deactivate || true

# --- 6. install to /Applications -------------------------------------------
echo "-> Installing to /Applications ..."
DEST="/Applications/$APP_NAME.app"
# stop any running copy so a stale server doesn't linger
pkill -f "pdf_editor_app.py" 2>/dev/null || true
rm -rf "$DEST"
cp -R "$APP_PATH" "$DEST"
xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true

# register with LaunchServices so "Open With" sees it
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
"$LSREGISTER" -f "$DEST" 2>/dev/null || true

# Remove the build copy in ./dist so Spotlight/Finder only ever show the one in
# /Applications (otherwise the app appears twice). The installed copy is the
# canonical one; build/ and dist/ are just scratch.
"$LSREGISTER" -u "$APP_PATH" 2>/dev/null || true
rm -rf build dist
# Nudge Spotlight to drop the now-deleted dist copy from its index.
mdimport -r "$DEST" 2>/dev/null || true

# --- 6b. optionally set as the DEFAULT app for all PDFs --------------------
read -r -p "Set \"$APP_NAME\" as the default app for ALL PDFs? [y/N] " ans || true
if [[ "${ans:-N}" =~ ^[Yy]$ ]]; then
    /usr/bin/python3 - "$BUNDLE_ID" <<'PYDEF' 2>/dev/null || true
import sys, plistlib, os
bundle_id = sys.argv[1]
p = os.path.expanduser("~/Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist")
try:
    with open(p, "rb") as f: data = plistlib.load(f)
except Exception:
    data = {}
h = [x for x in data.get("LSHandlers", []) if x.get("LSHandlerContentType") != "com.adobe.pdf"]
h.append({"LSHandlerContentType": "com.adobe.pdf", "LSHandlerRoleAll": bundle_id})
data["LSHandlers"] = h
os.makedirs(os.path.dirname(p), exist_ok=True)
with open(p, "wb") as f: plistlib.dump(data, f)
print("   Default PDF handler set to " + bundle_id)
PYDEF
    "$LSREGISTER" -kill -r -domain local -domain system -domain user 2>/dev/null || true
    /usr/bin/killall Finder 2>/dev/null || true
fi

echo ""
echo "=================================================="
echo "  Done!  Installed: $DEST"
echo "  * Launch it from /Applications or Launchpad."
echo "  * Double-click PDFs (or Open With) to open them in tabs."
echo "=================================================="
read -n 1 -s -r -p "Press any key to close this window..." || true
echo ""
