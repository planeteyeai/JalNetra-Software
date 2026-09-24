#!/usr/bin/env bash
# Rule 13 — Test coverage enforcement
PROJECT_PATH="${1:-$(pwd)}"
MIN_COVERAGE=70  # minimum % threshold
FOUND=0

echo "  Checking test coverage..."
cd "$PROJECT_PATH" || exit 1

# ---- Node.js / Jest ----
if [ -f "package.json" ] && grep -q '"jest"' package.json 2>/dev/null; then
  echo "  [Node.js] Running Jest with coverage..."
  npx jest --coverage --coverageReporters=json-summary --passWithNoTests \
    --forceExit 2>/dev/null | tail -5 || true

  if [ -f "coverage/coverage-summary.json" ]; then
    cp coverage/coverage-summary.json "$PROJECT_PATH/.policy-reports/coverage.json"
    LINES=$(python3 -c "
import json
d = json.load(open('coverage/coverage-summary.json'))
t = d.get('total', {}).get('lines', {})
print(int(t.get('pct', 0)))
" 2>/dev/null || echo "0")
    echo "  Line coverage: ${LINES}%"
    if [ "$LINES" -lt "$MIN_COVERAGE" ]; then
      echo "  [FAIL] Coverage ${LINES}% is below minimum ${MIN_COVERAGE}%"
      FOUND=1
    else
      echo "  [PASS] Coverage ${LINES}% meets threshold."
    fi
  fi
fi

# ---- Python / pytest ----
if [ -f "pytest.ini" ] || [ -f "setup.cfg" ] || [ -f "pyproject.toml" ] || \
   find "$PROJECT_PATH" -name "test_*.py" -maxdepth 4 2>/dev/null | grep -q .; then
  if command -v pytest &>/dev/null; then
    echo "  [Python] Running pytest with coverage..."
    pytest --cov="$PROJECT_PATH" --cov-report=json \
      --cov-fail-under="$MIN_COVERAGE" -q 2>/dev/null
    COVERAGE_EXIT=$?
    cp coverage.json "$PROJECT_PATH/.policy-reports/py_coverage.json" 2>/dev/null || true
    if [ $COVERAGE_EXIT -ne 0 ]; then
      echo "  [FAIL] Python coverage below ${MIN_COVERAGE}%"
      FOUND=1
    fi
  else
    echo "  [WARN] pytest not installed."
    exit 2
  fi
fi

# Check test files exist at all
test_count=$(find "$PROJECT_PATH" \( \
  -name "*.test.js" -o -name "*.test.ts" -o -name "*.spec.js" -o \
  -name "*.spec.ts" -o -name "test_*.py" -o -name "*_test.go" \) \
  -not -path "*/node_modules/*" -not -path "*/.git/*" 2>/dev/null | wc -l)

if [ "$test_count" -eq 0 ]; then
  echo "  [FAIL] No test files found. Rule 13 requires tests for every feature."
  FOUND=1
else
  echo "  Found $test_count test file(s)."
fi

[ $FOUND -eq 0 ] && exit 0 || exit 1
