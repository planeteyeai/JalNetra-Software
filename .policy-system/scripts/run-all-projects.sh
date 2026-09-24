#!/usr/bin/env bash
# ============================================================
# Multi-Project Policy Runner
# Runs checks across all registered projects and generates
# a combined dashboard report.
#
# Usage:
#   ./scripts/run-all-projects.sh                  # uses projects.conf
#   ./scripts/run-all-projects.sh /path/a /path/b  # explicit paths
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POLICY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECTS_CONF="${POLICY_DIR}/projects.conf"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
COMBINED_DIR="${POLICY_DIR}/reports/combined_${TIMESTAMP}"
COMBINED_JSON="${COMBINED_DIR}/all_projects.json"

mkdir -p "$COMBINED_DIR"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'

echo -e "${CYAN}"
echo "╔══════════════════════════════════════════════════════╗"
echo "║      MULTI-PROJECT POLICY COMPLIANCE RUNNER          ║"
echo "╚══════════════════════════════════════════════════════╝"
echo -e "${NC}"
echo "Combined report: $COMBINED_DIR"
echo ""

# ---- Collect project list ----
PROJECTS=()

if [ $# -gt 0 ]; then
  # Passed as arguments
  for p in "$@"; do
    PROJECTS+=("$p")
  done
elif [ -f "$PROJECTS_CONF" ]; then
  # Read from config file
  while IFS= read -r line; do
    [[ "$line" =~ ^#.*$ || -z "$line" ]] && continue
    PROJECTS+=("$line")
  done < "$PROJECTS_CONF"
else
  echo -e "${YELLOW}No projects.conf found and no paths given.${NC}"
  echo "Create $PROJECTS_CONF with one project path per line, or pass paths as arguments."
  echo ""
  echo "Example projects.conf:"
  echo "  /home/user/my-api"
  echo "  /home/user/frontend-app"
  echo "  /home/user/mobile-app"
  exit 1
fi

echo "Projects to scan: ${#PROJECTS[@]}"
echo ""

# ---- Run checks for each project ----
declare -a ALL_RESULTS
TOTAL_PASS=0; TOTAL_FAIL=0; TOTAL_WARN=0; TOTAL_SKIP=0

for project_path in "${PROJECTS[@]}"; do
  project_name=$(basename "$project_path")

  if [ ! -d "$project_path" ]; then
    echo -e "${RED}[SKIP] Directory not found: $project_path${NC}"
    continue
  fi

  echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${BLUE}Project: $project_name${NC}"
  echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

  # Run checks (non-fatal even if project fails)
  bash "$POLICY_DIR/scripts/run-checks.sh" "$project_name" "$project_path" || true

  # Find the latest results file for this project
  latest_result=$(ls -t "${project_path}/.policy-reports/results_"*.json 2>/dev/null | head -1 || echo "")

  if [ -n "$latest_result" ]; then
    # Copy to combined dir
    cp "$latest_result" "${COMBINED_DIR}/${project_name}.json"
    # Copy HTML report
    latest_html=$(ls -t "${project_path}/.policy-reports/report_"*.html 2>/dev/null | head -1 || echo "")
    [ -n "$latest_html" ] && cp "$latest_html" "${COMBINED_DIR}/${project_name}.html"

    # Accumulate totals
    p=$(python3 -c "import json; d=json.load(open('$latest_result')); print(d['summary']['pass'])" 2>/dev/null || echo 0)
    f=$(python3 -c "import json; d=json.load(open('$latest_result')); print(d['summary']['fail'])" 2>/dev/null || echo 0)
    w=$(python3 -c "import json; d=json.load(open('$latest_result')); print(d['summary']['warn'])" 2>/dev/null || echo 0)
    s=$(python3 -c "import json; d=json.load(open('$latest_result')); print(d['summary']['skip'])" 2>/dev/null || echo 0)
    TOTAL_PASS=$((TOTAL_PASS + p))
    TOTAL_FAIL=$((TOTAL_FAIL + f))
    TOTAL_WARN=$((TOTAL_WARN + w))
    TOTAL_SKIP=$((TOTAL_SKIP + s))
    ALL_RESULTS+=("$latest_result")
  fi
  echo ""
done

# ---- Generate combined JSON ----
python3 - "${COMBINED_DIR}" "${ALL_RESULTS[@]}" <<'PYEOF'
import json, sys, os, glob
from datetime import datetime, timezone

combined_dir = sys.argv[1]
result_files = sys.argv[2:]

projects = []
for f in result_files:
    try:
        data = json.load(open(f))
        s = data['summary']
        score = round(s['pass'] / max(s['pass'] + s['fail'] + s['warn'], 1) * 100)
        data['score'] = score
        projects.append(data)
    except Exception as e:
        print(f"Could not parse {f}: {e}")

total = {
    'pass': sum(p['summary']['pass'] for p in projects),
    'fail': sum(p['summary']['fail'] for p in projects),
    'warn': sum(p['summary']['warn'] for p in projects),
    'skip': sum(p['summary']['skip'] for p in projects),
}
total['total'] = total['pass'] + total['fail'] + total['warn'] + total['skip']
overall_score = round(total['pass'] / max(total['pass'] + total['fail'] + total['warn'], 1) * 100)

combined = {
    'generated_at': datetime.now(timezone.utc).isoformat(),
    'policy_version': '1.2',
    'overall_score': overall_score,
    'total_projects': len(projects),
    'summary': total,
    'projects': sorted(projects, key=lambda x: x.get('score', 0))
}

out = os.path.join(combined_dir, 'all_projects.json')
json.dump(combined, open(out, 'w'), indent=2)
print(f"Combined JSON: {out}")
PYEOF

# ---- Generate combined HTML dashboard ----
bash "$POLICY_DIR/scripts/generate-dashboard.sh" "$COMBINED_DIR" "$COMBINED_JSON"

# ---- Final summary ----
OVERALL_SCORE=$(python3 -c "
import json
d = json.load(open('$COMBINED_JSON'))
print(d['overall_score'])
" 2>/dev/null || echo 0)

echo ""
echo -e "${CYAN}══════════════════════════════════════════════════════${NC}"
echo -e "  MULTI-PROJECT SUMMARY"
echo -e "${CYAN}══════════════════════════════════════════════════════${NC}"
echo -e "  Projects scanned: ${#PROJECTS[@]}"
echo -e "  Overall score:    ${OVERALL_SCORE}%"
echo -e "  ${GREEN}PASS${NC} $TOTAL_PASS  ${RED}FAIL${NC} $TOTAL_FAIL  ${YELLOW}WARN${NC} $TOTAL_WARN  SKIP $TOTAL_SKIP"
echo -e "  Dashboard: ${COMBINED_DIR}/dashboard.html"
echo -e "${CYAN}══════════════════════════════════════════════════════${NC}"

[ $TOTAL_FAIL -eq 0 ] && exit 0 || exit 1
