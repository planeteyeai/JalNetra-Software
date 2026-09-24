#!/usr/bin/env bash
# Rule 20 — AI Hallucination: verify packages actually exist in registries
PROJECT_PATH="${1:-$(pwd)}"
FOUND=0

echo "  Checking AI dependency hallucination risks..."
cd "$PROJECT_PATH" || exit 1

# ---- npm: verify packages exist ----
if [ -f "package.json" ] && command -v node &>/dev/null; then
  echo "  [npm] Verifying packages exist in registry..."
  SUSPICIOUS=()

  # Read dependencies from package.json
  deps=$(node -e "
    const p = require('./package.json');
    const all = { ...p.dependencies, ...p.devDependencies };
    console.log(Object.keys(all).join('\n'));
  " 2>/dev/null || true)

  checked=0
  suspicious=0

  while IFS= read -r pkg; do
    [ -z "$pkg" ] && continue
    # Check if package exists on npm
    status=$(curl -s -o /dev/null -w "%{http_code}" \
      "https://registry.npmjs.org/$pkg" --max-time 5 2>/dev/null || echo "000")

    if [ "$status" = "404" ]; then
      echo "  [FAIL] Package '$pkg' not found in npm registry — possible hallucination!"
      SUSPICIOUS+=("$pkg")
      suspicious=$((suspicious + 1))
      FOUND=1
    elif [ "$status" = "000" ]; then
      echo "  [SKIP] Could not reach npm registry (network issue) for: $pkg"
    fi
    checked=$((checked + 1))
  done <<< "$deps"

  echo "  Checked $checked npm packages. Suspicious: $suspicious"
fi

# ---- Python: verify packages on PyPI ----
if [ -f "requirements.txt" ]; then
  echo "  [PyPI] Verifying packages exist in PyPI..."
  suspicious=0
  checked=0

  while IFS= read -r line; do
    # Skip comments and empty lines
    [[ "$line" =~ ^#.*$ || -z "$line" ]] && continue
    # Extract package name (before ==, >=, etc.)
    pkg=$(echo "$line" | sed 's/[>=<!].*//' | tr '[:upper:]' '[:lower:]' | xargs)
    [ -z "$pkg" ] && continue

    status=$(curl -s -o /dev/null -w "%{http_code}" \
      "https://pypi.org/pypi/$pkg/json" --max-time 5 2>/dev/null || echo "000")

    if [ "$status" = "404" ]; then
      echo "  [FAIL] Python package '$pkg' not found on PyPI — possible hallucination!"
      suspicious=$((suspicious + 1))
      FOUND=1
    fi
    checked=$((checked + 1))
  done < requirements.txt

  echo "  Checked $checked PyPI packages. Suspicious: $suspicious"
fi

# ---- Check for known slopsquatting patterns ----
echo "  Checking for typosquatting/slopsquatting patterns..."
if [ -f "package.json" ]; then
  # Common typosquatted packages
  KNOWN_SQUATS=("lo-dash" "loadash" "expres" "reqeuest" "axois" "mongoos" "expresjs")
  for squatted in "${KNOWN_SQUATS[@]}"; do
    if grep -q "\"$squatted\"" package.json 2>/dev/null; then
      echo "  [FAIL] Suspicious package name '$squatted' (possible typosquat of a popular package)"
      FOUND=1
    fi
  done
fi

[ $FOUND -eq 0 ] && echo "  AI dependency checks passed." && exit 0
exit 1
