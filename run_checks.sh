#!/bin/bash
# run_checks.sh — local CI for PyPDF for Mac.
# Runs: python syntax check, JS syntax check (embedded frontend), ruff lint,
# and the pytest suite. Exits non-zero on the first failure.
# Usage: ./run_checks.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

echo "-> Python syntax"
python3 -m py_compile pdf_editor_app.py
python3 -m py_compile setup.py

echo "-> Frontend JS syntax (extracted from INDEX_HTML)"
python3 - <<'EOF'
import re
src = open("pdf_editor_app.py", encoding="utf-8").read()
m = re.search(r"<script>\n(.*)\n</script>", src, re.S)
assert m, "could not locate the embedded <script> block"
open("/tmp/pypdf_frontend_check.js", "w", encoding="utf-8").write(m.group(1))
EOF
if command -v node >/dev/null 2>&1; then
    node --check /tmp/pypdf_frontend_check.js
else
    echo "   (node not found — skipping JS check)"
fi

echo "-> Lint (ruff)"
if command -v ruff >/dev/null 2>&1; then
    ruff check .
else
    echo "   (ruff not found — 'pip install ruff' to enable)"
fi

echo "-> Tests (pytest)"
python3 -m pytest tests/ -q

echo "All checks passed."
