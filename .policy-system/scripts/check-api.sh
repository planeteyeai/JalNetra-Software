#!/usr/bin/env bash
# Rule 10 — API Design: HTTP methods, status codes, validation, versioning
PROJECT_PATH="${1:-$(pwd)}"
FOUND=0

echo "  Checking API design compliance..."

EXCLUDE="node_modules\|.git\|dist\|build\|vendor\|.policy-reports\|test\|spec"

# ---- Check for API versioning ----
echo "  Checking API versioning..."
has_routes=$(find "$PROJECT_PATH" \( -name "routes*.js" -o -name "routes*.ts" \
  -o -name "*router*.js" -o -name "*router*.ts" \) \
  -not -path "*/node_modules/*" 2>/dev/null | wc -l)

if [ "$has_routes" -gt 0 ]; then
  versioned=$(grep -rniE '/(v[0-9]+|api/v[0-9]+)/' "$PROJECT_PATH" \
    --include="*.js" --include="*.ts" 2>/dev/null \
    | grep -vE "$EXCLUDE" | wc -l || echo "0")

  if [ "$versioned" -eq 0 ]; then
    echo "  [WARN] No versioned API routes found (e.g., /api/v1/...). Consider adding versioning."
  else
    echo "  API versioning found."
  fi
fi

# ---- Check for improper HTTP status codes ----
echo "  Checking HTTP status code usage..."

bad_status=$(grep -rniE \
  'res\.(status|sendStatus)\s*\(\s*(1|600|700)\d{2}\s*\)|response\.status\s*\(\s*(1|600)\d{2}\s*\)' \
  "$PROJECT_PATH" --include="*.js" --include="*.ts" 2>/dev/null \
  | grep -vE "$EXCLUDE" || true)

if [ -n "$bad_status" ]; then
  echo "  [WARN] Potentially invalid HTTP status codes:"
  echo "$bad_status" | head -5
fi

# ---- Check for 200 on errors ----
ok_on_error=$(grep -rniEA3 'catch\s*\([^)]*\)' "$PROJECT_PATH" \
  --include="*.js" --include="*.ts" 2>/dev/null \
  | grep -E 'status\s*\(\s*200\s*\)' | grep -vE "$EXCLUDE" || true)

if [ -n "$ok_on_error" ]; then
  echo "  [FAIL] Returning HTTP 200 in a catch/error block:"
  echo "$ok_on_error" | head -5
  FOUND=1
fi

# ---- OpenAPI spec check (if exists) ----
openapi_files=$(find "$PROJECT_PATH" \( -name "openapi*.yml" -o -name "openapi*.yaml" \
  -o -name "swagger*.yml" -o -name "swagger*.yaml" \) \
  -not -path "*/node_modules/*" 2>/dev/null)

if [ -n "$openapi_files" ]; then
  echo "  OpenAPI spec found."
  if command -v spectral &>/dev/null; then
    echo "$openapi_files" | while read -r spec; do
      spectral lint "$spec" --format json \
        > "$PROJECT_PATH/.policy-reports/spectral.json" 2>/dev/null || {
        echo "  [WARN] OpenAPI spec lint issues found."
      }
    done
  fi
else
  echo "  [WARN] No OpenAPI/Swagger spec found. Consider documenting your API."
fi

[ $FOUND -eq 0 ] && echo "  API checks passed." && exit 0
exit 1
