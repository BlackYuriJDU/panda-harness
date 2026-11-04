# Trilha A — Regressão e variantes (rodada 2, 704f387)

Base de comparação: 231e48f. Testes rodados com `PYTHONDONTWRITEBYTECODE=1 -p no:cacheprovider`.
Execução no alvo: tests/security + test_policy/validator/agent/cockpit/contract/workflows = 487 passed.

## Tabela de vereditos (preenchida incrementalmente)

| id | veredito | evidência (uma linha) |
|---|---|---|
| SEC-001 | bypassed | R2-100: argv[0]='git status/../..'+bin roda sem aprovação; R2-101/102 em git read-only |
| SEC-002 | bypassed | R2-104: panda validate --root corpus/x roda validador do agente sem aprovação |
| SEC-003 | partial | grep/list_files ok; rota git read-only vaza .env sem aprovação (R2-102) |
| SEC-010 | fixed-verified | card contido e autorizado; INDEX simbólico (R2-109) e study_list sem policy (R2-118) são P4 |
| PROV-001 | partial | loop do agente fail-closed; '!cmd' sem registro e sem parar (R2-111) |
| TRUST-001 | fixed-verified | destino fixo verificado; R2-110 (provider.name escolhe a chave do cofre) P4 |
| TRUST-002 | fixed-verified | status não executa node; variante Windows cwd não verificada |
| TRUST-005 | fixed-verified | config/policy/instructions/validator protegidos; INDEX.md fora da lista (R2-109) |
| TRUST-003 | bypassed | R2-113: regra allow do workspace abre shell e escrita sem aprovação |
| SEC-004 | partial | NAME=value ok; JSON-texto, URLs e vários prefixos vazam (R2-112) |
| SEC-005 | partial | lista ampliada ok; nomes comuns fora da lista (R2-108) e rota git (R2-102) |
| SEC-006 | fixed-verified | grep em processo filho com timeout; testes do baseline falham por símbolo novo |
| SEC-011 | fixed-verified | symlink de arquivo e de diretório recusados |
| VAL-001 | fixed-verified | relatórios vazios/pulados recusados; duplicatas e lixo final refutados |
| VAL-002 | partial | chave do provedor removida; PGPASSWORD/MYSQL_PWD/DATABASE_URL/SSH_AUTH_SOCK passam (R2-114) |
| AGT-001 | partial | exceção do handler vira resultado; RecursionError no parse escapa (R2-121) |
| AGT-002 | fixed-verified | tarefa fixada e trim registrado; 1 teste do grupo passa no baseline |
| AGT-003 | partial | motivos conhecidos ok; desconhecidos executam tool calls (R2-115) |
| PROV-002 | partial | corte sem terminal recusado; '[DONE]' falso aceito em 3 protocolos (R2-120) |
| PROV-004 | fixed-verified | ids gerados únicos; ids do provedor não são deduplicados (R2-123) |
| POL-001 | fixed-verified | efeito validado; tipos e risk não validados (R2-116) |
| ADP-001 | partial | 3 protocolos reparados; OpenAI chat não (R2-124) |
| APR-002 | fixed-verified | modal mostra e exige o fim; prévia do run_shell ambígua (R2-117) |
| APR-003 | partial | REPL ok; CLI study show/list passam ESC (R2-122) |
| MCP-002 | partial | validate/study_show autorizados; study_list/panda://studies sem policy (R2-118) |
| EDIT-001 | partial | CRLF preservado; CR solto vindo de new_string (R2-119) |
| SES-001 | fixed-verified | evento > 128 KiB reabre; falha no baseline por JSONDecodeError |
| SES-002 | fixed-verified | os.write completo; recover explícito |
| SES-003 | fixed-verified | evento forjado não é lavado; limite documentado |
| LED-001 | fixed-verified | hashes no ledger; testes de treehash falham por módulo novo |
| GEN-001 | not-addressed | não endereçado (roadmap) |
| PERF-001 | fixed-verified | sem releitura do ledger; 1 teste não discriminante |
| SEC-007 | partial | ordem de opções ok; wrappers/abreviações rebaixam hard_deny a ask (R2-103) |
| SEC-008 | partial | grupo morto; setsid escapa (R2-107); Windows não rodado |
| SEC-009 | fixed-verified | lock com timeout e modo preservado |
| AGT-004 | fixed-verified | reset recusado durante o turno |
| APR-001 | partial | aviso de truncagem existe, mas aprovação continua possível (R2-125) |
| APR-004 | bypassed | ver R2-500 |
| UI-001 | fixed-verified | y prematuro não aprova; alto risco pede 2x |
| UI-002 | partial | REPL/run ok; study show/list e cockpit sem sanitize (R2-122) |
| INIT-001 | fixed-verified | init não reescreve config inalterado |
| TRUST-004 | bypassed | só core.fsmonitor neutralizado; clean filter/hook (R2-106) e run_shell (R2-101) executam |
| ADP-002 | fixed-verified | Gemini começa com user |
| DOC-001 | partial | contrato estrutural verificado; afirmações de garantia falsas (R2-113, R2-107) |
| SUP-001 | partial | write só no job publish; actions por tag |
| DEP-001 | fixed-verified | piso mcp 1.30 |
| PRIV-001 | partial | prosa em hash; args e resultados de tools gravados |
| MCP-001 | fixed-verified | nomes FastMCP casam as regras |
| PROV-003 | fixed-verified | SSE multilinha ok |
| WIN-001 | unverified | não executado: sem host Windows |
| LEG-001 | partial | envelope corrigido; funções mortas permanecem |
| PRIV-002 | fixed-verified | crash log redigido e privado |
| DEAD-001 | partial | ações marcadas PLANNED; testes falham por símbolo novo |

Contagem: fixed-verified 25 · partial 21 · bypassed 5 (SEC-001, SEC-002, TRUST-003, TRUST-004, APR-004) · not-addressed 1 (GEN-001) · unverified 1 (WIN-001, valor fora do enum a pedido do auditor principal).

## Não-vacuidade (passo b)

- Todos os 7 arquivos `tests/security/test_w*.py` + `test_contract/test_workflows/test_policy/test_validator/test_agent/test_cockpit` copiados para 231e48f e rodados: 298 falhas / 108 passes na pasta security (os passes são, em sua maioria, testes `test_neg_*` e variantes que já eram hard_deny).
- `test_w4.py` e `test_w5.py` só coletam no baseline com shim de import (`sanitize_display`, `register_secret` ausentes); os testes que dependem deles falham por ImportError (sanitizador, `register_secret`), os demais por comportamento.
- Falhas no baseline só por símbolo novo (comportamento não testado no baseline): SEC-006 (`GREP_TIMEOUT_SECONDS`), DEAD-001 (`ENFORCED_ACTIONS`/`PLANNED_ACTIONS`), LED-001 (módulo `treehash`), SES-002/003 `recover` (método novo), UI-001 em parte (`ARM_DELAY_SECONDS`), APR-001/002 em parte (widgets `#approval-status`), SEC-008 em parte (kwargs `max_output_bytes`/`cancel`), TRUST-005 1 teste (`safe_read`).
- Testes não discriminantes (passam também no baseline): `test_cancelling_the_tool_call_stops_the_command`, `test_wait_for_really_bounds_a_running_tool` (SEC-008), `test_is_closed_follows_another_instance` (PERF-001), `test_trimming_never_leaves_an_orphan_tool_message` (AGT-002), `test_init_backs_up_a_config_that_really_changes` (INIT-001).
- Testes de tempo `test_timeout_kills_the_whole_process_group` e `test_wait_for_really_bounds_a_running_tool`: 3/3 execuções verdes no alvo.

## Achados novos/variantes (detalhe em A-regression.yaml)

| id | sev | título curto |
|---|---|---|
| R2-100 | P1 | argv[0] `git status/../..`+binário casa a regra allow pelo texto unido e executa sem aprovação |
| R2-104 | P1 | `panda validate --root corpus/x` (allow) roda validador escrito pelo agente |
| R2-113 | P1 | policy.json do corpus afrouxa shell/write com regras allow (CONTRACT "só aperta" é falso) |
| R2-101 | P2 | git status/diff/show (allow) executam fsmonitor/diff.external/textconv do .git/config |
| R2-102 | P2 | `.env` rastreado sai por git diff/show/log -p e pathspec glob, sem aprovação |
| R2-106 | P2 | sonda git_state roda filtro clean e hook post-index-change do corpus ao abrir |
| R2-103 | P3 | hard_deny vira ask: timeout/stdbuf, `sudo -u`, `env -u/-C`, `rm --r`, `push --prune` |
| R2-105 | P3 | piso do control plane não segue `config.validator` |
| R2-107 | P3 | neto com setsid sobrevive ao kill do grupo |
| R2-108 | P3 | denylist de credenciais sem .envrc, id_ecdsa, kubeconfig, ADC, tfstate... |
| R2-111 | P3 | `!cmd` sem registro no ledger e sem parar com ledger quebrado |
| R2-112 | P3 | redator não cobre JSON-texto (tool.result), URL com senha, vários prefixos |
| R2-114 | P3 | filhos herdam PGPASSWORD, MYSQL_PWD, DATABASE_URL, SSH_AUTH_SOCK |
| R2-117 | P3 | prévia do run_shell = `" ".join(argv)`: argv diferentes, prévia igual |
| R2-120 | P3 | `[DONE]` aceito como terminal em Anthropic/Responses/Google |
| R2-122 | P3 | `panda study show/list` mandam ESC/OSC do corpus ao terminal |
| R2-125 | P3 | prévia truncada em 6000 chars continua aprovável |
| R2-109 | P4 | INDEX.md simbólico lido sem safe_read |
| R2-110 | P4 | provider.name do workspace escolhe a chave do cofre |
| R2-115 | P4 | finish_reason desconhecido conta como completo e executa tools |
| R2-116 | P4 | policy aceita actions/resources string e risk arbitrário |
| R2-118 | P4 | study_list (tool/MCP) ignora a política de leitura |
| R2-119 | P4 | edit_file introduz CR solto (finais mistos) |
| R2-121 | P4 | RecursionError em argumentos aninhados escapa do try do AGT-001 |
| R2-123 | P4 | ids repetidos do provedor chegam duplicados ao Anthropic |
| R2-124 | P4 | OpenAI chat sem reparo de tool_call pendente |

Refutados: REF-100 (claims dir simbólico), REF-101 (check duplicado / JSON extra), REF-102 (glob `**` e compile no laço), REF-103 (diferencial de parser de URL), REF-105 (links codificados no INDEX), REF-104 (tarefa maior que a janela).

## Variantes tentadas por linha P1/P2 (resumo)

SEC-001: prefixo pelo texto unido com argv[0] caminho; .git/config hostil; git sem caminho/glob. · SEC-002: `panda validate --root` aninhado; validator configurado fora do diretório padrão; `corpus/../` (refutado). · SEC-003: rota git; globs/symlink (ok). · SEC-010: links codificados (refutado); INDEX simbólico; study_list sem policy. · PROV-001: `!cmd`; ordem registro/ação no MCP. · TRUST-001: parser de URL (refutado); provider.name. · TRUST-002: node=sh/python3 (escopo); Windows cwd (estático). · TRUST-005: INDEX.md; TOCTOU de diretório intermediário (estático). · TRUST-003: regra allow; redefinição de regra do piso (ok); tipos de campos. · SEC-004: JSON, URL, Cookie, prefixos, PEM. · SEC-005: nomes fora da lista; rota git. · SEC-006: glob e compile (refutados). · SEC-011: diretório simbólico (refutado); nome no piso (estático). · VAL-001: duplicata e JSON extra (refutados). · VAL-002: variáveis sem sufixo; SSH_AUTH_SOCK. · AGT-001: JSON aninhado. · AGT-002: tarefa enorme; segundo turno. · AGT-003: 4 motivos desconhecidos. · PROV-002: [DONE] em 3 protocolos. · PROV-004: ids do provedor repetidos. · POL-001: tipos de lista e risk. · ADP-001: OpenAI chat. · APR-002: linha longa (estático), End pula o meio (desenho), argv ambíguo. · APR-003: rotas do REPL (refutado), CLI study. · MCP-002: study_list, ordem do registro. · EDIT-001: CR solto.

## Não verificado e por quê

- WIN-001 e todas as variantes Windows (taskkill /T, junctions, ADS, nomes 8.3, busca no cwd do CreateProcess, msvcrt): sem host Windows.
- R2-500 / APR-004: não re-derivado (dono: auditor principal).
- TOCTOU de safe_read (troca de diretório intermediário) e do grep_worker (troca por symlink entre a checagem e o open): exige processo local concorrente; só estático.
- Nomes de finish_reason de Anthropic/Google em R2-115: [memory: unverified]; o defeito (desconhecido = completo) foi executado.

PoCs: `/home/user/panda-audit-out/poc/A/test_a_*.py` (conftest.py copiado do repositório). Rodar: `cd /home/user/panda-audit-src && PYTHONDONTWRITEBYTECODE=1 /home/user/.venv-a5/bin/python -m pytest -p no:cacheprovider -q --rootdir=/home/user/panda-audit-out/poc/A /home/user/panda-audit-out/poc/A`.
