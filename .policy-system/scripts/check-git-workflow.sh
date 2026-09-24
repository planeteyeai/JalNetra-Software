#!/usr/bin/env bash
# Rule 15 — Git workflow: branches, commit messages, protected main
PROJECT_PATH="${1:-$(pwd)}"
FOUND=0

echo "  Checking Git workflow compliance..."
cd "$PROJECT_PATH" || exit 1

if [ ! -d ".git" ]; then
  echo "  [SKIP] Not a git repository."
  exit 3
fi

# ---- Check recent commit messages follow convention ----
echo "  Checking commit message format..."
BAD_COMMITS=$(git log --oneline -20 2>/dev/null | grep -vE \
  '^[a-f0-9]+ (feat|fix|docs|style|refactor|test|chore|ci|build|perf|revert)(\(.+\))?: .{1,}' \
  || true)

if [ -n "$BAD_COMMITS" ]; then
  echo "  [WARN] Some commits don't follow Conventional Commits format:"
  echo "$BAD_COMMITS" | head -5
  echo "  Expected: feat|fix|docs|style|refactor|test|chore: description"
  # Warn only — don't hard fail on existing history
fi

# ---- Check for direct commits to main/master ----
echo "  Checking for direct commits to main branch..."
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
if [ "$CURRENT_BRANCH" = "main" ] || [ "$CURRENT_BRANCH" = "master" ]; then
  echo "  [WARN] Currently on protected branch ($CURRENT_BRANCH). Changes should go via PR."
fi

# ---- Check .gitignore exists ----
echo "  Checking .gitignore..."
if [ ! -f ".gitignore" ]; then
  echo "  [FAIL] No .gitignore found. Secrets and build artifacts may be committed."
  FOUND=1
else
  # Check common sensitive entries
  for entry in ".env" "*.key" "*.pem" "node_modules" "__pycache__" ".DS_Store"; do
    if ! grep -q "$entry" .gitignore 2>/dev/null; then
      echo "  [WARN] .gitignore missing entry: $entry"
    fi
  done
fi

# ---- Check branch naming convention ----
if [ "$CURRENT_BRANCH" != "main" ] && [ "$CURRENT_BRANCH" != "master" ] && \
   [ "$CURRENT_BRANCH" != "HEAD" ]; then
  if ! echo "$CURRENT_BRANCH" | grep -qE '^(feature|fix|hotfix|chore|release|docs)/'; then
    echo "  [WARN] Branch '$CURRENT_BRANCH' doesn't follow naming convention."
    echo "         Expected: feature/*, fix/*, hotfix/*, chore/*, release/*, docs/*"
  fi
fi

# ---- Check for PR template ----
echo "  Checking for PR template..."
if [ ! -f ".github/PULL_REQUEST_TEMPLATE.md" ] && \
   [ ! -f ".github/pull_request_template.md" ] && \
   [ ! -f "docs/pull_request_template.md" ]; then
  echo "  [WARN] No PR template found. Run install.sh to add one."
fi

[ $FOUND -eq 0 ] && echo "  Git workflow checks passed." && exit 0
exit 1
