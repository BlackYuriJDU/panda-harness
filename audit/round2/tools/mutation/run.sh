#!/bin/bash
# usage: run.sh  -> runs every mutant on a fresh copy of the worktree
cd /tmp/claude-0/mut
OUT=/home/user/panda-audit-out/gates/mutation.txt; : > $OUT
/home/user/.venv-a5/bin/python - <<'PY' >> $OUT 2>&1
import shutil, subprocess, os, sys, pathlib
sys.path.insert(0, "/tmp/claude-0/mut")
from mutants import MUTANTS
SRC = pathlib.Path("/home/user/panda-audit-src")
for mid, rel, old, new, desc in MUTANTS:
    work = pathlib.Path(f"/tmp/claude-0/mut/w_{mid}")
    if work.exists(): shutil.rmtree(work)
    shutil.copytree(SRC, work, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    f = work / "src" / rel
    text = f.read_text(encoding="utf-8")
    n = text.count(old)
    if n != 1:
        print(f"{mid} SKIP site-count={n} {desc}", flush=True); shutil.rmtree(work); continue
    f.write_text(text.replace(old, new), encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(work / "src"), PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run(["/home/user/.venv-a5/bin/python", "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "tests"],
                       cwd=work, env=env, capture_output=True, text=True, timeout=900)
    tail = [l for l in r.stdout.splitlines() if l.strip()][-1:] 
    failed = [l for l in r.stdout.splitlines() if l.startswith("FAILED")][:2]
    status = "KILLED" if r.returncode != 0 else "SURVIVED"
    print(f"{mid} {status} {desc} :: {tail[0] if tail else ''} :: {' | '.join(failed)}", flush=True)
    shutil.rmtree(work)
PY
