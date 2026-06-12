#!/bin/bash
#
# build_app.command  -  Build & install "PyPDF Editor.app" on macOS
# ---------------------------------------------------------------------------
# Double-click this file (or run it in Terminal). It will:
#   1. Find a python3 that has PyMuPDF (or fall back to the system python3).
#   2. Compile an AppleScript launcher into a real .app bundle.
#   3. Embed the PDF editor + icon inside the bundle.
#   4. Register the app as a handler for PDF files.
#   5. (Optional) Set it as the DEFAULT app for all PDFs.
#   6. Install it to /Applications.
#
# Re-run any time to update the app.
# ---------------------------------------------------------------------------

set -e

APP_NAME="PyPDF Editor"
BUNDLE_ID="com.pyedit.pdfeditor"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SRC="$SCRIPT_DIR/pdf_editor_app.py"
ICON_PNG="$SCRIPT_DIR/appicon.png"
DEST="/Applications/$APP_NAME.app"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

echo "=================================================="
echo "  Building \"$APP_NAME\""
echo "=================================================="

# --- sanity checks ---------------------------------------------------------
if [ ! -f "$PY_SRC" ]; then
    echo "ERROR: cannot find pdf_editor_app.py next to this script."
    echo "       Expected at: $PY_SRC"
    read -n 1 -s -r -p "Press any key to close..."
    exit 1
fi

# --- 1. choose a python3 ---------------------------------------------------
echo "-> Looking for a python3 with PyMuPDF installed ..."
PYTHON=""
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3 "$(command -v python3)"; do
    [ -x "$c" ] || continue
    if "$c" -c "import fitz" >/dev/null 2>&1; then
        PYTHON="$c"
        echo "   Found PyMuPDF in: $PYTHON"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    # fall back to first available python3; the app will try to auto-install PyMuPDF
    for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
        [ -x "$c" ] && PYTHON="$c" && break
    done
    echo "   PyMuPDF not found yet. Using $PYTHON (app will try to install it on first run)."
fi
[ -z "$PYTHON" ] && { echo "ERROR: no python3 found. Install Python 3 first."; exit 1; }

# --- 2. compile the AppleScript launcher into an .app ----------------------
echo "-> Compiling app launcher ..."
LAUNCHER="$TMP_DIR/launcher.applescript"
cat > "$LAUNCHER" <<APPLESCRIPT
-- PyPDF Editor launcher
property pythonPath : "$PYTHON"

on run
    launchEditor("")
end run

on open theFiles
    repeat with f in theFiles
        launchEditor(POSIX path of f)
    end repeat
end open

on launchEditor(filePath)
    set pyScript to POSIX path of (path to resource "pdf_editor_app.py")
    if filePath is "" then
        do shell script quoted form of pythonPath & " " & quoted form of pyScript & " > /dev/null 2>&1 &"
    else
        do shell script quoted form of pythonPath & " " & quoted form of pyScript & " " & quoted form of filePath & " > /dev/null 2>&1 &"
    end if
end launchEditor
APPLESCRIPT

rm -rf "$DEST"
osacompile -o "$DEST" "$LAUNCHER"

# --- 3. embed editor + icon ------------------------------------------------
echo "-> Embedding editor and icon ..."
RES="$DEST/Contents/Resources"
cp "$PY_SRC" "$RES/pdf_editor_app.py"

if [ -f "$ICON_PNG" ]; then
    ICONSET="$TMP_DIR/AppIcon.iconset"
    mkdir -p "$ICONSET"
    for sz in 16 32 64 128 256 512; do
        sips -z $sz $sz       "$ICON_PNG" --out "$ICONSET/icon_${sz}x${sz}.png"       >/dev/null 2>&1
        sips -z $((sz*2)) $((sz*2)) "$ICON_PNG" --out "$ICONSET/icon_${sz}x${sz}@2x.png" >/dev/null 2>&1
    done
    iconutil -c icns "$ICONSET" -o "$RES/applet.icns" 2>/dev/null || true
fi

# --- 4. patch Info.plist (identity + PDF document type) --------------------
echo "-> Configuring Info.plist (PDF handler) ..."
PLIST="$DEST/Contents/Info.plist"
PB=/usr/libexec/PlistBuddy

$PB -c "Set :CFBundleIdentifier $BUNDLE_ID"        "$PLIST" 2>/dev/null || $PB -c "Add :CFBundleIdentifier string $BUNDLE_ID" "$PLIST"
$PB -c "Set :CFBundleName '$APP_NAME'"             "$PLIST" 2>/dev/null || $PB -c "Add :CFBundleName string '$APP_NAME'" "$PLIST"
$PB -c "Set :LSMinimumSystemVersion 10.13"         "$PLIST" 2>/dev/null || $PB -c "Add :LSMinimumSystemVersion string 10.13" "$PLIST"

# Version is read from APP_VERSION in pdf_editor_app.py so the bundle always
# matches the editor build inside it.
APP_VER="$(grep -m1 '^APP_VERSION' "$PY_SRC" | sed 's/.*"\([^"]*\)".*/\1/')"
[ -z "$APP_VER" ] && APP_VER="1.0"
$PB -c "Set :CFBundleShortVersionString $APP_VER"  "$PLIST" 2>/dev/null || $PB -c "Add :CFBundleShortVersionString string $APP_VER" "$PLIST"
$PB -c "Set :CFBundleVersion $APP_VER"             "$PLIST" 2>/dev/null || $PB -c "Add :CFBundleVersion string $APP_VER" "$PLIST"

# Declare that this app opens PDF files (shows in Open With; eligible as default)
$PB -c "Delete :CFBundleDocumentTypes" "$PLIST" 2>/dev/null || true
$PB -c "Add :CFBundleDocumentTypes array" "$PLIST"
$PB -c "Add :CFBundleDocumentTypes:0 dict" "$PLIST"
$PB -c "Add :CFBundleDocumentTypes:0:CFBundleTypeName string 'PDF Document'" "$PLIST"
$PB -c "Add :CFBundleDocumentTypes:0:CFBundleTypeRole string Editor" "$PLIST"
$PB -c "Add :CFBundleDocumentTypes:0:LSHandlerRank string Owner" "$PLIST"
$PB -c "Add :CFBundleDocumentTypes:0:LSItemContentTypes array" "$PLIST"
$PB -c "Add :CFBundleDocumentTypes:0:LSItemContentTypes:0 string com.adobe.pdf" "$PLIST"
$PB -c "Add :CFBundleDocumentTypes:0:CFBundleTypeExtensions array" "$PLIST"
$PB -c "Add :CFBundleDocumentTypes:0:CFBundleTypeExtensions:0 string pdf" "$PLIST"

# --- 5. register with LaunchServices ---------------------------------------
echo "-> Registering with macOS ..."
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
"$LSREGISTER" -f "$DEST" 2>/dev/null || true

# Remove quarantine so it opens without a Gatekeeper prompt
xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true

# --- 6. set as DEFAULT app for all PDFs ------------------------------------
echo "-> Setting \"$APP_NAME\" as the default app for all PDFs ..."
DEFAULTS=com.apple.LaunchServices/com.apple.launchservices.secure
/usr/bin/python3 - "$BUNDLE_ID" <<'PYDEF' 2>/dev/null || true
import subprocess, sys, plistlib, os
bundle_id = sys.argv[1]
domain = "com.apple.LaunchServices/com.apple.launchservices.secure"
plist_path = os.path.expanduser("~/Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist")
try:
    with open(plist_path, "rb") as f:
        data = plistlib.load(f)
except Exception:
    data = {}
handlers = data.get("LSHandlers", [])
handlers = [h for h in handlers if h.get("LSHandlerContentType") != "com.adobe.pdf"]
handlers.append({"LSHandlerContentType": "com.adobe.pdf", "LSHandlerRoleAll": bundle_id})
data["LSHandlers"] = handlers
os.makedirs(os.path.dirname(plist_path), exist_ok=True)
with open(plist_path, "wb") as f:
    plistlib.dump(data, f)
print("   Default handler updated.")
PYDEF

"$LSREGISTER" -kill -r -domain local -domain system -domain user 2>/dev/null || true
/usr/bin/killall Finder 2>/dev/null || true

echo ""
echo "=================================================="
echo "  Done!  Installed: $DEST"
echo "--------------------------------------------------"
echo "  * Double-click any PDF -> opens in $APP_NAME"
echo "  * Right-click a PDF -> Open With -> $APP_NAME"
echo "  * It's in your Applications folder & Launchpad"
echo ""
echo "  If a PDF still opens in Preview, log out and back"
echo "  in once (or restart) so macOS picks up the change."
echo "=================================================="
echo ""
read -n 1 -s -r -p "Press any key to close this window..."
echo ""
