#!/usr/bin/env bash
# Rule 3 — Clean Code: formatting and linting
PROJECT_PATH="${1:-$(pwd)}"
FOUND=0

echo "  Checking code formatting and linting..."
cd "$PROJECT_PATH" || exit 1

# ---- JavaScript / TypeScript ----
if [ -f "package.json" ]; then
  # ESLint
  if command -v npx &>/dev/null && npx eslint --version &>/dev/null 2>&1; then
    echo "  [JS/TS] Running ESLint..."
    npx eslint . --max-warnings=0 --format json \
      --output-file "$PROJECT_PATH/.policy-reports/eslint.json" 2>/dev/null
    if [ $? -ne 0 ]; then
      echo "  [FAIL] ESLint found issues. See .policy-reports/eslint.json"
      FOUND=1
    else
      echo "  [JS/TS] ESLint passed."
    fi
  fi

  # Prettier
  if command -v npx &>/dev/null && npx prettier --version &>/dev/null 2>&1; then
    echo "  [JS/TS] Checking Prettier formatting..."
    if ! npx prettier --check . 2>/dev/null; then
      echo "  [WARN] Prettier formatting issues found. Run: npx prettier --write ."
    fi
  fi
fi

# ---- Python ----
if find "$PROJECT_PATH" -name "*.py" -not -path "*/node_modules/*" \
   -not -path "*/__pycache__/*" 2>/dev/null | grep -q .; then

  if command -v ruff &>/dev/null; then
    echo "  [Python] Running Ruff linter..."
    ruff check "$PROJECT_PATH" --output-format json \
      > "$PROJECT_PATH/.policy-reports/ruff.json" 2>/dev/null
    if [ $? -ne 0 ]; then
      echo "  [FAIL] Ruff found linting issues. See .policy-reports/ruff.json"
      FOUND=1
    fi
  elif command -v flake8 &>/dev/null; then
    echo "  [Python] Running flake8..."
    flake8 "$PROJECT_PATH" --max-line-length=100 \
      --output-file="$PROJECT_PATH/.policy-reports/flake8.txt" 2>/dev/null || {
      echo "  [FAIL] flake8 found issues."
      FOUND=1
    }
  fi
fi

# ---- Go ----
if [ -f "go.mod" ]; then
  if command -v gofmt &>/dev/null; then
    echo "  [Go] Checking gofmt..."
    unformatted=$(gofmt -l "$PROJECT_PATH" 2>/dev/null || true)
    if [ -n "$unformatted" ]; then
      echo "  [FAIL] Unformatted Go files:"
      echo "$unformatted"
      FOUND=1
    fi
  fi
fi

[ $FOUND -eq 0 ] && echo "  Formatting checks passed." && exit 0
exit 1
