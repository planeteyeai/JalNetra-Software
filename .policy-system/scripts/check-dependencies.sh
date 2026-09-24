#!/usr/bin/env bash
# Rule 16 — Vet third-party libraries for vulnerabilities
PROJECT_PATH="${1:-$(pwd)}"
FOUND=0

echo "  Checking dependencies for vulnerabilities..."

cd "$PROJECT_PATH" || exit 1

# ---- Node.js / npm ----
if [ -f "package.json" ] && [ -f "package-lock.json" ]; then
  echo "  [Node.js] Running npm audit..."
  if npm audit --audit-level=high --json > /tmp/npm_audit.json 2>/dev/null; then
    echo "  [Node.js] No high/critical vulnerabilities found."
  else
    HIGH=$(python3 -c "import json,sys; d=json.load(open('/tmp/npm_audit.json')); \
      print(d.get('metadata',{}).get('vulnerabilities',{}).get('high',0))" 2>/dev/null || echo "?")
    CRIT=$(python3 -c "import json,sys; d=json.load(open('/tmp/npm_audit.json')); \
      print(d.get('metadata',{}).get('vulnerabilities',{}).get('critical',0))" 2>/dev/null || echo "?")
    echo "  [FAIL] npm audit: HIGH=$HIGH CRITICAL=$CRIT"
    cp /tmp/npm_audit.json "$PROJECT_PATH/.policy-reports/npm_audit.json" 2>/dev/null || true
    FOUND=1
  fi

  # Check for deprecated packages
  echo "  [Node.js] Checking for outdated packages..."
  npm outdated --json > /tmp/npm_outdated.json 2>/dev/null || true
  outdated_count=$(python3 -c "import json; d=json.load(open('/tmp/npm_outdated.json')); print(len(d))" 2>/dev/null || echo "0")
  if [ "$outdated_count" -gt 0 ] 2>/dev/null; then
    echo "  [WARN] $outdated_count outdated packages found (see .policy-reports/npm_outdated.json)"
    cp /tmp/npm_outdated.json "$PROJECT_PATH/.policy-reports/npm_outdated.json" 2>/dev/null || true
  fi
fi

# ---- Python / pip ----
if [ -f "requirements.txt" ] || [ -f "pyproject.toml" ] || [ -f "Pipfile" ]; then
  echo "  [Python] Checking with pip-audit..."
  if command -v pip-audit &>/dev/null; then
    if pip-audit --output "$PROJECT_PATH/.policy-reports/pip_audit.json" \
        --format json 2>/dev/null; then
      echo "  [Python] No vulnerabilities found."
    else
      echo "  [FAIL] pip-audit found vulnerabilities. See .policy-reports/pip_audit.json"
      FOUND=1
    fi
  elif command -v safety &>/dev/null; then
    echo "  [Python] Running safety check..."
    safety check --json > "$PROJECT_PATH/.policy-reports/safety.json" 2>/dev/null || {
      echo "  [FAIL] Safety check found vulnerabilities."
      FOUND=1
    }
  else
    echo "  [WARN] No Python audit tool found. Install pip-audit: pip install pip-audit"
    exit 2
  fi
fi

# ---- Go ----
if [ -f "go.mod" ]; then
  echo "  [Go] Checking with govulncheck..."
  if command -v govulncheck &>/dev/null; then
    govulncheck ./... 2>&1 | tee "$PROJECT_PATH/.policy-reports/govulncheck.txt" || {
      echo "  [FAIL] govulncheck found vulnerabilities."
      FOUND=1
    }
  else
    echo "  [WARN] govulncheck not installed. Run: go install golang.org/x/vuln/cmd/govulncheck@latest"
    exit 2
  fi
fi

# ---- License check (all ecosystems) ----
if [ -f "package.json" ] && command -v license-checker &>/dev/null; then
  echo "  Checking licenses..."
  license-checker --excludePrivatePackages \
    --failOn "GPL-2.0;GPL-3.0;AGPL-3.0" \
    --json > "$PROJECT_PATH/.policy-reports/licenses.json" 2>/dev/null || {
    echo "  [WARN] Potentially incompatible licenses found. Review .policy-reports/licenses.json"
    exit 2
  }
fi

[ $FOUND -eq 0 ] && echo "  Dependency checks passed." && exit 0
exit 1
