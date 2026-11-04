"""Gera NEXT.md e STATE.yaml a partir de findings.yaml."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import yaml

OUT = Path("/home/user/panda-audit-out")
D = yaml.safe_load((OUT / "findings.yaml").read_text(encoding="utf-8"))
F = {f["id"]: f for f in D["findings"]}
DEFECT = {"new", "variant", "drift"}


def one(text: object, n: int = 240) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip().replace("|", "\\|")
    return s if len(s) <= n else s[: n - 1] + "…"


def tests(*ids: str) -> list[str]:
    out: list[str] = []
    for i in ids:
        for t in F.get(i, {}).get("tests_to_add") or []:
            if t not in out:
                out.append(t)
    return out


ITEMS = [
    ("N1", ["R2-106", "R2-101", "R2-201"], "Isolar toda invocacao de git que o Panda trata como read-only (sonda git_state e run_shell allow)",
     "criar `panda_core.gitsafe.git_env()`/`git_argv()` com `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null` (Windows: NUL), `GIT_ATTR_NOSYSTEM=1`, `GIT_OPTIONAL_LOCKS=0` e `-c core.fsmonitor= -c core.hooksPath=<dir vazio> -c core.pager=cat -c diff.external= --no-ext-diff --no-textconv`; recusar (ask) quando `.git/config` do repo ou de um repo aninhado define `filter.*`, `include*`, `diff.*.command`, `core.sshCommand`, `alias.*`; `git_state` passa a ler `.git/HEAD`/refs sem processo ou usa `git --no-optional-locks status` com o env acima",
     "medio: pode quebrar repos que dependem de filtros (LFS); oferecer opt-in de usuario", "reverter o modulo; a sonda volta ao env antigo"),
    ("N2", ["R2-113", "R2-207", "R2-116"], "Politica do workspace so pode restringir",
     "as regras `allow` passam a vir so do codigo (`default_policy()`); de `.panda/policy.json` aceitar `allow` apenas quando a regra e identica (id, actions, resources) a uma regra allow built-in, e qualquer outra `allow` do arquivo e recusada com exit 2 (atencao: `panda init` grava a politica padrao, que contem allow, nesse arquivo — por isso a comparacao com o built-in e nao uma recusa cega); alternativa equivalente: avaliar a politica do workspace e a built-in e ficar com a mais restritiva por acao/recurso; validar `risk` no enum e ignorar `risk`/`reason` de regra do workspace quando o efeito final vier de regra built-in; validar `actions`/`resources` como lista de strings; um arquivo de usuario (`$PANDA_HOME/policy.json`) passa a ser o unico lugar de `allow` extra",
     "baixo: workspaces que usavam allow passam a pedir aprovacao", "reverter o commit"),
    ("N3", ["R2-500", "R2-204", "R2-501", "DRIFT-501"], "never_auto pelo efeito resolvido",
     "acrescentar `never_auto: bool` em `ShellVerdict` e `Decision`; o classificador marca push/publicacao (git push, gh release/pr merge, twine/uv/poetry publish, npm/pnpm/yarn publish, cargo publish, git send-email); `decide_shell` faz OR das flags mesmo quando devolve `base`; `ApprovalRequest` carrega a flag; `ChatRuntime._approve` usa `request.never_auto`; help de `--auto` corrigido",
     "baixo", "reverter; NEVER_AUTO_RULES continua como fallback"),
    ("N4", ["R2-100", "R2-104", "R2-103", "R2-203", "R2-117"], "Politica de shell por argv, nao por texto",
     "`decide_shell`: regras `allow` so casam quando `argv[0]` canonizado (basename, sem `/`, `\\\\`, `..`) e os tokens seguintes casam um prefixo de tokens; `panda validate*` sai do allow (usar a tool `validate`); wrappers: desembrulhar `timeout`, `stdbuf`, `nice -n`, `ionice`, `setsid`, `sudo -u X`, `env -u X`/`-C`, `busybox`, `xargs`; wrapper desconhecido seguido de git/rm/find = ask; abreviacoes e `--prune`, `-c remote.*.mirror` no classificador; preview do run_shell com `shlex.join(argv)` + cwd",
     "medio: mais `ask` em comandos antes liberados", "reverter o commit; os testes novos voltam a falhar"),
    ("N5", ["R2-102"], "Conteudo vindo do git passa pela politica de leitura",
     "`git log -p`, `git show <rev>` sem pathspec, `git diff` sem pathspec e pathspec glob passam a `ask`; com pathspec explicito, cada caminho passa por `read`; `git show REV:path` passa `path` por `read` (hoje e so ask)",
     "baixo", "reverter"),
    ("N6", ["R2-218", "R2-112", "R2-219", "R2-220", "DRIFT-200"], "Redacao no limite de saida ao provedor e padroes melhores",
     "aplicar `redact_text` ao conteudo de tool.result antes de montar a mensagem ao provedor (agent.py), com a mesma lista `register_secret`; padroes: chave JSON entre aspas, userinfo em URL, `Authorization: Basic`, `Cookie:`, PEM de qualquer tipo, `sk_live_`, `glpat-`, `hf_`; limitar entrada (ex.: 1 MB por chamada) e trocar `(?i)secret` por nomes de variavel completos para reduzir falso positivo",
     "medio: o modelo deixa de ver valores que antes via", "flag de configuracao para desligar so a redacao rumo ao provedor"),
    ("N7", ["R2-221", "R2-306", "R2-310", "R2-222", "R2-223"], "Sanitizacao no ponto de renderizacao",
     "um `render_untrusted(text) -> rich.Text` em `panda_core.display` usado por cockpit (transcricao, ToolCard), tabelas do CLI, `print_error`, `panda models`, `validate`; incluir U+2028/2029, U+061C, U+180E, U+00AD, tags U+E0000-E007F",
     "baixo", "reverter"),
    ("N8", ["R2-502", "R2-503", "R2-504", "R2-505", "R2-506", "R2-224"], "Fechar lacunas da suite (mutantes, instabilidade, cobertura)",
     "acrescentar os testes listados; trocar orcamentos de tempo fixos por espera ativa com prazo 10x; `COVERAGE_PROCESS_START` para grep_worker; rodar a mutacao de bolso na CI semanal",
     "nenhum", "n/a"),
    ("N9", ["R2-107", "R2-318", "R2-320", "R2-317", "R2-211"], "Contencao de processos e Windows",
     "Linux: cgroup v2 ou `prctl(PR_SET_CHILD_SUBREAPER)` + varredura de descendentes; Windows: Job Object com `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, resolver executaveis por caminho absoluto fora do cwd, normalizar `git.exe.`/`git.exe `/`::$DATA`/8.3 no classificador, recusar reparse points em `ensure_plain_path`; executar o checklist Windows do AUDIT-REPORT §13",
     "medio", "reverter por plataforma"),
    ("N10", ["DRIFT-500", "DRIFT-502", "DRIFT-503"], "Documentacao",
     "reescrever SECURITY.md (modelo de ameaca com corpus hostil, controles 0.2, limites conhecidos), corrigir CONTRACT §7 (setsid) e §8 (hard_deny vence; workspace so restringe apos N2), estender test_contract para frases de garantia",
     "nenhum", "n/a"),
]

L: list[str] = []
w = L.append
w("# NEXT — lista de trabalho para o agente executor (auditoria rodada 2, 704f387)")
w("")
w("Fonte de verdade: `findings.yaml`. Ordem = risco x custo (AUDIT-REPORT §12). Cada item fecha uma causa raiz; os achados listados devem ter suas PoCs (pasta `poc/`) passando ao final. Regras: um item por commit/PR; escrever o teste primeiro (falhando em 704f387), depois a correcao; rodar os gates completos; nao apagar nem pular teste.")
w("")
w("Como rodar as PoCs (falham hoje, devem passar depois):")
w("```")
w("cd <repo> && python -m pytest -p no:cacheprovider -q --rootdir=audit/round2/poc/A audit/round2/poc/A")
w("cd <repo> && python -m pytest -p no:cacheprovider -q --rootdir=audit/round2/poc/B audit/round2/poc/B")
w("cd <repo> && python -m pytest -p no:cacheprovider -q --rootdir=audit/round2/poc/E audit/round2/poc/E")
w("```")
w("Ao mover uma PoC para `tests/security/`, renomeie para o comportamento protegido e remova a copia em `poc/`.")
w("")
for nid, ids, title, change, risk, rollback in ITEMS:
    sev = ", ".join(f"{i} [{F[i].get('severity')}]" for i in ids if i in F)
    w(f"## {nid}. {title}")
    w(f"- achados: {sev}")
    w(f"- mudanca minima: {change}")
    t = tests(*ids)
    w("- testes a adicionar:" + ("" if t else " (ver PoCs)"))
    for x in t:
        w(f"  - `{x}`")
    w(f"- risco: {risk}")
    w(f"- rollback: {rollback}")
    w("")

covered = {i for _, ids, *_ in ITEMS for i in ids}
rest = [f for f in D["findings"] if f.get("kind") in DEFECT and f["id"] not in covered]
order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4, "INFO": 5}
rest.sort(key=lambda f: (order.get(str(f.get("severity")), 9), f["id"]))
w("## N11. Demais achados (apos N1–N10)")
w("")
w("| id | sev | causa raiz | mudanca minima | teste | risco |")
w("|---|---|---|---|---|---|")
for f in rest:
    fix = f.get("fix") or {}
    t = (f.get("tests_to_add") or ["(PoC)"])[0]
    w(f"| {f['id']} | {f.get('severity')} | {f.get('root_cause')} | {one(fix.get('approach'), 220)} | `{one(t, 110)}` | {one(fix.get('risk'), 40)} |")
w("")
w("## Melhorias (depois dos defeitos; ordem da trilha D)")
w("")
w("| id | melhoria | custo | fonte |")
w("|---|---|---|---|")
for f in D["findings"]:
    if f.get("kind") == "improvement":
        w(f"| {f['id']} | {one(f.get('title'), 160)} | {f.get('cost')} | {one(f.get('source'), 80)} |")
w("")
w("## Mutacao de bolso (repetir antes de cada release)")
w("")
w("Script usado nesta auditoria: copia a worktree, aplica uma mutacao de uma linha, roda a suite com `PYTHONPATH` na copia e compara com uma rodada de controle. Mutacoes e resultados: `findings.yaml: mutation`. Meta: 0 sobreviventes nao equivalentes.")
(OUT / "NEXT.md").write_text("\n".join(L) + "\n", encoding="utf-8")

defects = [f for f in D["findings"] if f.get("kind") in DEFECT]
state = {
    "schema": "panda.audit.state.v1",
    "commit": "704f387",
    "baseline": "231e48f",
    "updated": "2026-11-04",
    "language": "pt-BR",
    "phases": {
        "P0_orientation": "done",
        "P1_regression": "done (trilha A)",
        "P2_new_surface": "done (trilha B)",
        "P3_blind_spots": "partial (trilha C interrompida por limite de uso apos 24 achados e 20 itens Windows; trifecta escrito pelo auditor principal; schemas/sdist parcial)",
        "P4_test_quality": "done (cobertura, 28 mutantes, instabilidade 3x carga + 5x repouso, drift)",
        "P5_harness": "done (trilha D, web verificada)",
        "P6_windows": "not-run (sem host Windows; checklist estatico/emulado)",
        "P7_consolidate": "done",
    },
    "commands_run": [
        "git fetch origin ccr-97408065-lnoi2u; git worktree add ../panda-audit-src 704f387; git worktree add ../panda-audit-base 231e48f",
        "pip install -e '.[dev,mcp]' pytest-cov hypothesis pip-audit (venv dedicado por commit)",
        "ruff check src tests --no-cache",
        "python -m compileall -q src tests",
        "python -m mypy --platform linux",
        "python -m mypy --platform win32",
        "python -m pytest -q -p no:cacheprovider",
        "pip-audit --skip-editable",
        "pytest --cov=src --cov-branch -q",
        "mutacao: 28 mutantes em copias (gates/mutation.txt, gates/mutation-rerun.txt)",
        "pytest tests/security x3 sob carga, x5 em repouso",
        "PoCs: poc/A (69 falham/10 passam), poc/B (100/9), poc/E (6/1), poc/C (14/2)",
    ],
    "gates": {
        "ruff": "exit 0",
        "compileall": "exit 0",
        "mypy_linux": "exit 0",
        "mypy_win32": "exit 0",
        "pytest": {"passed": 771, "failed": 0, "skipped": 3, "skips": "so-Windows"},
        "pip_audit": "no known vulnerabilities",
        "coverage_branch": "77%",
        "flaky": {"under_load": "1 falha em 3 rodadas (test_a_long_preview_reports_the_hidden_lines) + 4 outros testes de tempo observados nas rodadas de mutacao", "idle": "5/5 verdes"},
    },
    "counts": {
        "regression": dict(Counter(r["verdict"] for r in D["regression"])),
        "findings_by_severity": dict(Counter(str(f.get("severity")) for f in defects)),
        "findings_by_status": dict(Counter(f.get("status") for f in defects)),
        "refuted": sum(1 for f in D["findings"] if f.get("kind") == "refuted"),
        "improvements": sum(1 for f in D["findings"] if f.get("kind") == "improvement"),
        "mutants": {"total": 28, "killed": 21, "survived_real": 5, "survived_equivalent": 2},
    },
    "open_blockers": [
        "Sem host Windows: WIN-001 e 20 itens do checklist nao executados.",
        "Limite de uso da conta interrompeu a trilha C duas vezes.",
    ],
    "not_verified": [
        "Qualquer comportamento em Windows (junctions, ADS, nomes 8.3, taskkill /T, PATHEXT/cwd, shlex posix=False na pratica, msvcrt lock, O_BINARY).",
        "Corrida real entre processos nos TOCTOU de safe_read/grep_worker (demonstrados por troca entre chamadas).",
        "Comportamento com provedor real (OpenCode Go); so httpx.MockTransport.",
        "Conteudo completo de schemas/ contra o produtor e do sdist alem de R2-314/R2-315.",
        "Achados ACI R2-400..R2-416 com uma perna so (probable).",
        "R2-211, R2-317, R2-318, R2-319, R2-320 (Windows, probable/hypothesis).",
    ],
}
(OUT / "STATE.yaml").write_text(yaml.safe_dump(state, allow_unicode=True, sort_keys=False, width=200), encoding="utf-8")
print("NEXT linhas:", len(L), "| resto:", len(rest))
