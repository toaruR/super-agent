"""Diagnostic: capture what the claude vendor returns for the dashboard sample
and what replan() produces after touch_allow backfill. Run in background."""
import json
from harness.roles.decomposer import parse_tasks_md
from harness.roles import planner as planner_role

tasks = parse_tasks_md("probe/samples/dashboard-tasks.md")
with open("probe/samples/_diag_vendor_raw.json", "w", encoding="utf-8") as f:
    pass

# Capture the raw vendor response by wrapping planner_role.invoke
_orig_invoke = planner_role.invoke

def _capture(vendor, prompt, **kw):
    res = _orig_invoke(vendor, prompt, **kw)
    with open("probe/samples/_diag_vendor_raw.json", "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2, default=str)
    return res

planner_role.invoke = _capture

rep = planner_role.replan("台帳ダッシュボードを md/html で出力", tasks,
                          events=[], vendor="claude", seq=None, dry_run=False)
with open("probe/samples/_diag_replan_out.json", "w", encoding="utf-8") as f:
    json.dump(rep.get("tasks", []), f, ensure_ascii=False, indent=2, default=str)

print("=== VENDOR RAW (task_ids + touch_allow) ===")
vraw = json.load(open("probe/samples/_diag_vendor_raw.json", encoding="utf-8"))
for t in vraw.get("tasks", []):
    print(" ", t.get("task_id"), "touch_allow=", t.get("touch_allow"),
          "deps=", t.get("depends_on"))
print("=== REPLAN OUT ===")
for t in rep.get("tasks", []):
    print(" ", t["task_id"], "touch_allow=", t.get("touch_allow"),
          "deps=", t.get("depends_on"))
print("notes:", rep.get("notes"))
