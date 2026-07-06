# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for "PyPDF for Mac" (native pywebview build).
# Build it via build_native.command, or directly:
#     pyinstaller --noconfirm --clean PyPDF.spec
#
# Produces dist/"PyPDF for Mac.app" — a self-contained bundle with its own
# Python, PyMuPDF and pywebview. Written for PyInstaller 6.x.

import re

from PyInstaller.utils.hooks import collect_all, collect_submodules

# Single-source the bundle version from the app itself (BUNDLE_VERSION in
# pdf_editor_app.py) so About, Finder Get Info, and hand-off can never drift.
with open("pdf_editor_app.py", encoding="utf-8") as _fh:
    APP_BUNDLE_VERSION = re.search(
        r'^BUNDLE_VERSION\s*=\s*"([^"]+)"', _fh.read(), re.M).group(1)

datas = []
binaries = []
hiddenimports = []

# Pull in everything PyMuPDF and pywebview need (data files, the compiled
# extension, and the Cocoa/WebKit backend). pywebview also ships its own
# PyInstaller hook which Analysis applies automatically.
for pkg in ("fitz", "pymupdf", "webview"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# pyobjc bridge used by pywebview's macOS backend.
hiddenimports += collect_submodules("objc")
hiddenimports += [
    "Foundation", "AppKit", "WebKit", "Quartz", "Security", "Cocoa",
]

a = Analysis(
    ["pdf_editor_app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "matplotlib", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PyPDF for Mac",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,             # windowed app — no terminal
    disable_windowed_traceback=False,
    argv_emulation=True,       # so double-clicked PDFs arrive in sys.argv
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="AppIcon.icns",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PyPDF for Mac",
)

app = BUNDLE(
    coll,
    name="PyPDF for Mac.app",
    icon="AppIcon.icns",
    bundle_identifier="com.pyedit.pdfeditor",
    version=APP_BUNDLE_VERSION,
    info_plist={
        "CFBundleName": "PyPDF for Mac",
        "CFBundleDisplayName": "PyPDF for Mac",
        "CFBundleShortVersionString": APP_BUNDLE_VERSION,
        "CFBundleVersion": APP_BUNDLE_VERSION,
        "LSMinimumSystemVersion": "10.14",
        "NSHighResolutionCapable": True,
        # Declare the app as a PDF editor (shows in "Open With", can be default).
        "CFBundleDocumentTypes": [
            {
                "CFBundleTypeName": "PDF Document",
                "CFBundleTypeRole": "Editor",
                "LSHandlerRank": "Owner",
                "LSItemContentTypes": ["com.adobe.pdf"],
                "CFBundleTypeExtensions": ["pdf"],
            }
        ],
    },
)
