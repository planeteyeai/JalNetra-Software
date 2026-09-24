#!/usr/bin/env bash
# Rule 5 & 21 — Security / OWASP SAST
# Checks for injection, missing auth, insecure defaults
PROJECT_PATH="${1:-$(pwd)}"
FOUND=0

echo "  Running SAST security checks..."

EXCLUDE="node_modules|.git|dist|build|vendor|__pycache__|.policy-reports"

# ---- Injection checks ----
echo "  [A05] Checking for injection vulnerabilities..."

# SQL string concatenation
sql_issues=$(grep -rniE \
  '(execute|query|raw)\s*\(\s*["\x27].*\+|"SELECT.*"\s*\+|"INSERT.*"\s*\+|"UPDATE.*"\s*\+|"DELETE.*"\s*\+' \
  "$PROJECT_PATH" --include="*.js" --include="*.ts" --include="*.py" \
  --include="*.java" --include="*.php" --include="*.rb" 2>/dev/null \
  | grep -vE "$EXCLUDE" || true)

if [ -n "$sql_issues" ]; then
  echo "  [FAIL] Potential SQL injection (string-concatenated queries):"
  echo "$sql_issues" | head -10
  FOUND=1
fi

# eval() usage
eval_issues=$(grep -rniE '\beval\s*\(' "$PROJECT_PATH" \
  --include="*.js" --include="*.ts" --include="*.py" 2>/dev/null \
  | grep -vE "$EXCLUDE|test|spec|\.min\." || true)

if [ -n "$eval_issues" ]; then
  echo "  [FAIL] Dangerous eval() usage:"
  echo "$eval_issues" | head -5
  FOUND=1
fi

# ---- Broken Access Control (A01) ----
echo "  [A01] Checking for missing authorization patterns..."

# Routes without any auth middleware (JS/TS common patterns)
unauth_routes=$(grep -rniE \
  'app\.(get|post|put|delete|patch)\s*\(\s*["\x27][^"\x27]+["\x27]\s*,\s*(async\s*)?\(req' \
  "$PROJECT_PATH" --include="*.js" --include="*.ts" 2>/dev/null \
  | grep -vE "$EXCLUDE|auth|middleware|login|register|health|public" || true)

if [ -n "$unauth_routes" ]; then
  echo "  [WARN] Routes potentially missing auth middleware (manual review needed):"
  echo "$unauth_routes" | head -5
  # Warn only — can't determine intent automatically
fi

# ---- Cryptographic Failures (A04) ----
echo "  [A04] Checking for weak cryptography..."

weak_crypto=$(grep -rniE \
  '\b(md5|sha1|DES|RC4)\s*\(|hashlib\.(md5|sha1)|MessageDigest\.getInstance\s*\(\s*"(MD5|SHA-1)' \
  "$PROJECT_PATH" --include="*.js" --include="*.ts" --include="*.py" \
  --include="*.java" 2>/dev/null | grep -vE "$EXCLUDE" || true)

if [ -n "$weak_crypto" ]; then
  echo "  [FAIL] Weak cryptographic algorithm detected:"
  echo "$weak_crypto" | head -5
  FOUND=1
fi

# ---- Insecure defaults ----
echo "  [A06] Checking for insecure defaults..."

insecure=$(grep -rniE \
  'ssl\s*[:=]\s*(false|False|FALSE)|verify\s*[:=]\s*(false|False|FALSE)|rejectUnauthorized\s*:\s*false|NODE_TLS_REJECT_UNAUTHORIZED\s*=\s*["\x27]0["\x27]' \
  "$PROJECT_PATH" --include="*.js" --include="*.ts" --include="*.py" \
  --include="*.yml" --include="*.yaml" 2>/dev/null \
  | grep -vE "$EXCLUDE|test|spec" || true)

if [ -n "$insecure" ]; then
  echo "  [FAIL] Insecure TLS/SSL defaults:"
  echo "$insecure" | head -5
  FOUND=1
fi

# ---- Semgrep (if available) ----
if command -v semgrep &>/dev/null; then
  echo "  Running Semgrep OWASP ruleset..."
  semgrep --config "p/owasp-top-ten" "$PROJECT_PATH" \
    --json --output "${PROJECT_PATH}/.policy-reports/semgrep.json" \
    --quiet 2>/dev/null || true
  echo "  Semgrep results saved to .policy-reports/semgrep.json"
fi

[ $FOUND -eq 0 ] && echo "  No critical SAST issues found." && exit 0
exit 1
