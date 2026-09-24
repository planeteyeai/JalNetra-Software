#!/usr/bin/env bash
# Rule 8 — Database: no raw SQL string concatenation, use parameterized queries
PROJECT_PATH="${1:-$(pwd)}"
FOUND=0

echo "  Checking database query safety..."

EXCLUDE="node_modules\|.git\|dist\|build\|vendor\|__pycache__\|.policy-reports\|test\|spec\|migration"

# ---- String-concatenated SQL ----
echo "  Scanning for raw SQL string concatenation..."

concat_sql=$(grep -rniE \
  '(SELECT|INSERT|UPDATE|DELETE|WHERE)\s+.*["\x27]\s*\+\s*|`(SELECT|INSERT|UPDATE|DELETE).*\$\{' \
  "$PROJECT_PATH" \
  --include="*.js" --include="*.ts" --include="*.py" \
  --include="*.java" --include="*.php" --include="*.rb" \
  --include="*.go" 2>/dev/null | grep -vE "$EXCLUDE" || true)

if [ -n "$concat_sql" ]; then
  echo "  [FAIL] Raw SQL string concatenation detected (SQL injection risk):"
  echo "$concat_sql" | head -10
  FOUND=1
fi

# ---- Python: cursor.execute with % formatting ----
py_fmt_sql=$(grep -rniE 'cursor\.execute\s*\(\s*["\x27].*%\s*(' \
  "$PROJECT_PATH" --include="*.py" 2>/dev/null \
  | grep -vE "$EXCLUDE" || true)

if [ -n "$py_fmt_sql" ]; then
  echo "  [FAIL] Python: % string formatting in SQL queries (use parameterized queries):"
  echo "$py_fmt_sql" | head -5
  FOUND=1
fi

# ---- Check for missing migration files if ORM is used ----
echo "  Checking for migration files..."
if [ -f "package.json" ]; then
  has_orm=$(grep -E '"(sequelize|typeorm|prisma|knex|mongoose)"' package.json 2>/dev/null || true)
  if [ -n "$has_orm" ]; then
    migration_count=$(find "$PROJECT_PATH" \( \
      -path "*/migrations/*.js" -o -path "*/migrations/*.ts" -o \
      -name "*.migration.ts" -o -name "*.migration.js" \) \
      -not -path "*/.git/*" 2>/dev/null | wc -l)
    if [ "$migration_count" -eq 0 ]; then
      echo "  [WARN] ORM detected but no migration files found. Ensure schema changes use migrations."
    else
      echo "  Found $migration_count migration file(s)."
    fi
  fi
fi

[ $FOUND -eq 0 ] && echo "  Database checks passed." && exit 0
exit 1
