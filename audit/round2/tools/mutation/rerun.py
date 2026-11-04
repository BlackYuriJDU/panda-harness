import shutil, subprocess, os, sys, pathlib, re
sys.path.insert(0, "/tmp/claude-0/mut")
from mutants import MUTANTS
extra = [("M15b","panda_cli/cockpit.py",'        now = time.monotonic()\n        if now < self._armed_at:\n            return  # a key typed ahead','        now = time.monotonic()\n        if False:\n            return  # a key typed ahead',"ApprovalScreen not armed (action_allow)"),
         ("M27","panda_core/session.py",'        if anchor_seq > self._tail[0]:','        if False:',"_check_anchor accepts truncated log"),
         ("M28","panda_core/shell_policy.py",'    if sub == "reset" and _long_any(rest, "--hard", 4):','    if False:',"git reset --hard not hard_deny (classifier)")]
want = {"M02","M04","M11","M13","M16","M12","M22","M26"}
todo = [m for m in MUTANTS if m[0] in want] + extra
SRC = pathlib.Path("/home/user/panda-audit-src")
def run(work):
    env = dict(os.environ, PYTHONPATH=str(work / "src"), PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run(["/home/user/.venv-a5/bin/python","-m","pytest","-q","-p","no:cacheprovider","tests","--deselect","tests/security/test_w2b.py::test_timeout_kills_the_whole_process_group","--deselect","tests/security/test_w2b.py::test_wait_for_really_bounds_a_running_tool","--deselect","tests/security/test_w4.py::test_a_long_preview_reports_the_hidden_lines"], cwd=work, env=env, capture_output=True, text=True, timeout=1500)
    fails = sorted(set(re.findall(r"^FAILED (\S+)", r.stdout, re.M)))
    last = [l for l in r.stdout.splitlines() if l.strip()][-1]
    return fails, last
def copy(name):
    work = pathlib.Path(f"/tmp/claude-0/mut/w_{name}")
    if work.exists(): shutil.rmtree(work)
    shutil.copytree(SRC, work, ignore=shutil.ignore_patterns(".git","__pycache__"))
    return work
w = copy("CTRL"); base_fails, last = run(w); shutil.rmtree(w)
print(f"CONTROL (no mutation) :: {last} :: {base_fails}", flush=True)
for mid, rel, old, new, desc in todo:
    w = copy(mid); f = w/"src"/rel; t = f.read_text(encoding="utf-8")
    if t.count(old) != 1: print(f"{mid} SKIP count={t.count(old)}"); shutil.rmtree(w); continue
    f.write_text(t.replace(old,new), encoding="utf-8")
    fails, last = run(w); shutil.rmtree(w)
    specific = [x for x in fails if x not in base_fails]
    print(f"{mid} {'KILLED' if specific else 'SURVIVED'} {desc} :: {last} :: specific={specific[:3]}", flush=True)
