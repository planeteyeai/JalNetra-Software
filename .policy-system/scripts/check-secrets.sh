#!/usr/bin/env bash
# Rule 6 — Never hard-code secrets
# Scans for API keys, passwords, tokens in source code
PROJECT_PATH="${1:-$(pwd)}"
FOUND=0

echo "  Scanning for hardcoded secrets..."

# Pattern-based secret detection (no gitleaks dependency required)
PATTERNS=(
  'password\s*=\s*["\x27][^"\x27]{4,}'
  'secret\s*=\s*["\x27][^"\x27]{4,}'
  'api_key\s*=\s*["\x27][^"\x27]{4,}'
  'apikey\s*=\s*["\x27][^"\x27]{4,}'
  'token\s*=\s*["\x27][^"\x27]{4,}'
  'private_key\s*=\s*["\x27][^"\x27]{4,}'
  'aws_secret_access_key\s*='
  'AKIA[0-9A-Z]{16}'
  '-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY-----'
  'Authorization:\s*Bearer\s+[A-Za-z0-9\-_]{20,}'
)

EXCLUDE_DIRS="node_modules|.git|dist|build|vendor|__pycache__|.policy-reports"
EXCLUDE_FILES="*.min.js|*.lock|package-lock.json|yarn.lock|*.pdf|*.png|*.jpg"

for pattern in "${PATTERNS[@]}"; do
  matches=$(grep -rniE "$pattern" "$PROJECT_PATH" \
    --include="*.js" --include="*.ts" --include="*.py" \
    --include="*.java" --include="*.go" --include="*.rb" \
    --include="*.php" --include="*.env.example" --include="*.yml" \
    --include="*.yaml" --include="*.json" --include="*.sh" \
    2>/dev/null | grep -vE "$EXCLUDE_DIRS" | grep -v ".policy-reports" || true)

  if [ -n "$matches" ]; then
    echo "  [FAIL] Potential secret found:"
    echo "$matches" | while IFS= read -r line; do
      echo "         $line"
    done
    FOUND=1
  fi
done

# Check for .env files accidentally committed
env_files=$(find "$PROJECT_PATH" -name ".env" -not -path "*/.git/*" \
  -not -path "*/node_modules/*" -not -path "*/.policy-reports/*" 2>/dev/null || true)

if [ -n "$env_files" ]; then
  echo "  [WARN] .env file(s) found in repo — ensure they are in .gitignore:"
  echo "$env_files"
  exit 2
fi

[ $FOUND -eq 0 ] && echo "  No hardcoded secrets detected." && exit 0
exit 1
