#!/usr/bin/env bash
# ============================================================
# Master Policy Check Runner
# Runs all automated rule checks and collects results
# Usage: ./scripts/run-checks.sh [project_name] [project_path]
# ============================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POLICY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

PROJECT_NAME="${1:-$(basename "$(pwd)")}"
PROJECT_PATH="${2:-$(pwd)}"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
REPORT_DIR="${PROJECT_PATH}/.policy-reports"
RESULTS_FILE="${REPORT_DIR}/results_${TIMESTAMP}.json"

mkdir -p "$REPORT_DIR"

# Colors
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'

echo -e "${CYAN}"
echo "╔══════════════════════════════════════════════════════╗"
echo "║         SOFTWARE DEVELOPMENT POLICY CHECKER          ║"
echo "║                  v1.2 — All 25 Rules                 ║"
echo "╚══════════════════════════════════════════════════════╝"
echo -e "${NC}"
echo -e "${BLUE}Project:${NC} $PROJECT_NAME"
echo -e "${BLUE}Path:${NC}    $PROJECT_PATH"
echo -e "${BLUE}Time:${NC}    $(date)"
echo ""

# ---- Result tracking ----
PASS=0; FAIL=0; WARN=0; SKIP=0
declare -A RULE_RESULTS
declare -A RULE_MESSAGES

run_check() {
  local rule_id="$1"
  local rule_name="$2"
  local check_script="$3"

  echo -e "${BLUE}▶ Checking Rule ${rule_id}: ${rule_name}${NC}"

  # run check, capture exit code without letting -e abort the script
  local exit_code=0
  bash "$POLICY_DIR/scripts/${check_script}" "$PROJECT_PATH" 2>&1 || exit_code=$?

  if [ $exit_code -eq 0 ]; then
    RULE_RESULTS["$rule_id"]="PASS"
    RULE_MESSAGES["$rule_id"]="All checks passed"
    echo -e "  ${GREEN}✔ PASS${NC}"
    ((PASS++)) || true
  elif [ $exit_code -eq 2 ]; then
    RULE_RESULTS["$rule_id"]="WARN"
    RULE_MESSAGES["$rule_id"]="Warning — review recommended"
    echo -e "  ${YELLOW}⚠ WARN${NC}"
    ((WARN++)) || true
  elif [ $exit_code -eq 3 ]; then
    RULE_RESULTS["$rule_id"]="SKIP"
    RULE_MESSAGES["$rule_id"]="Skipped — not applicable"
    echo -e "  ${YELLOW}– SKIP${NC}"
    ((SKIP++)) || true
  else
    RULE_RESULTS["$rule_id"]="FAIL"
    RULE_MESSAGES["$rule_id"]="Violations found — see report"
    echo -e "  ${RED}✘ FAIL${NC}"
    ((FAIL++)) || true
  fi
  echo ""
}

# ---- Run all checks ----
run_check "R03"  "Clean Code / Formatting"        "check-formatting.sh"
run_check "R04"  "DRY & Complexity"               "check-complexity.sh"
run_check "R05"  "Security / OWASP SAST"          "check-sast.sh"
run_check "R06"  "Secrets Detection"              "check-secrets.sh"
run_check "R08"  "Database / SQL Injection"       "check-database.sh"
run_check "R10"  "API Design"                     "check-api.sh"
run_check "R11"  "Error Handling"                 "check-error-handling.sh"
run_check "R12"  "Logging Safety"                 "check-logging.sh"
run_check "R13"  "Test Coverage"                  "check-tests.sh"
run_check "R15"  "Git Workflow"                   "check-git-workflow.sh"
run_check "R16"  "Dependencies"                   "check-dependencies.sh"
run_check "R18"  "Config Separation"              "check-config.sh"
run_check "R20"  "AI Hallucination / Dep Verify"  "check-ai-deps.sh"
run_check "R21"  "SAST for AI Code"               "check-sast.sh"
run_check "R25"  "AI Attribution in PRs"          "check-pr-template.sh"

# ---- Build JSON results ----
cat > "$RESULTS_FILE" <<EOF
{
  "project": "$PROJECT_NAME",
  "project_path": "$PROJECT_PATH",
  "timestamp": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "summary": {
    "pass": $PASS,
    "fail": $FAIL,
    "warn": $WARN,
    "skip": $SKIP,
    "total": $((PASS + FAIL + WARN + SKIP))
  },
  "rules": {
EOF

# write each rule as valid JSON entries
rule_count=0
total_rules=${#RULE_RESULTS[@]}
for rule_id in "${!RULE_RESULTS[@]}"; do
  rule_count=$((rule_count + 1))
  comma=""
  [ $rule_count -lt $total_rules ] && comma=","
  cat >> "$RESULTS_FILE" <<EOF
    "$rule_id": {
      "status": "${RULE_RESULTS[$rule_id]}",
      "message": "${RULE_MESSAGES[$rule_id]}"
    }${comma}
EOF
done

echo "  }" >> "$RESULTS_FILE"
echo "}" >> "$RESULTS_FILE"

# ---- Generate HTML Report ----
bash "$POLICY_DIR/scripts/generate-report.sh" "$RESULTS_FILE" "$PROJECT_NAME" "$REPORT_DIR"

# ---- Final Summary ----
echo ""
echo -e "${CYAN}══════════════════════════════════════════════════════${NC}"
echo -e "  SUMMARY for ${BLUE}${PROJECT_NAME}${NC}"
echo -e "${CYAN}══════════════════════════════════════════════════════${NC}"
echo -e "  ${GREEN}PASS${NC}  $PASS   ${RED}FAIL${NC}  $FAIL   ${YELLOW}WARN${NC}  $WARN   SKIP  $SKIP"
echo -e "  Report: ${REPORT_DIR}/report_${TIMESTAMP}.html"
echo -e "${CYAN}══════════════════════════════════════════════════════${NC}"
echo ""

[ $FAIL -eq 0 ] && exit 0 || exit 1
