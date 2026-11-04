# Trilha C — pontos cegos e checklist Windows (704f387)

Escopo: Fase 3 (módulos não auditados no round 1 + classes transversais) e Fase 6 (Windows, só estático: não há host Windows; tudo que exige Windows real está marcado `not-run`; o que deu para emular em Linux com `ntpath`/`shlex`/monkeypatch está marcado `emulated`).
Achados já cobertos por outras trilhas são citados em `related`, não repetidos (R2-100/101/102/104/106/109/110/113, R2-209, R2-211, R2-214, R2-216, R2-221, R2-500, R2-503, DRIFT-500).

## Notas por módulo

### workspace.py (descoberta, raízes protegidas, sonda git)
- **R2-300 (P2, confirmado E3)**: `_candidates` sobe até `/` sem checar dono; `.panda/` em `/tmp` ou `C:\` de outra conta vira o workspace (instructions no prompt, ledger no diretório do atacante, validador do atacante executado por `validate`). Mesma classe do CVE-2022-24765 do git (`[memory: unverified]`).
- `_is_protected_dir` (workspace.py:77-96) só cobre SystemRoot/ProgramFiles; é otimização de desempenho, não controle de segurança. Sem achado.
- `git_state` (workspace.py:237-275): coberto por R2-106/R2-101 (filtros/hooks além de fsmonitor). Não repetido.
- `home_workspace_path().parent / "cache"` (workspace.py:301,314): com `PANDA_HOME=/tmp/x` o cache vai para `/tmp/cache` (pasta compartilhada). Conteúdo é só versão do node: INFO, sem achado.

### credentials.py + atomic.py (cofre)
- **R2-301 (P3, E3)**: tmp do `atomic_write_text` nasce 0644 e recebe a chave antes do `chmod`; cofre 0644 existente aceito sem aviso.
- Windows: `os.chmod(…, 0o600)` só mexe no bit read-only; a proteção real vem da ACL herdada de `%LOCALAPPDATA%` (per-user). `PANDA_CREDENTIALS_FILE` apontando para pasta compartilhada herda ACL da pasta. Ver checklist W-13.
- `file_lock` (atomic.py:20-77): `open(path,"a+b")` segue symlink e escreve `\0` se o alvo estiver vazio; lockfiles ficam em `.panda/` (o corpus pode enviar `.panda/sessions/*.lock` como link para arquivo vazio externo). P4, ver R2-308.

### tools.py — leitores evidence/claim/number
- **R2-302 (P3, E3)**: sem limite de tamanho na leitura (arquivo inteiro na memória, no event loop) e sem `_cap` no retorno de `evidence_show`/`number_show`/frontmatter de `claim_show`.
- Symlinks: `resolve_inside` + política no caminho resolvido; `claims` usa `ensure_plain_path`. Coberto por REF-100/REF-204.
- REF-300 (FIFO trava boot): refutado, chamadores filtram com `is_file()`.
- REF-301 (glob segue laço de symlink em 3.12): refutado por emulação 3.12/3.13.

### config.py (além da fronteira do provedor)
- **R2-303 (P2, E3)**: `instructions` aceita qualquer caminho relativo; `_read_instructions` não consulta a política. `.git/config` com token em URL chega ao system prompt (o redator pega chave PEM e `password:` mas não userinfo de URL, R2-217).
- **R2-304 (P3, E3)**: `context_window`, `max_output_tokens`, `max_retries`, `timeout_seconds`, `temperature` vêm do corpus; `context_window=10M` anula o trim do AGT-002.
- `node` (regex de nome simples) impede caminho, mas o corpus ainda escolhe QUAL programa do PATH roda (`python3`, `bash`), com `--version` no doctor e com o script no validate. Não dá poder além do próprio validador (que já é código arbitrário). INFO. No Windows, nome nu + busca no cwd: R2-214.
- `timeouts.run_seconds` sem teto superior (ge=1 apenas); `validate_seconds` até 3600: junto de R2-228 (MCP congelado). Incluído em R2-304 como mesma raiz.
- `OpencodeSettings.binary`/`panda_command` aceitos do corpus mas só usados pelo export legado (sem chamadores de execução: `launch_opencode`, `build_run_command` não têm chamador em src). INFO (known limit).

### init.py (backups, .gitignore, symlinks)
- **R2-305 (P2, E3)**: init escreve/copia através de symlink: link pendente cria arquivo fora; `--force` sobrescreve o alvo e copia o conteúdo para `.panda/backups/<stamp>/instructions.md`, que `read_file` lê sem aprovação (o nome não casa com o piso); `.panda/.gitignore` como link anexa linhas a arquivo externo (e faz `.strip()` no conteúdo, removendo indentação inicial).
- `_backup` pode colidir: dois backups no mesmo segundo com mesmo nome em `init.py` usam contador; o `_backup` duplicado em `opencode.py:242-249` não tem contador e sobrescreve backup do mesmo segundo (P4, perda de backup). Registrado em R2-309.
- `_write_if_changed` sem `force` e arquivo existente diferente: anota `unchanged` (mensagem enganosa "kept" quando o conteúdo difere do esperado). P4/INFO.

### doctor.py
- O hint da política inválida manda `panda init --force` (doctor.py:165), o que alimenta R2-305 num corpus hostil.
- Não verifica: modo do cofre, symlinks/junções no control plane, workspace adotado de ancestral (R2-300), `.panda/sessions` com eventos alheios. Proposta de melhoria (IMP) para o consolidado.
- `node --version` usa `config.node` do corpus (aceito, é o lugar documentado onde o node roda).

### registry.py
- `load_registry`/`load_study_card` sem limite de tamanho; `study_list` não tem `limit` e devolve todas as linhas com `question/data/next` sem corte. Mesma raiz de R2-302 (incluído lá).
- `_checked_link` cobre `/`, `//`, `X:` e `..`; com `normpath` e `ensure_plain_path` no card. Coberto por REF-105/R2-109.
- `_LINK_RE` não-guloso corta nomes com `)`; robustez, INFO.

### cockpit.py (fora do modal)
- **R2-307 (P3, E3)**: crash log segue symlink do corpus e o fallback em `/tmp` usa O_CREAT sem O_EXCL.
- Transcrição/ToolCard/ValidationPanel sem `sanitize_display`: R2-221 (não repetido). `/models` mostra ids da rede (B5) sem validação: ver R2-306 (markup) e R2-310 (ids não validados).
- `_on_agent_event` re-renderiza `RichMarkdown` do buffer inteiro a cada 60 ms: custo quadrático em respostas longas (1 MB de texto do provedor congela a UI). P4, R2-311.
- `action_quit_app` e `on_unmount` fecham a sessão com status `completed` mesmo após cancelamento/erro; `ChatRuntime.close` engole exceções (`contextlib.suppress(Exception)`, runtime.py:257): falha de `session.end` é silenciosa. P4, R2-312.

### render.py
- **R2-306 (P3, E3)**: células `str` em `Table.add_row` são markup Rich: OSC 8 a partir do INDEX.md, `MarkupError` com `[/]` num rule id do policy.json.
- `print_error` imprime `details` como JSON com `ensure_ascii=False` (C1/bidi passam) — coberto por R2-223.

### entry.py / __main__.py
- **R2-313 (P3, E3)**: `panda_cli` não tem `__init__.py`; com `python -m panda_cli` o `panda_cli/entry.py` do cwd é importado (namespace) antes do `_sanitize_sys_path`. O fallback `default_panda_command()` grava exatamente esse comando no MCP do OpenCode.
- `_sanitize_sys_path` funciona para `typer.py`/`pydantic.py` no cwd depois que `entry` já carregou (bom); não protege `PYTHONPATH`/`.pth` (fora do modelo).

### terminal.py, brand.py, mascot.py, model_catalog.py, limits.py
- terminal.py: restauração por ANSI só em TTY + `SetConsoleMode` via ctypes com `suppress(Exception)`; sem entrada externa. Sem achado.
- brand.py: respeita `NO_COLOR`/`TERM=dumb`; só constantes. Sem achado.
- mascot.py: valores vêm normalizados e o callback é protegido por `Agent._emit` (REF-302). Só o nome do modelo (de `/model <arg>` ou do corpus) entra cortado em 20 chars num `Text` (ESC passa, R2-221).
- model_catalog.py: snapshot estático de 2026-10-25; usado como fallback silencioso quando a lista ao vivo falha (o `/models` mostra a origem). Ids da rede não validados: R2-310.
- limits.py: `DEFAULT_MAX_STEPS=24`; CLI limita `--max-steps` a 1..200. Sem achado; custo por passo controlado pelo corpus em R2-304.
