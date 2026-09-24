#!/usr/bin/env bash
# Rule 11 — Never silently swallow errors
PROJECT_PATH="${1:-$(pwd)}"
FOUND=0

echo "  Checking error handling..."

EXCLUDE="node_modules\|.git\|dist\|build\|vendor\|__pycache__\|.policy-reports"

# ---- Empty catch blocks (JS/TS) ----
empty_catch_js=$(grep -rniEA2 'catch\s*\(\s*\w*\s*\)\s*\{' "$PROJECT_PATH" \
  --include="*.js" --include="*.ts" 2>/dev/null \
  | grep -vE "$EXCLUDE" \
  | grep -B1 '^\s*\}' \
  | grep 'catch' || true)

# Simpler check: catch blocks with only a comment or nothing
silent_catch=$(grep -rniE 'catch\s*\([^)]*\)\s*\{\s*\}|catch\s*\([^)]*\)\s*\{\s*//.*\}' \
  "$PROJECT_PATH" --include="*.js" --include="*.ts" 2>/dev/null \
  | grep -vE "$EXCLUDE" || true)

if [ -n "$silent_catch" ]; then
  echo "  [FAIL] Empty/silent catch blocks found:"
  echo "$silent_catch" | head -5
  FOUND=1
fi

# ---- Python: bare except ----
bare_except=$(grep -rniE '^\s*except\s*:' "$PROJECT_PATH" \
  --include="*.py" 2>/dev/null | grep -vE "$EXCLUDE" || true)

if [ -n "$bare_except" ]; then
  echo "  [FAIL] Bare 'except:' found in Python (catches everything, swallows errors):"
  echo "$bare_except" | head -5
  FOUND=1
fi

# ---- Python: pass in except ----
pass_except=$(grep -rniEA1 'except\s+\w+' "$PROJECT_PATH" \
  --include="*.py" 2>/dev/null | grep -B1 '^\s*pass$' \
  | grep 'except' | grep -vE "$EXCLUDE" || true)

if [ -n "$pass_except" ]; then
  echo "  [WARN] 'except ... pass' found — silently swallowing exceptions:"
  echo "$pass_except" | head -5
fi

# ---- Unhandled promise rejections (JS/TS) ----
unhandled_promise=$(grep -rniE '\.catch\s*\(\s*\)\s*|\.catch\s*\(\s*err\s*=>\s*\{\s*\}\s*\)' \
  "$PROJECT_PATH" --include="*.js" --include="*.ts" 2>/dev/null \
  | grep -vE "$EXCLUDE" || true)

if [ -n "$unhandled_promise" ]; then
  echo "  [WARN] Empty .catch() handlers on promises:"
  echo "$unhandled_promise" | head -5
fi

[ $FOUND -eq 0 ] && echo "  Error handling checks passed." && exit 0
exit 1
