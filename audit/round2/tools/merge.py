"""Consolida tracks/*.yaml em findings.yaml (schema panda.audit.findings.v2).

- Mapeia root_cause locais para causas raiz globais (RC-01..).
- Funde duplicatas exatas: o id canonico fica, o duplicado some e entra em `related`
  e em `merged_from` do canonico (com a evidencia anexada).
- Recalibra severidades decididas na consolidacao (SEVERITY_OVERRIDE).
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

OUT = Path("/home/user/panda-audit-out")
TRACKS = ["A-regression.yaml", "B-newsurface.yaml", "C-blindspots.yaml", "D-harness.yaml", "E-lead.yaml"]

# duplicado -> canonico
MERGE = {
    "R2-205": "R2-104",
    "R2-206": "R2-113",
    "R2-200": "R2-102",
    "R2-202": "R2-103",
    "R2-212": "R2-107",
    "R2-208": "R2-108",
    "R2-217": "R2-112",
    "R2-213": "R2-114",
    "R2-230": "R2-120",
    "R2-231": "R2-124",
}
EXTRA_MERGE_FILE = OUT / "merge_extra.yaml"  # duplicatas de C/D decididas depois

RC = {
    "RC-01": "never_auto decidido pelo nome da regra, nao pelo efeito resolvido",
    "RC-02": "politica de shell casa o texto unido do argv; allow por prefixo de texto",
    "RC-03": "classificador de argv so escala e cobre poucos programas/wrappers/grafias",
    "RC-04": "politica do workspace (dado do corpus) pode afrouxar: floor so contem hard_deny",
    "RC-05": "comandos git 'read-only' executam programas escolhidos pela config do repositorio",
    "RC-06": "conteudo vindo do git (historico, diff) contorna a politica de leitura",
    "RC-07": "denylists incompletas (arquivos de credencial, variaveis de ambiente)",
    "RC-08": "redacao so na persistencia e por padroes fracos; texto ao provedor sem redacao",
    "RC-09": "sanitizacao de terminal aplicada por ponto de chamada, nao na saida",
    "RC-10": "check-then-use e symlinks/juncoes (TOCTOU) em leituras do workspace",
    "RC-11": "contencao e resolucao de processos (grupo POSIX, taskkill, busca no PATH/cwd no Windows)",
    "RC-12": "integridade do ledger depende de cache e de rotas que nao registram",
    "RC-13": "criterios de fim de stream e reparo de historico por protocolo incompletos",
    "RC-14": "pedido de aprovacao incompleto ou ambiguo (C-4)",
    "RC-15": "fechamento do validador: o que ele importa/le nao e protegido",
    "RC-16": "trabalho sincrono no event loop",
    "RC-17": "escopo do tree hash",
    "RC-18": "spec de CI/release testada por texto",
    "RC-19": "qualidade da suite: testes instaveis, lacunas de cobertura e de mutacao",
    "RC-20": "documentacao divergente do codigo",
    "RC-21": "fronteira de configuracao do workspace (config.json) e validacao de esquema",
    "RC-22": "robustez do loop do agente e dos editores",
    "RC-23": "interface agente-ferramenta (ACI): schemas, erros, limites, saida para maquinas",
    "RC-24": "estado de usuario fora do workspace (cofre, backups, logs) e permissoes",
    "RC-25": "isolamento de importacao e empacotamento (cwd, namespace package, sdist)",
}

# id -> RC global (preenchido para A/B/E; C/D usam MAP_EXTRA em merge_extra.yaml)
RC_OF = {
    "R2-500": "RC-01", "R2-204": "RC-01", "R2-501": "RC-01", "DRIFT-501": "RC-01",
    "R2-100": "RC-02", "R2-104": "RC-02", "R2-203": "RC-02",
    "R2-103": "RC-03", "R2-503": "RC-03", "R2-504": "RC-03",
    "R2-113": "RC-04", "R2-207": "RC-04", "R2-116": "RC-04",
    "R2-101": "RC-05", "R2-106": "RC-05", "R2-201": "RC-05",
    "R2-102": "RC-06",
    "R2-108": "RC-07", "R2-114": "RC-07",
    "R2-112": "RC-08", "R2-218": "RC-08", "R2-219": "RC-08", "R2-220": "RC-08", "DRIFT-200": "RC-08",
    "R2-221": "RC-09", "R2-222": "RC-09", "R2-223": "RC-09", "R2-122": "RC-09",
    "R2-209": "RC-10", "R2-211": "RC-10", "R2-215": "RC-10", "R2-109": "RC-10", "R2-118": "RC-10",
    "R2-107": "RC-11", "R2-214": "RC-11",
    "R2-224": "RC-12", "R2-225": "RC-12", "R2-227": "RC-12", "R2-502": "RC-12", "R2-111": "RC-12",
    "R2-120": "RC-13", "R2-115": "RC-13", "R2-123": "RC-13", "R2-124": "RC-13", "R2-121": "RC-22",
    "R2-117": "RC-14", "R2-125": "RC-14", "R2-229": "RC-14",
    "R2-210": "RC-15", "R2-105": "RC-15",
    "R2-216": "RC-16", "R2-228": "RC-16",
    "R2-226": "RC-17",
    "R2-232": "RC-18", "R2-233": "RC-18",
    "R2-505": "RC-19", "R2-506": "RC-19",
    "DRIFT-500": "RC-20", "DRIFT-502": "RC-20", "DRIFT-503": "RC-20",
    "R2-110": "RC-21", "R2-119": "RC-22",
}

SEVERITY_OVERRIDE = {
    "R2-106": ("P0", "consolidacao: abrir `panda chat`/`panda run` num corpus entregue com .git (zip/tar/copia) executa filtro clean/hook do corpus sem nenhuma tool call nem aprovacao; rubrica P0. Um clone git nao traz .git/config nem hooks, por isso o vetor se limita a entrega por arquivo/copia, o mesmo modelo aceito em TRUST-004"),
    "R2-101": ("P1", "consolidacao: execucao de programa escolhido pelo corpus via git status/diff/show (allow) quando o agente e guiado por texto do corpus, sem aprovacao; rubrica P1"),
    # push/destrutivo sob --auto sem aprovacao humana: mesma rubrica de R2-500
    "R2-103": ("P1", "consolidacao: com --auto, hard_deny rebaixado a ask e executado sem aprovacao humana (mesma precondicao de R2-500); sem --auto seria P2"),
    # segredo hard_deny lido e enviado ao provedor sem aprovacao
    "R2-102": ("P1", "consolidacao: git log -p/show HEAD sao allow; o .env versionado chega ao modelo/provedor sem aprovacao (A e B divergiam: P2/P1)"),
}

REQUIRED = ["id", "kind", "title", "claim"]


def load(name: str) -> dict:
    path = OUT / "tracks" / name
    if not path.exists():
        print(f"AVISO: {name} ausente", file=sys.stderr)
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def main() -> None:
    extra = yaml.safe_load(EXTRA_MERGE_FILE.read_text(encoding="utf-8")) if EXTRA_MERGE_FILE.exists() else {}
    merge = {**MERGE, **(extra.get("merge") or {})}
    rc_of = {**RC_OF, **(extra.get("rc_of") or {})}
    sev = {**SEVERITY_OVERRIDE, **{k: tuple(v) for k, v in (extra.get("severity") or {}).items()}}

    regression, findings, side = [], [], {}
    for name in TRACKS:
        data = load(name)
        regression += data.get("regression") or []
        findings += data.get("findings") or []
        for key in ("windows_checklist", "trifecta_map", "mutation"):
            if data.get(key):
                side[key] = data[key]

    if not side.get("trifecta_map") and extra.get("trifecta_map"):
        side["trifecta_map"] = extra["trifecta_map"]
    for row in regression:
        if row.get("verdict") == "unverified":
            row["verdict"] = "not-reproducible"
            row["note"] = "nao executado: sem host Windows (o enum do schema nao tem 'unverified'). " + str(row.get("note", ""))

    by_id = {f["id"]: f for f in findings}
    assert len(by_id) == len(findings), "ids repetidos entre trilhas"
    for dup, canon in merge.items():
        if dup not in by_id or canon not in by_id:
            print(f"AVISO: merge {dup}->{canon} ignorado (id ausente)", file=sys.stderr)
            continue
        d, c = by_id.pop(dup), by_id[canon]
        c.setdefault("merged_from", []).append(dup)
        c["related"] = sorted(set((c.get("related") or []) + (d.get("related") or []) + [dup]) - {canon})
        c["locations"] = list(dict.fromkeys((c.get("locations") or []) + (d.get("locations") or [])))
        c["tests_to_add"] = list(dict.fromkeys((c.get("tests_to_add") or []) + (d.get("tests_to_add") or [])))
        for ev in d.get("evidence") or []:
            ev = dict(ev)
            ev["detail"] = f"[{dup}] " + str(ev.get("detail", "")) if "detail" in ev else ev.get("detail")
            if ev.get("detail") is None:
                ev.pop("detail")
            ev.setdefault("note", f"evidencia de {dup}")
            c.setdefault("evidence", []).append(ev)
        order = ["P0", "P1", "P2", "P3", "P4", "INFO"]
        if d.get("severity") in order and c.get("severity") in order and order.index(d["severity"]) < order.index(c["severity"]):
            c["severity"] = d["severity"]
            c.setdefault("calibration", f"severidade elevada para a de {dup}")
    for fid, (level, why) in sev.items():
        if fid in by_id:
            prev = by_id[fid].get("severity")
            by_id[fid]["severity"] = level
            by_id[fid]["calibration"] = why
            if prev != level:
                by_id[fid]["falsification"] = (
                    f"[consolidacao: severidade da trilha {prev} -> {level}; ver `calibration`] "
                    + str(by_id[fid].get("falsification", ""))
                )
    for f in by_id.values():
        if f["id"] in rc_of:
            f["local_root_cause"] = f.get("root_cause")
            f["root_cause"] = rc_of[f["id"]]
        for key in (REQUIRED if f.get("kind") != "improvement" else ["id", "kind", "title"]):
            assert key in f, (f["id"], key)
        if f.get("status") == "confirmed":
            legs = {e.get("leg") for e in f.get("evidence") or []}
            if not {"A", "B"} <= legs:
                f["status"] = "probable"
                f["calibration"] = (f.get("calibration", "") + "; rebaixado a probable: falta uma das pernas A/B").strip("; ")

    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4, "INFO": 5}
    kind_order = {"new": 0, "variant": 0, "drift": 1, "refuted": 3, "improvement": 2}
    final = sorted(by_id.values(), key=lambda f: (kind_order.get(f.get("kind"), 9), order.get(str(f.get("severity")), 6), f["id"]))
    doc = {
        "schema": "panda.audit.findings.v2",
        "meta": {
            "commit": "704f387",
            "baseline": "231e48f",
            "date": "2026-11-03",
            "auditor_model": "claude-opus-5-5",
            "effort": "high",
            "language": "pt-BR",
            "environment": {"os": "Linux 6.18.44 (x86_64, container de nuvem)", "python": "3.13.18", "node": "22.23.4", "git": "2.43.0"},
            "gates": {"ruff": "0", "compileall": "0", "mypy_linux": "0", "mypy_win32": "0", "pytest": "771/0/3", "pip_audit": "no known vulnerabilities"},
            "root_causes": RC,
        },
        "regression": regression,
        "findings": final,
        **side,
    }
    (OUT / "findings.yaml").write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=200), encoding="utf-8"
    )
    from collections import Counter
    print("regression:", len(regression), Counter(r["verdict"] for r in regression))
    print("findings:", len(final), Counter(f.get("kind") for f in final))
    print("severity:", Counter(str(f.get("severity")) for f in final if f.get("kind") in {"new", "variant", "drift"}))
    print("status:", Counter(f.get("status") for f in final if f.get("kind") != "improvement"))
    missing = [f["id"] for f in final if f.get("kind") in {"new", "variant", "drift"} and f["id"] not in rc_of]
    print("sem RC global:", missing)


if __name__ == "__main__":
    main()
