# Trilha B — superfície nova (Fase 2), 704f387

Notas condensadas por componente: hipóteses -> resultado (confirmada/provável/refutada) -> ponteiro de evidência.
PoCs em `poc/B/` (asserem o resultado SEGURO; falham hoje quando o achado é real).
Bypass de `--auto`/NEVER_AUTO_RULES é do auditor principal (R2-500); aqui só é citado.

## Resumo (47 entradas no YAML)

- P1 (4, confirmados E3): R2-201 `git status` sem aprovação roda `core.fsmonitor` de `.git/config` aninhado gravável; R2-205 `panda validate --root corpus/x` (allow por prefixo de texto) roda JS do agente; R2-200 `git log -p`/`git show` (allow) vazam `.env`/chaves versionados; R2-206 policy.json do corpus transforma todo ask em allow ("only tighten" é falso).
- P2 (8): R2-202 grafias irmãs rebaixam hard_deny para ask; R2-204 publicação não-git aprovada por --auto (rel. R2-500); R2-207 risk/reason da regra da workspace no prompt; R2-210 dependências do validador em corpus/node_modules; R2-217 falsos negativos da redação (JSON etc.); R2-218 resultados de ferramenta sem redação ao provedor; R2-221 cockpit emite ESC/OSC/C1; R2-229 aprovação sem cwd.
- P3 (16, incl. DRIFT-200; 1 provável, 1 hipótese) e P4 (7): ver YAML.
- Refutadas (12): REF-200..REF-211 (inclui DNS rebinding MCP, floor de hard_deny, abreviações de push, `git clean -f` com opção global, gate do modal, C-6, C-11).
- Mutantes do auditor principal: M26 e M12 não são bypass, só falta de teste (testes que os matam: `test_git_clean_force_with_global_option_is_hard_denied`, `test_append_refuses_tail_with_same_seq_but_different_hash`); M22 corresponde a R2-203.
- PoCs: 12 arquivos em `poc/B/` + `poc/B/scripts/`; execução completa: `100 failed, 9 passed` (falha = achado real; os 9 que passam são guardas de refutação). Testes temporais (R2-216, R2-228) repetidos 2-3x com o mesmo resultado.
- Não executado: Windows (R2-211, R2-214 e a tokenização posix=False foram simuladas/estáticas).

## 1. shell_policy.py + policy.decide_shell (+ rotas REPL/cockpit `!cmd`)

Hipóteses e resultado:
- H1 Paridade string (policy_check/MCP) x argv (run_shell) -> **confirmada parcial** (R2-203, P3): `" ".join(argv)` perde fronteiras (`env -S 'git push --force'`: argv ask, texto hard_deny); Windows posix=False mantém aspas (`"--force"` -> ask). Simulado no Linux. M22 sobreviveu.
- H2 Opções globais de git desconhecidas rebaixam force/reset/clean -> **confirmada** (R2-202, P2): `--no-replace-objects`, `--literal-pathspecs`, `--glob-pathspecs` => ask.
- H3 Wrappers com valor / não listados -> **confirmada** (R2-202): `env -u X`, `env -C .`, `sudo -u u`, `timeout`, `stdbuf`, `setsid`, `busybox rm -rf` => ask. `nice -n 5` e `git -C .` OK.
- H4 Config de git (-c remote.*.mirror, push --prune) -> **confirmada** (R2-202): deleção remota é só ask.
- H5 Abreviações/refspecs/caminhos absolutos/git.exe/maiúsculas -> **refutada** (REF-201).
- H6 `git clean -f` (M26 sobreviveu) -> **refutada como bypass**, lacuna de teste (REF-200); `--glob-pathspecs clean -fdx` é bypass (R2-202).
- H7 Comandos read-only (allow) vazam segredos versionados -> **confirmada** (R2-200, P1): `git log -p`, `git show HEAD` imprimem `.env`/`id_ed25519` do histórico; `git show HEAD:.env` só ask (aprovado por --auto).
- H8 Comandos read-only executam configuração de repositório -> **confirmada** (R2-201, P1): `.git/config` aninhado em `corpus/` (gravável por write_file) + `git status` (allow, cwd escolhido) roda `core.fsmonitor`. Irmã de TRUST-004.
- H9 Allows por prefixo de texto para programas não classificados -> **confirmada** (R2-205, P1): `panda validate --root corpus/x` roda JS escrito pelo agente; floor só ancora `.panda/*` e `corpus/validation/*` na raiz.
- H10 Outras publicações sob --auto -> **confirmada** (R2-204, P2; relacionado a R2-500 do auditor principal): gh release, twine, npm publish, git send-email, gh pr merge.
- H11 REPL/cockpit `!cmd` fora do decide_shell -> **refutada** (REF-202).
- H12 Destrutivos fora de rm/find/git (shred, truncate, dd, git checkout -- ., git restore, stash drop/clear, reset --keep, update-ref -d, reflog expire, gc --prune=now, filter-branch, tag -d, branch -M) -> todos `ask` (por projeto; registrado como nota dentro de R2-202, sem achado separado).

PoC: `poc/B/test_b_shell_policy.py` (27 testes; 22 falham hoje = achados; 5 passam = refutações/guardas).

## 2. policy.py (floor, baseline, ENFORCED/PLANNED, carregamento) e runtime.NEVER_AUTO_RULES

Hipóteses:
- H1 Floor resiste a policy.json hostil -> **refutada a quebra** (REF-203): segredos, control plane, `|sh` continuam hard_deny.
- H2 "Workspace só aperta" -> **confirmada falsa** (R2-206, P1): regra allow-all transforma todo ask fora do floor em allow (`python -c`, `.git/hooks/*`, README).
- H3 Classe never_auto decidida por nome de regra -> **confirmada** (R2-500, do auditor principal; não refeito). Variantes de publicação não-git em R2-204.
- H4 risk/reason da regra da workspace chegam ao prompt -> **confirmada** (R2-207, P2): push com risk 'low' e reason 'pre-approved'; risk sem validação e sem sanitize no modal.
- H5 Lista de credenciais incompleta -> **confirmada** (R2-208, P3): 10/11 nomes comuns legíveis.
- H6 PLANNED_ACTIONS: regras para ações não consultadas (delete/git/release) não protegem nada -> já é DEAD-001 (conhecido); não reportado de novo.

PoC: `poc/B/test_b_policy_floor.py` (14 testes; 13 falham = achados).

## 3. safe_read.py (+ quem chama / quem não chama)

Hipóteses:
- H1 TOCTOU de diretório pai (O_NOFOLLOW só no último componente) -> **confirmada** (R2-209, P3; janela simulada).
- H2 Validador: hash e execução em momentos distintos; node segue links após o check -> **provável**, incluída em R2-209 (estática).
- H3 Fecho de dependências do validador gravável (corpus/node_modules, ../lib) -> **confirmada sob pré-condição** (R2-210, P2).
- H4 Junções no Windows -> **provável** (R2-211, E1, não executado).
- H5 evidence/number/claim seguem link -> **refutada** (REF-204; claims usam ensure_plain_path, evidence/numbers usam resolve_inside + authorize).

PoC: `poc/B/test_b_safe_read.py` (2 testes, ambos falham = achados).

## 4. process.py (run_command, _kill_tree, scrubbed_env)

Hipóteses:
- H1 Escape do grupo por setsid/start_new_session -> **confirmada** (R2-212, P3): sobrevive a saída normal e a timeout.
- H2 Teto de saída contornado -> **refutada** (leitura por read1 com corte por stream; neto escapado só atrasa 2 s o join). Sem achado.
- H3 Reuso de PID em killpg após wait -> **hipótese** teórica (janela mínima; grupo vazio). Não reportada como achado.
- H4 scrubbed_env deixa credenciais -> **confirmada** (R2-213, P3): 9/9 nomes comuns passam.
- H5 Windows: busca no cwd do pai (taskkill, git, node) -> **hipótese** (R2-214, E0).
- H6 Leitura concorrente do bytearray -> **refutada** (GIL; join antes do decode).

PoC: `poc/B/test_b_process.py` (11 testes, todos falham = achados; netos são mortos no finally).

## 5. grep tool + grep_worker.py

Hipóteses:
- H1 Ordem política x abertura (TOCTOU para .env) -> **confirmada** (R2-215, P3; janela simulada).
- H2 Enumeração/política no event loop (DoS) -> **confirmada** (R2-216, P4; 3 medições ~1,1 s para 9 mil arquivos).
- H3 Worker importa código do corpus -> **refutada** (`-I`) (REF-205).
- H4 ReDoS no pai -> **refutada** (compile linear; match no worker com timeout).
- H5 Exposição de dados: linhas de arquivos permitidos vão ao provedor sem redação -> coberta pelo achado transversal de redação (seção 6).

PoC: `poc/B/test_b_grep.py` (2 testes, ambos falham).

## 6. redact.py e pontos de aplicação

Hipóteses:
- H1 Saída de ferramenta redigida antes do provedor (C-7) -> **refutada a afirmação** = achado (R2-218, P2): só ledger e instruções.
- H2 Falsos negativos (JSON, URL userinfo, Basic, Cookie, valor com espaço, PEM ENCRYPTED/DSA/PGP, sk_live_, glpat-, hf_, JWT, pwd=) -> **confirmada** (R2-217, P2): 15/15 + JSON real no ledger.
- H3 Falsos positivos em prosa biomédica -> **confirmada** (R2-219, P4).
- H4 ReDoS -> **refutada como exponencial**, mas custo linear alto: instructions.md de 2 MB = ~28 s (R2-220, P4).
- H5 Nome de chave minúscula/variantes (dict-level) -> funciona para dicts Python; problema só em texto (coberto por R2-217).

PoC: `poc/B/test_b_redact.py` (20 testes; 19 falham).

## 7. display.py e todo ponto onde texto externo chega ao terminal

Hipóteses:
- H1 Cobertura Unicode (U+2028/2029, U+061C, U+180E, U+00AD, tags E0000-E007F, VS, fillers) -> **confirmada** (R2-222, P3).
- H2 C0/C1/ESC removidos pelo sanitizer -> **refutada a falha** (o `_CONTROL` final remove tudo de 00-1F/7F-9F; sequências viram texto inerte).
- H3 Cockpit transcrição/ToolCard sem sanitize -> **confirmada** (R2-221, P2): ESC[2J, ESC]52 e 0x9b chegam a `Strip.render`. Rich Markdown também cria hyperlinks OSC 8 cujo destino difere do texto (nota no fix).
- H4 Modal de aprovação: tool/action/resource/rule/reason/preview sanitizados; `risk` não (cockpit.py:233) -> registrado em R2-207.
- H5 `panda validate`/print_error/`--json` -> **confirmada** (R2-223, P3).
- H6 REPL e `panda run` texto -> **refutada** (sanitize_display em repl.py:46/78-88 e main.py:508-514); `panda session show` usa json.dumps (escapa ESC).

PoC: `poc/B/test_b_display.py` (13 testes, todos falham).

## 8. session.py (append estrito, recover, cauda, is_closed) + treehash.py

Hipóteses:
- H1 Evento forjado com mesmo seq e hash diferente aceito por append (M12) -> **refutada** em processo novo (teste novo passa e mata M12); **confirmada** em escritor de longa duração quando o tamanho não muda (R2-224, P3).
- H2 recover legitima evento forjado -> **confirmada como projeto** (R2-227, P4): aceita tipo/ts arbitrários; verify fica ok.
- H3 Leitura reversa da cauda (>128 KiB, linha parcial) -> **refutada** (janela cresce x4 até 256 MiB; linha incompleta -> SessionIntegrityError). Coberto por SES-001.
- H4 is_closed engolindo erro -> sem impacto (append seguinte levanta). MCP anexa após session.end -> **confirmada** (R2-225, P3).
- H5 treehash: escopo podado e cache por (mtime,size) -> **confirmada** (R2-226, P3).
- H6 DoS no tail -> limitado por MAX_TAIL_BYTES; sem achado.

PoC: `poc/B/test_b_session.py` (5 testes; 4 falham, 1 passa = guarda para M12); `poc/B/scripts/rec.py` (recover).

## 9. service.py (_call_tool, _run) + mcp_server.py (stdio e HTTP)

Hipóteses:
- H1 DNS rebinding / Origin / Content-Type no HTTP -> **refutada** (REF-206; proteção vem do FastMCP 1.30; sem teste no repo).
- H2 Paridade MCP x CLI (política e ledger) -> **refutada** (REF-207); MCP não expõe run_shell/write.
- H3 Bloqueio do loop por tool síncrona -> **confirmada** (R2-228, P4; 2 medições de 2,6 s).
- H4 Append após session.end via PANDA_SESSION_FILE -> **confirmada** (R2-225, seção 8).
- H5 policy_check MCP com `approved=true` -> só avaliação, sem efeito; caminho string tem a divergência de R2-203.

PoC: `poc/B/test_b_mcp_http.py` (2 testes; 1 passa = refutação, 1 falha = achado).

## 10. cockpit ApprovalScreen + flags de turno do Agent

Hipóteses:
- H1 Medidor de linhas ocultas ignora overflow horizontal -> **refutada** (texto quebra; REF-208).
- H2 Clique em Allow contorna a trava -> **refutada** (mesmo action_allow).
- H3 Dupla confirmação depende de `risk` controlável pela workspace -> **confirmada** (R2-207).
- H4 O pedido não mostra tudo o que define o efeito (cwd) -> **confirmada** (R2-229, P2).
- H5 Comandos que mudam a conversa durante o turno -> **refutada** (AGT-004 se mantém).

PoC: `poc/B/test_b_cockpit_gate.py` (2 testes; 1 falha).

## 11. provider.py (_sse_data, eventos terminais, _generated_id, reparo de histórico)

Hipóteses:
- H1 [DONE] aceito como terminal fora do OpenAI chat -> **confirmada** (R2-230, P3) nos 3 protocolos.
- H2 Reparo de chamada pendente em todos os protocolos -> **parcial** (R2-231, P4): Google/OpenAI não sintetizam; Anthropic/Responses sim.
- H3 Ids repetidos pelo provedor -> **confirmada** (R2-231): PROV-004 cobre só ids gerados.
- H4 _sse_data (multi-linha, comentários, CRLF, evento final parcial) -> **refutada** (REF-209).
- H5 finish 'length'/'incomplete' executa tools -> **refutada** (AGT-003 se mantém).

PoC: `poc/B/test_b_provider.py` (6 testes, todos falham; httpx.MockTransport, chave sintética).

## 12. .github/workflows (release, ci, dependabot) + tests/test_workflows.py (C-12)

Hipóteses:
- H1 Só `publish` escreve e roda após test+build -> **verdadeira hoje** (sem achado de bypass).
- H2 A especificação (test_workflows) pega regressões perigosas -> **refutada** (R2-232, P3): 4/4 mutações passam.
- H3 Gate do release equivalente ao CI -> **refutada** (R2-233, P4): sem Windows, sem audit, token persistido, ação de terceiro com escrita.
- H4 Dependabot cobre actions e pip -> verdadeiro (sem achado).

PoC: `poc/B/test_b_workflows.py` (4 mutações, todas não detectadas).

