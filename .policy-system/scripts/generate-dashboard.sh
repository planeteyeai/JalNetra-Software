#!/usr/bin/env bash
# Generates combined HTML dashboard for all projects
COMBINED_DIR="$1"
COMBINED_JSON="$2"
DASHBOARD="${COMBINED_DIR}/dashboard.html"
TIMESTAMP_DISPLAY=$(date)

# Parse data with Python
python3 - "$COMBINED_JSON" "$DASHBOARD" "$TIMESTAMP_DISPLAY" <<'PYEOF'
import json, sys
from datetime import datetime

combined_json = sys.argv[1]
dashboard_path = sys.argv[2]
timestamp = sys.argv[3]

data = json.load(open(combined_json))
projects = data['projects']
overall_score = data['overall_score']
s = data['summary']

# Score color
def score_color(score):
    if score >= 80: return "#22c55e"
    if score >= 60: return "#f59e0b"
    return "#ef4444"

def score_icon(score):
    if score >= 80: return "🟢"
    if score >= 60: return "🟡"
    return "🔴"

# Build project cards
cards = []
for p in sorted(projects, key=lambda x: x.get('score', 0), reverse=True):
    name = p.get('project', 'Unknown')
    ps = p.get('summary', {})
    score = p.get('score', 0)
    color = score_color(score)
    icon = score_icon(score)

    # Rule breakdown rows
    rule_rows = ""
    for rid, rdata in sorted(p.get('rules', {}).items()):
        status = rdata.get('status', 'SKIP')
        msg = rdata.get('message', '')
        badge_map = {
            'PASS': '<span class="b pass">✔</span>',
            'FAIL': '<span class="b fail">✘</span>',
            'WARN': '<span class="b warn">⚠</span>',
            'SKIP': '<span class="b skip">–</span>',
        }
        badge = badge_map.get(status, badge_map['SKIP'])
        rule_rows += f'<tr><td>{rid}</td><td>{badge}</td><td class="msg">{msg}</td></tr>'

    html_link = f'{name}.html'

    cards.append(f'''
    <div class="project-card">
      <div class="card-header">
        <div>
          <div class="project-name">{icon} {name}</div>
          <div class="project-meta">
            ✔ {ps.get("pass",0)} &nbsp; ✘ {ps.get("fail",0)} &nbsp; ⚠ {ps.get("warn",0)} &nbsp; – {ps.get("skip",0)}
          </div>
        </div>
        <div class="score-badge" style="color:{color}">{score}%</div>
      </div>
      <table class="rule-table">
        <tbody>{rule_rows}</tbody>
      </table>
      <div style="margin-top:12px;text-align:right">
        <a href="{html_link}" class="detail-link">Full Report →</a>
      </div>
    </div>
    ''')

cards_html = "\n".join(cards)
overall_color = score_color(overall_score)

# Build leaderboard rows
leaderboard_rows = ""
for i, p in enumerate(sorted(projects, key=lambda x: x.get('score', 0), reverse=True), 1):
    name = p.get('project', 'Unknown')
    score = p.get('score', 0)
    color = score_color(score)
    ps = p.get('summary', {})
    leaderboard_rows += f'''
    <tr>
      <td style="color:#64748b;font-size:13px">#{i}</td>
      <td><strong>{name}</strong></td>
      <td style="color:{color};font-weight:700">{score}%</td>
      <td style="color:#22c55e">{ps.get("pass",0)}</td>
      <td style="color:#ef4444">{ps.get("fail",0)}</td>
      <td style="color:#f59e0b">{ps.get("warn",0)}</td>
    </tr>'''

html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Policy Dashboard — All Projects</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          background: #0f172a; color: #e2e8f0; }}
  .header {{ background: linear-gradient(135deg, #1e3a5f, #1e293b);
             padding: 40px 48px; border-bottom: 1px solid #334155; }}
  .header h1 {{ font-size: 26px; font-weight: 700; color: #f8fafc; }}
  .header .sub {{ font-size: 13px; color: #94a3b8; margin-top: 6px; }}
  .container {{ max-width: 1300px; margin: 0 auto; padding: 40px 24px; }}

  /* Overall score */
  .overall {{ display:flex; gap:20px; margin-bottom:40px; flex-wrap:wrap; align-items:center; }}
  .overall-score {{ background:#1e293b; border:1px solid #334155; border-radius:12px;
                    padding:24px 36px; text-align:center; }}
  .overall-number {{ font-size:56px; font-weight:800; color:{overall_color}; }}
  .overall-label {{ font-size:13px; color:#94a3b8; margin-top:4px; }}
  .stat-block {{ background:#1e293b; border:1px solid #334155; border-radius:12px;
                 padding:18px 24px; text-align:center; min-width:100px; flex:1; }}
  .stat-n {{ font-size:32px; font-weight:700; }}
  .stat-l {{ font-size:11px; color:#94a3b8; text-transform:uppercase; letter-spacing:.05em; }}

  /* Leaderboard */
  .section-title {{ font-size:17px; font-weight:600; color:#f1f5f9; margin-bottom:14px;
                    padding-bottom:8px; border-bottom:1px solid #334155; }}
  .leaderboard {{ background:#1e293b; border:1px solid #334155; border-radius:12px;
                  overflow:hidden; margin-bottom:40px; }}
  .leaderboard table {{ width:100%; border-collapse:collapse; }}
  .leaderboard th {{ background:#0f172a; padding:10px 16px; font-size:11px;
                     text-transform:uppercase; letter-spacing:.05em; color:#94a3b8; text-align:left; }}
  .leaderboard td {{ padding:12px 16px; font-size:14px; border-top:1px solid #1e293b; }}
  .leaderboard tr:hover td {{ background:#263148; }}

  /* Project cards */
  .cards-grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(420px, 1fr));
                 gap:20px; }}
  .project-card {{ background:#1e293b; border:1px solid #334155; border-radius:12px;
                   padding:20px 24px; }}
  .card-header {{ display:flex; justify-content:space-between; align-items:flex-start;
                  margin-bottom:14px; }}
  .project-name {{ font-size:16px; font-weight:600; color:#f1f5f9; }}
  .project-meta {{ font-size:12px; color:#64748b; margin-top:4px; }}
  .score-badge {{ font-size:28px; font-weight:800; }}
  .rule-table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  .rule-table td {{ padding:5px 8px; border-top:1px solid #1e293b; color:#94a3b8; }}
  .rule-table td:first-child {{ color:#64748b; width:48px; font-weight:600; }}
  .msg {{ color:#94a3b8; font-size:12px; }}
  .b {{ padding:2px 7px; border-radius:10px; font-size:11px; font-weight:600; }}
  .b.pass {{ background:#14532d; color:#4ade80; }}
  .b.fail {{ background:#450a0a; color:#f87171; }}
  .b.warn {{ background:#422006; color:#fbbf24; }}
  .b.skip {{ background:#1e293b; color:#64748b; }}
  .detail-link {{ font-size:12px; color:#60a5fa; text-decoration:none; }}
  .detail-link:hover {{ text-decoration:underline; }}

  .footer {{ text-align:center; font-size:12px; color:#475569;
             padding:24px; border-top:1px solid #1e293b; margin-top:20px; }}
</style>
</head>
<body>
<div class="header">
  <h1>Policy Compliance Dashboard</h1>
  <div class="sub">All Projects — Software Development Policy v1.2 &nbsp;|&nbsp; {timestamp}</div>
</div>

<div class="container">

  <div class="overall">
    <div class="overall-score">
      <div class="overall-number">{overall_score}%</div>
      <div class="overall-label">Overall Score</div>
    </div>
    <div class="stat-block"><div class="stat-n" style="color:#94a3b8">{data["total_projects"]}</div><div class="stat-l">Projects</div></div>
    <div class="stat-block"><div class="stat-n" style="color:#22c55e">{s["pass"]}</div><div class="stat-l">Total Pass</div></div>
    <div class="stat-block"><div class="stat-n" style="color:#ef4444">{s["fail"]}</div><div class="stat-l">Total Fail</div></div>
    <div class="stat-block"><div class="stat-n" style="color:#f59e0b">{s["warn"]}</div><div class="stat-l">Total Warn</div></div>
  </div>

  <div class="section-title">Leaderboard</div>
  <div class="leaderboard">
    <table>
      <thead>
        <tr>
          <th>#</th><th>Project</th><th>Score</th><th>Pass</th><th>Fail</th><th>Warn</th>
        </tr>
      </thead>
      <tbody>{leaderboard_rows}</tbody>
    </table>
  </div>

  <div class="section-title">Project Details</div>
  <div class="cards-grid">
    {cards_html}
  </div>

</div>
<div class="footer">
  Software Development Policy v1.2 — Internal Use Only &nbsp;|&nbsp; {timestamp}
</div>
</body>
</html>'''

open(dashboard_path, 'w').write(html)
print(f"Dashboard: {dashboard_path}")
PYEOF
