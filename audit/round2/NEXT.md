# NEXT — lista de trabalho para o agente executor (auditoria rodada 2, 704f387)

Fonte de verdade: `findings.yaml`. Ordem = risco x custo (AUDIT-REPORT §12). Cada item fecha uma causa raiz; os achados listados devem ter suas PoCs (pasta `poc/`) passando ao final. Regras: um item por commit/PR; escrever o teste primeiro (falhando em 704f387), depois a correcao; rodar os gates completos; nao apagar nem pular teste.

Como rodar as PoCs (falham hoje, devem passar depois):
```
cd <repo> && python -m pytest -p no:cacheprovider -q --rootdir=audit/round2/poc/A audit/round2/poc/A
cd <repo> && python -m pytest -p no:cacheprovider -q --rootdir=audit/round2/poc/B audit/round2/poc/B
cd <repo> && python -m pytest -p no:cacheprovider -q --rootdir=audit/round2/poc/E audit/round2/poc/E
```
Ao mover uma PoC para `tests/security/`, renomeie para o comportamento protegido e remova a copia em `poc/`.

## N1. Isolar toda invocacao de git que o Panda trata como read-only (sonda git_state e run_shell allow)
- achados: R2-106 [P0], R2-101 [P1], R2-201 [P1]
- mudanca minima: criar `panda_core.gitsafe.git_env()`/`git_argv()` com `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null` (Windows: NUL), `GIT_ATTR_NOSYSTEM=1`, `GIT_OPTIONAL_LOCKS=0` e `-c core.fsmonitor= -c core.hooksPath=<dir vazio> -c core.pager=cat -c diff.external= --no-ext-diff --no-textconv`; recusar (ask) quando `.git/config` do repo ou de um repo aninhado define `filter.*`, `include*`, `diff.*.command`, `core.sshCommand`, `alias.*`; `git_state` passa a ler `.git/HEAD`/refs sem processo ou usa `git --no-optional-locks status` com o env acima
- testes a adicionar:
  - `tests/security/test_w5.py::test_git_state_probe_does_not_run_a_corpus_clean_filter`
  - `tests/security/test_w5.py::test_git_state_probe_does_not_run_a_corpus_hook`
  - `tests/security/test_w5.py::test_run_shell_git_status_does_not_run_a_corpus_fsmonitor`
  - `tests/security/test_w5.py::test_run_shell_git_diff_does_not_run_a_corpus_external_diff`
  - `tests/security/test_w5.py::test_run_shell_git_show_does_not_run_a_corpus_textconv`
  - `tests/security/test_shell_git_config.py::test_git_status_in_agent_writable_nested_repo_runs_no_config_hook`
- risco: medio: pode quebrar repos que dependem de filtros (LFS); oferecer opt-in de usuario
- rollback: reverter o modulo; a sonda volta ao env antigo

## N2. Politica do workspace so pode restringir
- achados: R2-113 [P1], R2-207 [P2], R2-116 [P4]
- mudanca minima: as regras `allow` passam a vir so do codigo (`default_policy()`); de `.panda/policy.json` aceitar `allow` apenas quando a regra e identica (id, actions, resources) a uma regra allow built-in, e qualquer outra `allow` do arquivo e recusada com exit 2 (atencao: `panda init` grava a politica padrao, que contem allow, nesse arquivo — por isso a comparacao com o built-in e nao uma recusa cega); alternativa equivalente: avaliar a politica do workspace e a built-in e ficar com a mais restritiva por acao/recurso; validar `risk` no enum e ignorar `risk`/`reason` de regra do workspace quando o efeito final vier de regra built-in; validar `actions`/`resources` como lista de strings; um arquivo de usuario (`$PANDA_HOME/policy.json`) passa a ser o unico lugar de `allow` extra
- testes a adicionar:
  - `tests/security/test_w0.py::test_a_workspace_policy_cannot_loosen_shell_or_writes`
  - `tests/security/test_w0.py::test_workspace_policy_cannot_turn_asks_into_allows`
  - `tests/security/test_w4.py::test_workspace_rule_cannot_lower_risk_or_rewrite_reason_of_a_push`
  - `tests/test_policy.py::test_policy_loader_rejects_unknown_risk`
  - `tests/security/test_w1.py::test_policy_rule_with_string_lists_is_refused`
  - `tests/security/test_w1.py::test_policy_rule_with_unknown_risk_is_refused`
- risco: baixo: workspaces que usavam allow passam a pedir aprovacao
- rollback: reverter o commit

## N3. never_auto pelo efeito resolvido
- achados: R2-500 [P1], R2-204 [P2], R2-501 [P3], DRIFT-501 [P3]
- mudanca minima: acrescentar `never_auto: bool` em `ShellVerdict` e `Decision`; o classificador marca push/publicacao (git push, gh release/pr merge, twine/uv/poetry publish, npm/pnpm/yarn publish, cargo publish, git send-email); `decide_shell` faz OR das flags mesmo quando devolve `base`; `ApprovalRequest` carrega a flag; `ChatRuntime._approve` usa `request.never_auto`; help de `--auto` corrigido
- testes a adicionar:
  - `tests/security/test_w4.py::test_auto_never_approves_a_push_with_the_default_policy (parametrizado: env, timeout, git -c, git -C, /usr/bin/git, GIT.EXE)`
  - `tests/security/test_w4.py::test_auto_never_approves_a_push_under_a_corpus_supplied_policy`
  - `tests/security/test_w4.py::test_auto_mode_does_not_approve_other_publishing_commands`
  - `tests/security/test_w4.py::test_run_shell_push_matrix_is_never_auto_approved`
  - `tests/test_contract.py::test_auto_help_matches_the_never_auto_contract`
- risco: baixo
- rollback: reverter; NEVER_AUTO_RULES continua como fallback

## N4. Politica de shell por argv, nao por texto
- achados: R2-100 [P1], R2-104 [P1], R2-103 [P1], R2-203 [P3], R2-117 [P3]
- mudanca minima: `decide_shell`: regras `allow` so casam quando `argv[0]` canonizado (basename, sem `/`, `\\`, `..`) e os tokens seguintes casam um prefixo de tokens; `panda validate*` sai do allow (usar a tool `validate`); wrappers: desembrulhar `timeout`, `stdbuf`, `nice -n`, `ionice`, `setsid`, `sudo -u X`, `env -u X`/`-C`, `busybox`, `xargs`; wrapper desconhecido seguido de git/rm/find = ask; abreviacoes e `--prune`, `-c remote.*.mirror` no classificador; preview do run_shell com `shlex.join(argv)` + cwd
- testes a adicionar:
  - `tests/security/test_w1.py::test_run_shell_argv0_with_allowed_prefix_cannot_run_another_program`
  - `tests/security/test_w1.py::test_decide_shell_joined_text_prefix_is_not_the_program`
  - `tests/security/test_w1.py::test_agent_cannot_run_a_validator_it_wrote_through_panda_validate_root`
  - `tests/security/test_shell_policy.py::test_panda_validate_via_run_shell_cannot_run_agent_written_validator`
  - `tests/security/test_shell_policy.py::test_unclassified_program_is_never_allow`
  - `tests/security/test_w1.py::test_destructive_forms_stay_hard_denied`
  - `tests/security/test_shell_policy.py::test_force_and_destructive_spellings_stay_hard_denied`
  - `tests/security/test_shell_policy.py::test_text_shown_in_preview_decides_like_the_argv_that_runs`
  - `tests/security/test_shell_policy.py::test_windows_string_tokenization_keeps_quoted_force_hard_denied`
  - `tests/security/test_w4.py::test_run_shell_preview_distinguishes_argument_boundaries`
- risco: medio: mais `ask` em comandos antes liberados
- rollback: reverter o commit; os testes novos voltam a falhar

## N5. Conteudo vindo do git passa pela politica de leitura
- achados: R2-102 [P1]
- mudanca minima: `git log -p`, `git show <rev>` sem pathspec, `git diff` sem pathspec e pathspec glob passam a `ask`; com pathspec explicito, cada caminho passa por `read`; `git show REV:path` passa `path` por `read` (hoje e so ask)
- testes a adicionar:
  - `tests/security/test_w1.py::test_git_read_only_cannot_print_a_hard_denied_file`
  - `tests/security/test_shell_policy.py::test_git_log_patch_does_not_print_committed_secret_file`
  - `tests/security/test_shell_policy.py::test_git_show_rev_colon_secret_is_hard_denied`
- risco: baixo
- rollback: reverter

## N6. Redacao no limite de saida ao provedor e padroes melhores
- achados: R2-218 [P2], R2-112 [P2], R2-219 [P4], R2-220 [P4], DRIFT-200 [P3]
- mudanca minima: aplicar `redact_text` ao conteudo de tool.result antes de montar a mensagem ao provedor (agent.py), com a mesma lista `register_secret`; padroes: chave JSON entre aspas, userinfo em URL, `Authorization: Basic`, `Cookie:`, PEM de qualquer tipo, `sk_live_`, `glpat-`, `hf_`; limitar entrada (ex.: 1 MB por chamada) e trocar `(?i)secret` por nomes de variavel completos para reduzir falso positivo
- testes a adicionar:
  - `tests/security/test_w5.py::test_tool_results_are_redacted_before_they_reach_the_provider`
  - `tests/security/test_w5.py::test_redact_text_removes_the_secret`
  - `tests/security/test_w5.py::test_ledger_does_not_keep_a_json_secret_read_by_the_agent`
  - `tests/security/test_w5.py::test_redaction_covers_common_secret_formats`
  - `tests/security/test_w5.py::test_json_secrets_in_tool_results_are_redacted_in_the_ledger`
  - `tests/security/test_w5.py::test_redaction_keeps_biomedical_prose`
  - `tests/security/test_w5.py::test_hostile_instructions_file_does_not_stall_prompt_build`
- risco: medio: o modelo deixa de ver valores que antes via
- rollback: flag de configuracao para desligar so a redacao rumo ao provedor

## N7. Sanitizacao no ponto de renderizacao
- achados: R2-221 [P2], R2-306 [P3], R2-310 [P4], R2-222 [P3], R2-223 [P3]
- mudanca minima: um `render_untrusted(text) -> rich.Text` em `panda_core.display` usado por cockpit (transcricao, ToolCard), tabelas do CLI, `print_error`, `panda models`, `validate`; incluir U+2028/2029, U+061C, U+180E, U+00AD, tags U+E0000-E007F
- testes a adicionar:
  - `tests/security/test_w4.py::test_cockpit_tool_card_and_markdown_do_not_emit_terminal_sequences`
  - `tests/test_cli.py::test_study_table_does_not_interpret_corpus_text_as_rich_markup`
  - `tests/test_cli.py::test_rules_table_survives_markup_in_a_workspace_rule`
  - `tests/security/test_w4.py::test_study_show_strips_terminal_control_sequences`
  - `tests/security/test_w4.py::test_study_list_strips_terminal_control_sequences`
  - `tests/test_provider.py::test_list_models_drops_ids_with_control_characters`
  - `tests/test_cli.py::test_models_survives_infinite_created`
  - `tests/security/test_w4.py::test_sanitizer_makes_invisible_or_line_breaking_marks_visible`
  - `tests/security/test_w4.py::test_validation_report_does_not_emit_terminal_sequences_from_validator_output`
  - `tests/security/test_w4.py::test_json_output_escapes_bidi_and_c1`
- risco: baixo
- rollback: reverter

## N8. Fechar lacunas da suite (mutantes, instabilidade, cobertura)
- achados: R2-502 [P3], R2-503 [P3], R2-504 [P3], R2-505 [P3], R2-506 [P4], R2-224 [P3]
- mudanca minima: acrescentar os testes listados; trocar orcamentos de tempo fixos por espera ativa com prazo 10x; `COVERAGE_PROCESS_START` para grep_worker; rodar a mutacao de bolso na CI semanal
- testes a adicionar:
  - `tests/security/test_w2a.py::test_append_refuses_a_tail_whose_hash_differs_from_the_anchor`
  - `tests/security/test_w1.py::test_string_and_argv_paths_agree_in_windows_shlex_mode`
  - `tests/security/test_w1.py::test_destructive_and_exfiltrating_variants_fail_closed[git push -d origin x, git -C . push --mirror, env git push --mirror, git -C . push origin :x, git -C . clean -f, git clean -xdf, env git clean -f]`
  - `tests/test_repl.py::test_bang_command_goes_through_decide_shell`
  - `tests/test_repl.py::test_repl_approval_renders_markup_literally`
  - `tests/security/test_w2a.py::test_append_refuses_tail_with_same_seq_but_different_hash`
  - `tests/security/test_w2a.py::test_long_lived_writer_refuses_same_size_forgery_of_the_tail`
- risco: nenhum
- rollback: n/a

## N9. Contencao de processos e Windows
- achados: R2-107 [P3], R2-318 [P2], R2-320 [P3], R2-317 [P2], R2-211 [P3]
- mudanca minima: Linux: cgroup v2 ou `prctl(PR_SET_CHILD_SUBREAPER)` + varredura de descendentes; Windows: Job Object com `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, resolver executaveis por caminho absoluto fora do cwd, normalizar `git.exe.`/`git.exe `/`::$DATA`/8.3 no classificador, recusar reparse points em `ensure_plain_path`; executar o checklist Windows do AUDIT-REPORT §13
- testes a adicionar:
  - `tests/security/test_w2b.py::test_a_grandchild_that_calls_setsid_does_not_survive`
  - `tests/security/test_w2b.py::test_grandchild_in_new_session_does_not_outlive_timeout (xfail até haver contenção, ou ajustar docs)`
  - `tests/test_process.py::test_windows_which_never_resolves_inside_the_workspace`
  - `tests/test_process.py::test_windows_bare_name_is_resolved_outside_cwd (win32)`
  - `tests/test_process.py::test_windows_grandchild_of_exited_command_is_killed`
  - `tests/security/test_w1.py::test_windows_program_spellings_keep_hard_deny`
  - `tests/security/test_w0.py::test_junction_in_panda_dir_is_refused (skip se não win32)`
- risco: medio
- rollback: reverter por plataforma

## N10. Documentacao
- achados: DRIFT-500 [P3], DRIFT-502 [P4], DRIFT-503 [P4]
- mudanca minima: reescrever SECURITY.md (modelo de ameaca com corpus hostil, controles 0.2, limites conhecidos), corrigir CONTRACT §7 (setsid) e §8 (hard_deny vence; workspace so restringe apos N2), estender test_contract para frases de garantia
- testes a adicionar:
  - `tests/test_contract.py::test_security_policy_describes_the_current_threat_model`
- risco: nenhum
- rollback: n/a

## N11. Demais achados (apos N1–N10)

| id | sev | causa raiz | mudanca minima | teste | risco |
|---|---|---|---|---|---|
| R2-210 | P2 | RC-15 | Tratar o fecho de dependências como control plane | `tests/security/test_w3.py::test_validator_bare_import_cannot_resolve_to_agent_writable_node_modules` | baixo |
| R2-229 | P2 | RC-14 | Incluir cwd e argv exato no pedido | `tests/security/test_w4.py::test_run_shell_approval_shows_the_working_directory` | nenhum |
| R2-300 | P2 | RC-21 | Parar a subida no primeiro diretório cujo dono difere do usuário atual (POSIX st_uid; Windows: dono via GetSecurityInfo ou recusar raiz de volume) e em $HOME; opcional PANDA_CEILING_DIRECTORIES como GIT_CEILING_DIRECTOR… | `tests/security/test_w0.py::test_ancestor_workspace_owned_by_other_user_is_not_adopted` | baixo: workspaces legítimos pertencem a… |
| R2-303 | P2 | RC-21 | Antes de ler instructions, exigir engine.decide('read', relative).effect == 'allow' (piso incluso) e, melhor, restringir instructions a .panda/*.md ou corpus/**/*.md. | `tests/security/test_w0.py::test_config_instructions_cannot_name_a_file_the_read_floor_denies` | baixo |
| R2-305 | P2 | RC-10 | Na init, chamar ensure_plain_path(root, p) antes de ler/copiar/escrever qualquer arquivo de .panda/ e de config.instructions; escrever com atomic_write_* (os.replace troca o link, não o alvo); _backup com copy2(follow_s… | `tests/security/test_w5.py::test_init_does_not_create_files_through_a_dangling_symlink` | baixo |
| R2-105 | P3 | RC-15 | PolicyEngine recebe o caminho do validador e acrescenta hard_deny write/edit para ele e seu diretório | `tests/security/test_w0.py::test_configured_validator_outside_the_default_dir_is_not_agent_writable` | baixo |
| R2-108 | P3 | RC-07 | ampliar o piso e/ou detectar conteúdo (PEM, chaves conhecidas) antes de devolver | `tests/security/test_w1.py::test_common_credential_files_are_not_readable` | baixo |
| R2-111 | P3 | RC-12 | passar !cmd pelo mesmo _log crítico (tool.call antes, tool.result depois) | `tests/security/test_w2a.py::test_operator_shell_route_is_recorded_in_the_ledger` | baixo |
| R2-114 | P3 | RC-07 | allowlist de ambiente (PATH, HOME, LANG, LC_*, TERM, TMPDIR, SYSTEMROOT...) em vez de denylist | `tests/security/test_w3.py::test_child_env_drops_common_secret_variables` | baixo |
| R2-120 | P3 | RC-13 | aceitar [DONE] só no protocolo OpenAI chat | `tests/security/test_w2b.py::test_done_marker_is_not_terminal_for_protocols_that_do_not_use_it` | baixo |
| R2-125 | P3 | RC-14 | recusar aprovação de prévia truncada (ou mostrar a prévia completa paginada / hash + caminho de arquivo temporário) | `tests/security/test_w4.py::test_a_truncated_preview_cannot_be_approved` | baixo |
| R2-209 | P3 | RC-10 | Abrir componente a componente com openat(dir_fd, name, O_NOFOLLOW\|O_DIRECTORY) | `tests/security/test_w0.py::test_parent_directory_swapped_after_lstat_walk_is_not_followed` | baixo |
| R2-215 | P3 | RC-10 | No worker, abrir com O_NOFOLLOW e comparar (st_dev, st_ino) com os valores medidos no pai | `tests/security/test_w2b.py::test_grep_worker_does_not_follow_a_file_swapped_to_a_secret_after_the_policy_check` | baixo |
| R2-225 | P3 | RC-12 | Recusar append após session.end no próprio SessionLog | `tests/test_service_mcp.py::test_service_does_not_append_after_session_end` | baixo |
| R2-226 | P3 | RC-17 | Não podar dentro de corpus/ (ou registrar a lista podada no evento) e invalidar cache por ctime/inode | `tests/security/test_w2b.py::test_tree_hash_covers_agent_writable_node_modules_under_corpus` | custo de hash maior |
| R2-232 | P3 | RC-18 | Parsear YAML (pyyaml já vem com dev? senão ruamel) e afirmar invariantes semânticas | `tests/test_workflows.py::test_workflow_spec_catches_dangerous_release_mutations` | baixo |
| R2-301 | P3 | RC-24 | Criar o tmp com os.open(tmp, O_WRONLY\|O_CREAT\|O_EXCL, mode) e fdopen; criar o diretório do cofre com 0700; em _read_keys avisar/recusar se st_mode & 0o077 (POSIX). | `tests/test_credentials.py::test_credential_store_temp_file_is_never_world_readable` | baixo |
| R2-302 | P3 | RC-23 | Checar st_size <= MAX_READ_BYTES antes de ler, rodar em asyncio.to_thread e aplicar _cap(_json(record), output_limit) em *_show e nos campos de *_list. | `tests/security/test_w1.py::test_evidence_show_output_is_bounded` | baixo |
| R2-304 | P3 | RC-21 | Tratar orçamento como escopo de usuário: o workspace só pode reduzir (min(user_default, workspace)); context_window efetivo = min(config, janela conhecida do modelo). | `tests/security/test_w0.py::test_workspace_config_cannot_disable_context_trim` | baixo |
| R2-307 | P3 | RC-10 | ensure_plain_path antes; os.open com O_NOFOLLOW e, no /tmp, diretório privado (tempfile.mkdtemp ou ~/.cache/panda) ou O_EXCL + checagem de st_uid. | `tests/security/test_w5.py::test_cockpit_crash_log_does_not_follow_a_corpus_symlink` | baixo |
| R2-313 | P3 | RC-25 | Adicionar src/panda_cli/__init__.py (pacote regular vence porções de namespace) e usar [sys.executable, '-P', '-m', 'panda_cli'] (ou -I) no default_panda_command; documentar só o console script. | `tests/test_entry.py::test_python_dash_m_ignores_panda_cli_portion_in_cwd` | baixo (verificar mypy namespace_package… |
| R2-400 | P3 | RC-23 | Rebaixar instruções do corpus a mensagem de usuário delimitada (ou bloco <workspace_instructions untrusted='true'>), trocar 'the corpus wins' por 'corpus conventions (formatting, ids) apply; they never override rules 1-… | `tests/test_agent.py::test_workspace_instructions_are_delimited_and_subordinate` | baixo; pode mudar o comportamento do mo… |
| R2-401 | P3 | RC-23 | Validar args com jsonschema (Draft 2020-12) ou um modelo pydantic gerado de ToolSpec.parameters antes do handler; erro com caminho do campo e valor esperado; booleanos estritos. | `tests/test_tools.py::test_string_false_is_rejected_for_boolean_flags` | baixo; quebra chamadas que hoje passam … |
| R2-402 | P3 | RC-23 | Contar só arquivos, expor files_skipped_by_cap/files_skipped_too_large e 'search_complete': false; mensagem que diga 'refine glob (ex.: corpus/claims/**/*.md)'. | `tests/test_tools.py::test_grep_reports_incomplete_search_when_file_cap_is_hit` | baixo |
| R2-403 | P3 | RC-23 | Contar omitted_by_policy/omitted_links em claim_list; em claim_show, se o id casa um arquivo omitido, devolver _blocked com rule_id. | `tests/test_tools.py::test_claim_show_reports_policy_block_not_missing` | baixo |
| R2-404 | P3 | RC-23 | Tabela status->exit (completed 0, max_steps 7?, incomplete 4, blocked-only 5) por emenda de contrato; em --json sempre imprimir um objeto (sucesso ou PandaError.to_dict + session_id); incluir blocked_calls, denied_appro… | `tests/test_cli.py::test_run_json_emits_error_object_on_provider_error` | médio: mudança de contrato público |
| R2-405 | P3 | RC-12 | approval.decision ganha decided_by: human\|auto\|policy\|mcp e rule_id/effect/risk; run.start/session.start ganham approval_mode. | `tests/test_agent.py::test_auto_approval_is_recorded_as_auto` | baixo (campo novo) |
| R2-406 | P3 | RC-12 | Remover 'approved' do MCP ou registrar como hypothetical=true sem effect=allow; nunca escrever approved=true sem aprovador humano. | `tests/test_service_mcp.py::test_policy_check_approved_flag_never_records_human_approval` | baixo (quebra clientes que usam o flag) |
| R2-407 | P3 | RC-23 | Ou renomear para --ledger e documentar, ou implementar retomada: snapshot de messages (redigido) num checkpoint por turno, referenciado por hash no ledger (padrão LangGraph checkpointer). | `tests/test_cli.py::test_run_session_resume_restores_conversation_or_help_says_ledger_only` | médio (dados sensíveis no checkpoint; r… |
| R2-408 | P3 | RC-23 | Teto por resultado (ex.: 20% da janela ou ctx.output_limit) aplicado no registro para toda ferramenta, com mensagem 'truncated at line N; call read_file with start_line=N+1'; linhas longas cortadas em 2000 chars como no… | `tests/test_tools.py::test_read_file_caps_result_and_says_how_to_continue` | baixo |
| R2-109 | P4 | RC-10 | ler INDEX.md com read_workspace_file(root, path) | `tests/security/test_w1.py::test_study_list_does_not_follow_a_symlinked_index` | baixo |
| R2-110 | P4 | RC-21 | aceitar só provider.name == 'opencode-go' vindo de workspace | `tests/security/test_w0.py::test_workspace_cannot_choose_which_vault_key_is_sent` | baixo |
| R2-115 | P4 | RC-13 | inverter: só {'stop','tool_calls','end_turn',None} completam; o resto é incompleto | `tests/security/test_w2a.py::test_unknown_finish_reasons_do_not_run_tool_calls` | baixo |
| R2-118 | P4 | RC-10 | autorizar read em corpus/studies/INDEX.md em ambos | `tests/security/test_w1.py::test_study_list_obeys_a_read_deny_on_the_registry` | baixo |
| R2-119 | P4 | RC-22 | recusar '\r' solto em new_string ou normalizá-lo | `tests/security/test_w2b.py::test_edit_does_not_introduce_mixed_line_endings` | baixo |
| R2-121 | P4 | RC-22 | capturar Exception também em parsed_arguments | `tests/security/test_w2a.py::test_deeply_nested_arguments_become_a_failed_result` | baixo |
| R2-123 | P4 | RC-13 | reescrever ids duplicados ao montar o corpo de cada protocolo (mapa id->id único por mensagem) | `tests/security/test_w2b.py::test_anthropic_body_has_unique_tool_use_ids_even_if_the_history_repeats_them` | baixo |
| R2-124 | P4 | RC-13 | aplicar o mesmo pareamento no chat | `tests/security/test_w2b.py::test_openai_chat_gets_a_tool_message_for_every_tool_call` | baixo |
| R2-216 | P4 | RC-16 | Mover enumeração+filtro para asyncio.to_thread e usar os.walk com poda de IGNORED_DIRS e limite de visitas | `tests/test_tools.py::test_grep_file_enumeration_does_not_block_the_event_loop` | baixo |
| R2-227 | P4 | RC-12 | Aceitar só eventos plausíveis de crash | `tests/security/test_w2a.py::test_recover_refuses_backdated_or_approval_events` | baixo |
| R2-228 | P4 | RC-16 | Declarar as tools como async e usar anyio.to_thread.run_sync | `tests/test_service_mcp.py::test_long_validate_does_not_freeze_other_mcp_clients` | baixo (serializar acesso ao ledger com … |
| R2-233 | P4 | RC-18 | Reusar o CI como gate do release | `tests/test_workflows.py::test_release_gate_runs_the_ci_matrix_and_audit` | baixo |
| R2-308 | P4 | RC-10 | os.open(path, O_RDWR\|O_CREAT\|O_NOFOLLOW, 0o600) e recusar não-regular. | `tests/test_session.py::test_file_lock_does_not_follow_symlink` | baixo |
| R2-309 | P4 | RC-24 | Remover a duplicata e reutilizar init._backup. | `tests/test_opencode.py::test_backup_does_not_overwrite_same_second` | baixo |
| R2-311 | P4 | RC-16 | Renderizar markdown só no final ou por blocos fechados; durante o stream, Text simples do último trecho. | `tests/test_cockpit.py::test_streaming_render_is_incremental` | baixo |
| R2-312 | P4 | RC-12 | Passar o status do último turno (cancelled/error) e registrar/avisar a falha de close (crash log + mensagem). | `tests/test_cockpit.py::test_quit_after_cancel_records_cancelled` | baixo |
| R2-314 | P4 | RC-19 | Adicionar "/fixtures" ao include do sdist e um job de CI que extrai o sdist e roda pytest. | `tests/test_workflows.py::test_sdist_contains_every_path_tests_read` | baixo |
| R2-315 | P4 | RC-19 | Teste que gera um ledger real (todos os 23 tipos) e valida com format_checker; marcar os 3 schemas sem produtor como 'planned' no CONTRACT. | `tests/test_schemas.py::test_real_ledger_lines_match_event_schema` | baixo |
| R2-316 | P4 | RC-19 | Rodar `python -m panda_cli version` e `panda version` no diretório com shadows + um panda_cli/entry.py hostil. | `tests/test_entry.py::test_module_entry_survives_shadowing_cwd_for_a_subcommand` | baixo |
| R2-319 | P4 | RC-22 | flags \|= getattr(os, 'O_BINARY', 0) em todos os os.open. | `tests/test_session.py::test_ledger_lines_end_with_lf_on_every_platform` | baixo |
| R2-409 | P4 | RC-23 | Mensagem em inglês, marcada como nota do harness, com lista de arquivos lidos/editados e 'files may have changed; re-read before edit_file'; contar schemas no orçamento. | `tests/test_agent.py::test_trim_note_lists_touched_paths_and_is_english` | baixo |
| R2-410 | P4 | RC-23 | Enum = ENFORCED_ACTIONS; campo enforced:bool e tool que aplicaria a ação; para shell, aceitar argv (paridade com run_shell, ver R2-203). | `tests/test_tools.py::test_policy_check_enum_matches_enforced_actions` | baixo |
| R2-411 | P4 | RC-23 | Reescrever descrições como docstring para um colega novo (quando usar, limites, erros comuns, exemplo); description em todo parâmetro; teste que exige description não vazia. | `tests/test_tools.py::test_every_tool_parameter_has_a_description` | baixo |
| R2-412 | P4 | RC-23 | Campo next_step por classe: hard_deny -> 'do not retry by any route; tell the user rule X blocks it'; ask sem aprovador -> 'tell the user this needs approval: rerun interactively'; operator denied -> 'ask the user why b… | `tests/test_tools.py::test_blocked_result_carries_next_step` | baixo |
| R2-413 | P4 | RC-23 | Campo truncated/has_more uniforme; erro 'start_line 99 is past the end (3 lines)'; total semântico documentado. | `tests/test_tools.py::test_list_files_reports_truncation` | baixo |
| R2-414 | P4 | RC-23 | Avisar 'N steps left' na penúltima chamada e fazer um passo final com tool_choice=none pedindo resumo; --max-cost/--max-tokens com status 'budget_exceeded' (padrão mini-SWE-agent). | `tests/test_agent.py::test_max_steps_gives_model_a_final_summary_step` | baixo |
| R2-415 | P4 | RC-23 | Mostrar só o nome da pasta e dizer 'paths are workspace-relative'; trocar a menção a --auto por 'pushing/publishing always needs a person'. | `tests/test_agent.py::test_system_prompt_has_no_absolute_home_path` | baixo |
| R2-416 | P4 | RC-23 | Gerar as ferramentas MCP a partir de ToolRegistry.describe() (uma fonte de schema) e gravar tool.call antes de cada chamada. | `tests/test_service_mcp.py::test_mcp_tool_schemas_match_registry` | baixo |
| R2-417 | INFO | RC-23 | nenhuma ação | `(PoC)` |  |

## Melhorias (depois dos defeitos; ordem da trilha D)

| id | melhoria | custo | fonte |
|---|---|---|---|
| IMP-400 | [#2] Política de shell por argv (estilo Codex prefix_rule) com exemplos match/not_match validados na carga e decisão mais restritiva vence | M | [web: https://learn.chatgpt.com/docs/agent-configuration/rules, retrieved 2026-… |
| IMP-401 | [#1] Política do workspace só pode restringir: regras allow/risk/reason do corpus ignoradas (ou exigem 'trust' explícito do usuário com lista das regras) | S | [web: https://code.claude.com/docs/en/permissions, retrieved 2026-11-03] ('perm… |
| IMP-402 | [#4] never_auto e risco pela classe do efeito resolvido (publish/push/network-send/destructive), não pelo rule_id; --auto aprova só classes listadas | M | [web: https://learn.chatgpt.com/docs/sandboxing, retrieved 2026-11-03] (aprovaç… |
| IMP-403 | [#3] Git com configuração isolada para toda invocação que Panda considera read-only (probe e run_shell allow) | M | [web: https://code.claude.com/docs/en/permissions, retrieved 2026-11-03] (exemp… |
| IMP-404 | [#11] Sandbox de SO opt-in como eixo separado da aprovação (escrita só em corpus/ menos caminhos protegidos, rede desligada) para run_shell e validate | L | [web: https://learn.chatgpt.com/docs/sandboxing, retrieved 2026-11-03] ('The sa… |
| IMP-405 | [#5] Redação também no caminho ferramenta->provedor e guarda de segredos versionados no git | M | [web: https://genai.owasp.org/llm-top-10/, retrieved 2026-11-03] (LLM02 Sensiti… |
| IMP-406 | [#7] Sanitização no sumidouro: um único tipo SafeText obrigatório para todo texto não confiável em Rich/Textual | S | [web: https://genai.owasp.org/llm-top-10/, retrieved 2026-11-03] (LLM05 Imprope… |
| IMP-407 | [#10] ACI: validação de argumentos pelo JSON Schema, tetos por resultado com instrução de continuação, completude explícita, next_step em bloqueios | S | [web: https://www.anthropic.com/engineering/building-effective-agents, retrieve… |
| IMP-408 | [#13] Separação dados/instruções e modos 'Rule of Two' por sessão | S | [web: https://ai.meta.com/blog/practical-ai-agent-security/, retrieved 2026-11-… |
| IMP-409 | [#8] Suíte adversarial de política sem LLM (tabela de casos) + mutação no CI para módulos de segurança | M | [web: https://inspect.aisi.org.uk/approval.html, retrieved 2026-11-03] (polític… |
| IMP-410 | [#18] Checkpoint/retomada do estado do agente e aprovações adiadas no modo headless | M | [web: https://docs.langchain.com/oss/python/langgraph/persistence, retrieved 20… |
| IMP-411 | [#15] Orçamentos e laços: --max-cost/--max-tokens, aviso de passos restantes, passo final sem ferramentas, detecção de 'doom loop' | S | [web: https://raw.githubusercontent.com/SWE-agent/mini-swe-agent/v2.4.6/src/min… |
| IMP-412 | [#12] Proveniência da aprovação: decided_by (human\|auto\|policy\|mcp) e modo de aprovação no run.start; MCP sem parâmetro approved | S | [web: https://pydantic.dev/docs/ai/tools-toolsets/deferred-tools/, retrieved 20… |
| IMP-413 | [#14] Contrato de saída para máquinas: --json sempre emite objeto (sucesso ou erro), tabela status->exit, --json-stream JSONL de eventos, export de trajetória | S | [web: https://raw.githubusercontent.com/SWE-agent/mini-swe-agent/v2.4.6/src/min… |
| IMP-414 | [#24] Descrições de ferramentas como docstring para um colega novo + snapshot do schema | S | [web: https://www.anthropic.com/engineering/building-effective-agents, retrieve… |
| IMP-415 | [#20] Hooks pre/post-ferramenta só no escopo do usuário, deny-first (nunca afrouxam) | M | [web: https://code.claude.com/docs/en/permissions, retrieved 2026-11-03] ('PreT… |
| IMP-416 | [#21] Perfil de workspace/validador plugável com caminhos protegidos derivados do perfil | M | [memory: unverified] |
| IMP-417 | [#22] Ferramentas de escrita tipadas para evidence/claims/numbers com validação antes de gravar (lint-in-edit) | M | [web: https://pypi.org/project/sweagent, retrieved 2026-11-03] [web: https://ar… |
| IMP-418 | [#23] Prontidão para BioMCP: cliente MCP com allowlist, ação network aplicada, conteúdo externo marcado e redigido | L | [web: https://ai.meta.com/blog/practical-ai-agent-security/, retrieved 2026-11-… |
| IMP-419 | [#16] Cadeia de release antes do PyPI: actions por SHA, Trusted Publishing (OIDC) com atestados, job Windows e pip-audit antes de publish, teste semântico dos … | S | [memory: unverified] (PyPI Trusted Publishing/attestations; pinagem por SHA é r… |
| IMP-420 | [#25] Observabilidade: export do ledger para trajetória/OTel GenAI e métricas por turno | M | [memory: unverified] (convenções OpenTelemetry GenAI) |
| IMP-421 | [#29] Risco por ação exibido e registrado (classe determinística; campo opcional do modelo que só pode aumentar o risco) | S | [web: https://docs.openhands.dev/sdk/arch/security.md, retrieved 2026-11-03] (S… |
| IMP-422 | [#26] Âncora do ledger assinada com chave fora do workspace (Ed25519 no escopo do usuário) | M | [memory: unverified] |
| IMP-423 | [#27] Docs: modelo de ameaça com corpus hostil e tabela Rule of Two por superfície; 'regras não são fronteira de segurança' | S | [web: https://code.claude.com/docs/en/permissions, retrieved 2026-11-03] ('isn'… |
| IMP-424 | [#28] Desempenho de busca: poda de IGNORED_DIRS antes de descer (os.walk) e ripgrep opcional | S | [memory: unverified] |
| IMP-425 | [#30] Mapa compacto do corpus (estudos, claims, evidências, números) sob orçamento de tokens | M | [web: https://aider.chat/docs/repomap.html, retrieved 2026-11-03] (repo map ran… |
| IMP-426 | [#17] Job de CI Windows (x64) e checklist WIN-001 automatizado | M | [memory: unverified] |
| IMP-427 | [#19] Contenção de processos por cgroup v2/pidfd (Linux) e Job Object com KILL_ON_JOB_CLOSE (Windows) | M | [memory: unverified] |
| IMP-428 | [#6] Ambiente dos filhos por allowlist, não por denylist | S | [memory: unverified] (prática comum; Codex/Claude Code filtram env em sandbox) |
| IMP-429 | [#9] Tarefas determinísticas de agente com provedor roteirizado (httpx.MockTransport) e trajetórias | M | [web: https://raw.githubusercontent.com/SWE-agent/mini-swe-agent/v2.4.6/src/min… |

## Mutacao de bolso (repetir antes de cada release)

Script usado nesta auditoria: copia a worktree, aplica uma mutacao de uma linha, roda a suite com `PYTHONPATH` na copia e compara com uma rodada de controle. Mutacoes e resultados: `findings.yaml: mutation`. Meta: 0 sobreviventes nao equivalentes.
