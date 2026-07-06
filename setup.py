"""
py2app build script for "PyPDF for Mac" (native pywebview build).

Builds a fully self-contained .app that bundles its own Python, PyMuPDF and
pywebview, so the end user needs nothing installed. Build it with the helper:

    ./build_native.command

or manually inside the build venv:

    python setup.py py2app

The result is dist/PyPDF for Mac.app. It is ad-hoc signed by the helper script
so it runs on the Mac that built it without an Apple Developer account.
"""
import re

from setuptools import setup

APP = ["pdf_editor_app.py"]
APP_NAME = "PyPDF for Mac"
BUNDLE_ID = "com.pyedit.pdfeditor"
# Single-sourced from BUNDLE_VERSION in pdf_editor_app.py (same as PyPDF.spec).
with open("pdf_editor_app.py", encoding="utf-8") as _fh:
    VERSION = re.search(r'^BUNDLE_VERSION\s*=\s*"([^"]+)"', _fh.read(), re.M).group(1)

DATA_FILES = ["appicon.png"]

PLIST = {
    "CFBundleName": APP_NAME,
    "CFBundleDisplayName": APP_NAME,
    "CFBundleIdentifier": BUNDLE_ID,
    "CFBundleShortVersionString": VERSION,
    "CFBundleVersion": VERSION,
    "LSMinimumSystemVersion": "10.14",
    # A windowed app (has a UI, no Dock-less background process).
    "LSUIElement": False,
    "NSHighResolutionCapable": True,
    # Declare the app as a PDF editor so it shows up in "Open With" and can be
    # set as the default handler for PDFs.
    "CFBundleDocumentTypes": [
        {
            "CFBundleTypeName": "PDF Document",
            "CFBundleTypeRole": "Editor",
            "LSHandlerRank": "Owner",
            "LSItemContentTypes": ["com.adobe.pdf"],
            "CFBundleTypeExtensions": ["pdf"],
        }
    ],
}

OPTIONS = {
    "argv_emulation": False,   # we read sys.argv ourselves; emulation is flaky on recent macOS
    "iconfile": "AppIcon.icns",  # generated from appicon.png by build_native.command
    "plist": PLIST,
    # pywebview pulls in its Cocoa/WebKit backend via pyobjc; PyMuPDF is `fitz`.
    "packages": ["webview", "fitz"],
    "includes": [
        "objc", "Foundation", "AppKit", "WebKit", "Quartz", "Security",
    ],
    # Keep the bundle lean; these are not used.
    "excludes": ["tkinter", "PyQt5", "PySide2", "PySide6", "matplotlib", "pytest"],
}

setup(
    app=APP,
    name=APP_NAME,
    version=VERSION,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    # NOTE: py2app is installed into the build venv by build_native.command, so
    # we deliberately do NOT use setup_requires=["py2app"] here — that triggers
    # setuptools' deprecated fetch_build_eggs path and a second, broken import.
)
