#!/usr/bin/env bash
# Rule 25 — AI Attribution in PRs
PROJECT_PATH="${1:-$(pwd)}"
FOUND=0

echo "  Checking PR template and AI attribution..."

PR_TEMPLATE_PATHS=(
  "$PROJECT_PATH/.github/PULL_REQUEST_TEMPLATE.md"
  "$PROJECT_PATH/.github/pull_request_template.md"
  "$PROJECT_PATH/docs/pull_request_template.md"
  "$PROJECT_PATH/PULL_REQUEST_TEMPLATE.md"
)

template_found=false
for path in "${PR_TEMPLATE_PATHS[@]}"; do
  if [ -f "$path" ]; then
    echo "  PR template found: $path"
    template_found=true

    # Check it contains AI attribution section
    if grep -qi "ai\|artificial intelligence\|vibe cod\|ai-generated\|ai-assisted" "$path" 2>/dev/null; then
      echo "  AI attribution section present in PR template."
    else
      echo "  [WARN] PR template missing AI attribution section (Rule 25)."
      echo "         Add a checkbox: '- [ ] This PR contains AI-generated code'"
    fi
    break
  fi
done

if [ "$template_found" = false ]; then
  echo "  [WARN] No PR template found. Run install.sh to add one."
  echo "         This means Rule 25 (AI attribution) cannot be enforced at PR creation."
fi

# ---- Check commitlint config ----
if [ -f "$PROJECT_PATH/commitlint.config.js" ] || \
   [ -f "$PROJECT_PATH/.commitlintrc.json" ] || \
   ([ -f "$PROJECT_PATH/package.json" ] && \
    grep -q '"commitlint"' "$PROJECT_PATH/package.json" 2>/dev/null); then
  echo "  Commitlint config found."
else
  echo "  [WARN] No commitlint config found. Conventional commit messages not enforced."
fi

exit 0  # Warnings only for this rule
