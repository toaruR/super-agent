import subprocess, shutil, json, os
from pathlib import Path
wt = Path("workspaces/probe_dm3").resolve()
if wt.exists(): shutil.rmtree(wt)
subprocess.run(["git","worktree","prune"], check=False)
subprocess.run(["git","worktree","add","-q",str(wt),"HEAD"], check=True)
from harness.roles.decomposer import parse_tasks_md
tasks = parse_tasks_md("probe/samples/dashboard-tasks.md")
t = next(x for x in tasks if x["task_id"]=="dashboard-model")
from harness.roles import implementer
r = implementer.implement("dashboard-model", t, str(wt), vendor="hermes", model="tencent/hy3:free", effort="high", dry_run=False)
print("RESULT:", json.dumps(r, ensure_ascii=False)[:800])
created = [str(p.relative_to(wt)) for p in sorted(wt.rglob("*")) if ".git" not in p.parts and p.is_file()]
print("FILES:", [p for p in created if "dashboard" in p or "live_probe" in p])
subprocess.run(["git","worktree","remove","-f",str(wt)], check=True)
print("cleaned")
