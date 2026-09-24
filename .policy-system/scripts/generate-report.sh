#!/usr/bin/env bash
# Generates HTML report from check results + SARIF files
RESULTS_FILE="$1"
PROJECT_NAME="$2"
REPORT_DIR="$3"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
HTML_FILE="${REPORT_DIR}/report_${TIMESTAMP}.html"
LATEST_FILE="${REPORT_DIR}/report_latest.html"

[ ! -f "$RESULTS_FILE" ] && echo "No results file found." && exit 1

PASS=$(python3 -c "import json; d=json.load(open('$RESULTS_FILE')); print(d['summary']['pass'])" 2>/dev/null || echo 0)
FAIL=$(python3 -c "import json; d=json.load(open('$RESULTS_FILE')); print(d['summary']['fail'])" 2>/dev/null || echo 0)
WARN=$(python3 -c "import json; d=json.load(open('$RESULTS_FILE')); print(d['summary']['warn'])" 2>/dev/null || echo 0)
SKIP=$(python3 -c "import json; d=json.load(open('$RESULTS_FILE')); print(d['summary']['skip'])" 2>/dev/null || echo 0)
TOTAL=$(python3 -c "import json; d=json.load(open('$RESULTS_FILE')); print(d['summary']['total'])" 2>/dev/null || echo 0)
SCORE=$(python3 -c "print(round($PASS / max($PASS+$FAIL+$WARN,1) * 100))" 2>/dev/null || echo 0)
TIMESTAMP_DISPLAY=$(date)

if   [ "$SCORE" -ge 80 ]; then SCORE_COLOR="#22c55e"
elif [ "$SCORE" -ge 60 ]; then SCORE_COLOR="#f59e0b"
else SCORE_COLOR="#ef4444"; fi

# Generate all dynamic HTML sections with one Python call
python3 - "$RESULTS_FILE" "$REPORT_DIR" "$HTML_FILE" "$LATEST_FILE" \
         "$PROJECT_NAME" "$TIMESTAMP_DISPLAY" \
         "$PASS" "$FAIL" "$WARN" "$SKIP" "$TOTAL" "$SCORE" "$SCORE_COLOR" <<'PYEOF'
import json, sys, os, glob

results_file, report_dir, html_file, latest_file, \
project_name, ts, PASS, FAIL, WARN, SKIP, TOTAL, SCORE, score_color = sys.argv[1:]

PASS, FAIL, WARN, SKIP, TOTAL, SCORE = int(PASS), int(FAIL), int(WARN), int(SKIP), int(TOTAL), int(SCORE)

# ── Rule meta ──────────────────────────────────────────────
RULE_META = {
    "R03": ("Rule 3",  "Clean Code / Formatting",      "Part 1 — Core"),
    "R04": ("Rule 4",  "DRY & Complexity",              "Part 1 — Core"),
    "R05": ("Rule 5",  "Security / OWASP SAST",         "Part 1 — Core"),
    "R06": ("Rule 6",  "Secrets Detection",             "Part 1 — Core"),
    "R08": ("Rule 8",  "Database / SQL Safety",         "Part 1 — Core"),
    "R10": ("Rule 10", "API Design",                    "Part 1 — Core"),
    "R11": ("Rule 11", "Error Handling",                "Part 1 — Core"),
    "R12": ("Rule 12", "Logging Safety",                "Part 1 — Core"),
    "R13": ("Rule 13", "Test Coverage",                 "Part 1 — Core"),
    "R15": ("Rule 15", "Git Workflow",                  "Part 1 — Core"),
    "R16": ("Rule 16", "Dependency Security",           "Part 1 — Core"),
    "R18": ("Rule 18", "Config Separation",             "Part 1 — Core"),
    "R20": ("Rule 20", "AI Hallucination / Dep Verify", "Part 2 — AI"),
    "R21": ("Rule 21", "SAST for AI Code",              "Part 2 — AI"),
    "R25": ("Rule 25", "AI Attribution in PRs",         "Part 2 — AI"),
}
BADGE = {
    "PASS": '<span class="badge pass">✔ PASS</span>',
    "FAIL": '<span class="badge fail">✘ FAIL</span>',
    "WARN": '<span class="badge warn">⚠ WARN</span>',
    "SKIP": '<span class="badge skip">– SKIP</span>',
}

try:
    data  = json.load(open(results_file))
    rules = data.get("rules", {})
except Exception:
    rules = {}

rule_rows = ""
for rid, (label, name, part) in sorted(RULE_META.items()):
    rd     = rules.get(rid, {"status": "SKIP", "message": "Not run"})
    status = rd.get("status", "SKIP")
    msg    = rd.get("message", "")
    badge  = BADGE.get(status, BADGE["SKIP"])
    css    = status.lower()
    rule_rows += f'<tr class="row-{css}"><td>{label}</td><td>{name}</td><td>{part}</td><td>{badge}</td><td>{msg}</td></tr>\n'

# ── Gitleaks / SARIF section ───────────────────────────────
sarif_files = glob.glob(os.path.join(report_dir, "*.sarif")) + \
              glob.glob(os.path.join(report_dir, "gitleaks*.sarif"))

secret_rows = ""
secret_count = 0

for sarif_path in sarif_files:
    try:
        sarif = json.load(open(sarif_path))
        for run in sarif.get("runs", []):
            results = run.get("results", [])
            secret_count += len(results)
            for r in results:
                rule_id  = r.get("ruleId", "unknown")
                msg_text = r.get("message", {}).get("text", "")
                locs     = r.get("locations", [])
                file_path, line = "", ""
                if locs:
                    pl = locs[0].get("physicalLocation", {})
                    file_path = pl.get("artifactLocation", {}).get("uri", "")
                    line      = pl.get("region", {}).get("startLine", "")
                commit = r.get("partialFingerprints", {}).get("commitSha", "")[:8] if r.get("partialFingerprints") else ""
                secret_rows += f"""
                <tr>
                  <td><span class="badge fail">{rule_id}</span></td>
                  <td style="font-family:monospace;font-size:12px">{file_path}</td>
                  <td style="color:#94a3b8">{line}</td>
                  <td style="color:#94a3b8;font-size:12px">{commit}</td>
                  <td style="font-size:12px;color:#fca5a5">{msg_text[:80]}</td>
                </tr>"""
    except Exception:
        pass

if secret_count == 0:
    secrets_section = '''
    <div class="section-title">R06 — Secret Detection</div>
    <div style="background:#1e293b;border:1px solid #334155;border-radius:12px;
                padding:20px 24px;margin-bottom:40px;color:#4ade80;font-size:14px;">
      ✔ No secrets detected in repository.
    </div>'''
else:
    secrets_section = f'''
    <div class="section-title">R06 — Secret Detection &nbsp;
      <span class="badge fail">{secret_count} SECRET(S) FOUND</span></div>
    <table style="margin-bottom:40px">
      <thead>
        <tr>
          <th>Rule</th><th>File</th><th>Line</th><th>Commit</th><th>Description</th>
        </tr>
      </thead>
      <tbody>{secret_rows}</tbody>
    </table>
    <div style="background:#450a0a;border:1px solid #7f1d1d;border-radius:8px;
                padding:14px 18px;margin-bottom:40px;font-size:13px;color:#fca5a5;">
      ⚠ Action required: Remove these files from git history and rotate the exposed keys immediately.<br>
      Run: <code style="background:#1e293b;padding:2px 6px;border-radius:4px">
      git rm --cached &lt;file&gt; &amp;&amp; echo "&lt;file&gt;" &gt;&gt; .gitignore</code>
    </div>'''

# ── Ruff / ESLint section ──────────────────────────────────
lint_section = ""
ruff_file = os.path.join(report_dir, "ruff.json")
eslint_file = os.path.join(report_dir, "eslint.json")

if os.path.exists(ruff_file):
    try:
        ruff_data = json.load(open(ruff_file))
        if ruff_data:
            lint_rows = ""
            for item in ruff_data[:20]:
                f    = item.get("filename", "")
                row  = item.get("location", {}).get("row", "")
                code = item.get("code", "")
                msg  = item.get("message", "")
                lint_rows += f'<tr><td style="font-family:monospace;font-size:12px">{f}</td><td>{row}</td><td><span class="badge warn">{code}</span></td><td style="font-size:12px">{msg}</td></tr>'
            lint_section = f'''
    <div class="section-title">R03 — Python Lint Issues (Ruff)</div>
    <table style="margin-bottom:40px">
      <thead><tr><th>File</th><th>Line</th><th>Code</th><th>Issue</th></tr></thead>
      <tbody>{lint_rows}</tbody>
    </table>'''
    except Exception:
        pass

# ── Write HTML ─────────────────────────────────────────────
html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Policy Report — {project_name}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
          background:#0f172a; color:#e2e8f0; min-height:100vh; }}
  .header {{ background:linear-gradient(135deg,#1e3a5f,#1e293b);
             padding:40px 48px; border-bottom:1px solid #334155; }}
  .header h1 {{ font-size:28px; font-weight:700; color:#f8fafc; }}
  .header .sub {{ font-size:14px; color:#94a3b8; margin-top:6px; }}
  .header .meta {{ font-size:13px; color:#64748b; margin-top:4px; }}
  .container {{ max-width:1100px; margin:0 auto; padding:40px 24px; }}
  .score-section {{ display:flex; gap:24px; margin-bottom:40px; flex-wrap:wrap; }}
  .score-card {{ background:#1e293b; border:1px solid #334155; border-radius:12px;
                 padding:28px 36px; text-align:center; min-width:160px; }}
  .score-number {{ font-size:48px; font-weight:800; color:{score_color}; }}
  .score-label {{ font-size:13px; color:#94a3b8; margin-top:4px; }}
  .stat-card {{ background:#1e293b; border:1px solid #334155; border-radius:12px;
                padding:20px 28px; text-align:center; min-width:120px; flex:1; }}
  .stat-number {{ font-size:36px; font-weight:700; }}
  .stat-label {{ font-size:12px; color:#94a3b8; margin-top:4px;
                 text-transform:uppercase; letter-spacing:.05em; }}
  .section-title {{ font-size:18px; font-weight:600; color:#f1f5f9;
                    margin-bottom:16px; padding-bottom:8px;
                    border-bottom:1px solid #334155; }}
  table {{ width:100%; border-collapse:collapse; margin-bottom:40px;
           background:#1e293b; border-radius:12px; overflow:hidden;
           border:1px solid #334155; }}
  th {{ background:#0f172a; padding:12px 16px; text-align:left;
        font-size:12px; text-transform:uppercase; letter-spacing:.05em;
        color:#94a3b8; font-weight:600; }}
  td {{ padding:12px 16px; font-size:14px; border-top:1px solid #1e293b; }}
  tr.row-pass {{ background:#1e293b; }}
  tr.row-fail {{ background:#1e1a1a; }}
  tr.row-warn {{ background:#1e1c14; }}
  tr.row-skip {{ background:#161e2e; }}
  tr:hover {{ background:#263148 !important; }}
  .badge {{ padding:3px 10px; border-radius:20px; font-size:12px; font-weight:600; white-space:nowrap; }}
  .badge.pass {{ background:#14532d; color:#4ade80; }}
  .badge.fail {{ background:#450a0a; color:#f87171; }}
  .badge.warn {{ background:#422006; color:#fbbf24; }}
  .badge.skip {{ background:#1e293b; color:#64748b; }}
  .manual-section {{ background:#1e293b; border:1px solid #334155; border-radius:12px;
                     padding:24px 28px; margin-bottom:40px; }}
  .manual-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:10px; margin-top:12px; }}
  .manual-item {{ background:#0f172a; border:1px solid #334155; border-radius:8px;
                  padding:10px 14px; font-size:13px; color:#94a3b8; }}
  .manual-item strong {{ color:#e2e8f0; display:block; margin-bottom:2px; }}
  .footer {{ text-align:center; font-size:12px; color:#475569;
             padding:24px; border-top:1px solid #1e293b; margin-top:20px; }}
  code {{ font-family:monospace; }}
</style>
</head>
<body>
<div class="header">
  <h1>Software Development Policy Report</h1>
  <div class="sub">Project: <strong>{project_name}</strong></div>
  <div class="meta">Generated: {ts} &nbsp;|&nbsp; Policy v1.2 — 25 Rules</div>
</div>
<div class="container">

  <div class="score-section">
    <div class="score-card">
      <div class="score-number">{SCORE}%</div>
      <div class="score-label">Compliance Score</div>
    </div>
    <div class="stat-card"><div class="stat-number" style="color:#22c55e">{PASS}</div><div class="stat-label">Pass</div></div>
    <div class="stat-card"><div class="stat-number" style="color:#ef4444">{FAIL}</div><div class="stat-label">Fail</div></div>
    <div class="stat-card"><div class="stat-number" style="color:#f59e0b">{WARN}</div><div class="stat-label">Warn</div></div>
    <div class="stat-card"><div class="stat-number" style="color:#64748b">{SKIP}</div><div class="stat-label">Skip</div></div>
    <div class="stat-card"><div class="stat-number" style="color:#94a3b8">{TOTAL}</div><div class="stat-label">Total</div></div>
  </div>

  {secrets_section}

  {lint_section}

  <div class="section-title">All Rule Results (15 automated checks)</div>
  <table>
    <thead>
      <tr><th>Rule</th><th>Check</th><th>Policy Part</th><th>Status</th><th>Detail</th></tr>
    </thead>
    <tbody>
      {rule_rows}
    </tbody>
  </table>

  <div class="manual-section">
    <div class="section-title" style="margin-bottom:0;border:none">Rules Requiring Human Review</div>
    <div class="manual-grid">
      <div class="manual-item"><strong>Rule 1</strong>Requirements defined before coding</div>
      <div class="manual-item"><strong>Rule 2</strong>Architecture layering followed</div>
      <div class="manual-item"><strong>Rule 7</strong>Auth enforced server-side</div>
      <div class="manual-item"><strong>Rule 9</strong>Performance & N+1 review</div>
      <div class="manual-item"><strong>Rule 14</strong>Code review completed</div>
      <div class="manual-item"><strong>Rule 17</strong>Documentation maintained</div>
      <div class="manual-item"><strong>Rule 19</strong>AI code read & understood</div>
      <div class="manual-item"><strong>Rule 22</strong>No secrets in AI prompts</div>
      <div class="manual-item"><strong>Rule 23</strong>Architecture not bypassed by AI</div>
      <div class="manual-item"><strong>Rule 24</strong>AI-generated tests verified</div>
    </div>
  </div>

</div>
<div class="footer">
  Software Development Policy v1.2 — Internal Use Only &nbsp;|&nbsp; {project_name} &nbsp;|&nbsp; {ts}
</div>
</body>
</html>"""

open(html_file, "w").write(html)
open(latest_file, "w").write(html)
print(f"  HTML report: {html_file}")
print(f"  Latest:      {latest_file}")
PYEOF
