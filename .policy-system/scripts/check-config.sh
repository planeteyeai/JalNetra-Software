#!/usr/bin/env bash
# Rule 18 — Config separation: no hardcoded env-specific values
PROJECT_PATH="${1:-$(pwd)}"
FOUND=0

echo "  Checking configuration separation..."

EXCLUDE="node_modules\|.git\|dist\|build\|vendor\|__pycache__\|.policy-reports\|test\|spec"

# ---- Hardcoded URLs / hostnames ----
hardcoded_urls=$(grep -rniE \
  '(https?://(localhost|127\.0\.0\.1|0\.0\.0\.0):\d+|https?://[a-z0-9.-]+\.(internal|local|corp))' \
  "$PROJECT_PATH" \
  --include="*.js" --include="*.ts" --include="*.py" \
  --include="*.java" --include="*.go" \
  2>/dev/null | grep -vE "$EXCLUDE|comment|README|docs" || true)

if [ -n "$hardcoded_urls" ]; then
  echo "  [WARN] Hardcoded URLs found (should be in config/env vars):"
  echo "$hardcoded_urls" | head -5
fi

# ---- Hardcoded port numbers ----
hardcoded_ports=$(grep -rniE \
  '(port|PORT)\s*[:=]\s*(3000|4000|5000|8000|8080|9000|27017|5432|3306|6379)' \
  "$PROJECT_PATH" \
  --include="*.js" --include="*.ts" --include="*.py" \
  2>/dev/null | grep -vE "$EXCLUDE|process\.env|os\.environ|config\." || true)

if [ -n "$hardcoded_ports" ]; then
  echo "  [WARN] Hardcoded port numbers (use environment variables):"
  echo "$hardcoded_ports" | head -5
fi

# ---- Check for environment config files ----
echo "  Checking environment configuration setup..."
if [ -f ".env.example" ] || [ -f ".env.sample" ] || [ -f ".env.template" ]; then
  echo "  .env template found — good practice."
else
  if find "$PROJECT_PATH" -name "*.js" -o -name "*.ts" -o -name "*.py" \
     2>/dev/null | grep -qE "process\.env|os\.environ" 2>/dev/null; then
    echo "  [WARN] Environment variables used but no .env.example found."
    echo "         Create .env.example with placeholder values for documentation."
  fi
fi

# ---- Check config is not environment-specific in code ----
env_in_code=$(grep -rniE \
  'if\s+.*env.*==?\s*["\x27](production|staging|development)["\x27]' \
  "$PROJECT_PATH" \
  --include="*.js" --include="*.ts" --include="*.py" \
  2>/dev/null | grep -vE "$EXCLUDE" || true)

if [ -n "$env_in_code" ]; then
  echo "  [WARN] Environment-specific logic in code (Rule 18: differ by config, not code):"
  echo "$env_in_code" | head -5
fi

[ $FOUND -eq 0 ] && echo "  Config separation checks passed." && exit 0
exit 1
