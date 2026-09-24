#!/usr/bin/env bash
# Rule 4 — DRY & SOLID: complexity and duplication
PROJECT_PATH="${1:-$(pwd)}"
FOUND=0
MAX_FUNCTION_LINES=50
MAX_FILE_LINES=400

echo "  Checking code complexity..."

EXCLUDE_PATH="node_modules\|.git\|dist\|build\|vendor\|__pycache__\|.policy-reports\|.min.js"

# ---- Large functions (proxy for complexity) ----
echo "  Scanning for oversized functions..."

# JS/TS: functions longer than MAX_FUNCTION_LINES
large_js=$(find "$PROJECT_PATH" \( -name "*.js" -o -name "*.ts" \) \
  -not -path "*/node_modules/*" -not -path "*/dist/*" -not -path "*/.git/*" \
  -not -name "*.min.js" 2>/dev/null | while read -r f; do
  awk -v max="$MAX_FUNCTION_LINES" -v file="$f" '
    /function\s+\w+\s*\(|=>\s*\{|\bfunction\s*\(/ { start=NR; fname=$0 }
    start && /^\s*\}/ {
      len = NR - start
      if (len > max) print file ":" start ": function ~" len " lines (max " max ")"
      start=0
    }
  ' "$f" 2>/dev/null
done || true)

if [ -n "$large_js" ]; then
  echo "  [WARN] Large functions detected (consider refactoring):"
  echo "$large_js" | head -5
fi

# ---- Large files ----
echo "  Scanning for oversized files..."
large_files=$(find "$PROJECT_PATH" \( -name "*.js" -o -name "*.ts" -o -name "*.py" \
  -o -name "*.java" -o -name "*.go" \) \
  -not -path "*/node_modules/*" -not -path "*/dist/*" \
  -not -path "*/.git/*" -not -name "*.min.js" 2>/dev/null | while read -r f; do
  lines=$(wc -l < "$f" 2>/dev/null || echo 0)
  if [ "$lines" -gt "$MAX_FILE_LINES" ]; then
    echo "  $f ($lines lines)"
  fi
done || true)

if [ -n "$large_files" ]; then
  echo "  [WARN] Large files (consider splitting):"
  echo "$large_files" | head -10
fi

# ---- Duplicate code detection (if jscpd available) ----
if command -v jscpd &>/dev/null; then
  echo "  Running duplication check (jscpd)..."
  jscpd "$PROJECT_PATH" \
    --ignore "**/.git/**,**/node_modules/**,**/dist/**,**/.policy-reports/**" \
    --reporters json \
    --output "$PROJECT_PATH/.policy-reports/" \
    --min-lines 10 --threshold 5 \
    --silent 2>/dev/null || {
    echo "  [WARN] Code duplication above 5% threshold detected."
  }
fi

# ---- Python complexity (if radon available) ----
if command -v radon &>/dev/null; then
  echo "  [Python] Checking cyclomatic complexity..."
  radon cc "$PROJECT_PATH" -n C --json \
    > "$PROJECT_PATH/.policy-reports/radon_cc.json" 2>/dev/null || true
  complex_funcs=$(python3 -c "
import json, os
data = json.load(open('$PROJECT_PATH/.policy-reports/radon_cc.json'))
count = sum(1 for f in data.values() for fn in f if fn.get('complexity', 0) > 10)
print(count)
" 2>/dev/null || echo "0")
  if [ "$complex_funcs" -gt 0 ]; then
    echo "  [WARN] $complex_funcs function(s) with complexity > 10 (C or worse). Review recommended."
  fi
fi

echo "  Complexity check complete."
exit 0  # Warnings only — don't hard fail
