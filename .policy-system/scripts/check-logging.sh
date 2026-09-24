#!/usr/bin/env bash
# Rule 12 — Never log passwords, tokens, API keys, or PII
PROJECT_PATH="${1:-$(pwd)}"
FOUND=0

echo "  Checking logging safety..."

EXCLUDE="node_modules\|.git\|dist\|build\|vendor\|__pycache__\|.policy-reports"

# ---- Detect logging of sensitive values ----
echo "  Scanning for sensitive data in log statements..."

sensitive_log=$(grep -rniE \
  '(console\.(log|error|warn|info)|logger\.(info|error|warn|debug)|print|logging\.(info|error|warning))\s*\(.*\b(password|passwd|secret|token|api_key|apikey|authorization|credit_card|ssn|social_security)\b' \
  "$PROJECT_PATH" \
  --include="*.js" --include="*.ts" --include="*.py" \
  --include="*.java" --include="*.go" --include="*.rb" \
  2>/dev/null | grep -vE "$EXCLUDE" || true)

if [ -n "$sensitive_log" ]; then
  echo "  [FAIL] Potentially logging sensitive data:"
  echo "$sensitive_log" | head -10
  FOUND=1
fi

# ---- Check logging of request bodies wholesale (may contain PII) ----
req_body_log=$(grep -rniE \
  '(console\.(log|info)|logger\.(info|debug))\s*\(.*req\.(body|params|query)' \
  "$PROJECT_PATH" --include="*.js" --include="*.ts" 2>/dev/null \
  | grep -vE "$EXCLUDE" || true)

if [ -n "$req_body_log" ]; then
  echo "  [WARN] Logging entire request body/params (may contain PII):"
  echo "$req_body_log" | head -5
fi

# ---- Check for logging framework being used (vs raw console.log in prod code) ----
if find "$PROJECT_PATH" -name "*.js" -o -name "*.ts" 2>/dev/null \
   | grep -vE "node_modules|dist|test|spec" | grep -q .; then
  console_log_count=$(grep -rniE 'console\.(log|info|warn|error)' "$PROJECT_PATH" \
    --include="*.js" --include="*.ts" \
    --exclude-dir=node_modules --exclude-dir=dist \
    2>/dev/null | grep -vE "test|spec|__tests__|.policy-reports" | wc -l || echo "0")

  if [ "$console_log_count" -gt 20 ]; then
    echo "  [WARN] High console.log usage ($console_log_count occurrences). Consider a structured logger."
  fi
fi

[ $FOUND -eq 0 ] && echo "  Logging checks passed." && exit 0
exit 1
