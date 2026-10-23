# Panda Harness

> O harness de terminal para pesquisa com evidência **fail-closed**.

**Status:** design / pré-alpha. Ainda não há código — o plano completo será publicado em `docs/PLAN.md`.

## O problema

Projetos como o [`dcj-study`](https://github.com/BlackYuriJDU/dcj-study) produzem reanálises independentes de dados públicos com uma regra dura:

> nenhum número sem evidência; nenhum claim sem `study_id` e `track`; nenhum release com payload de fonte restrita.

Hoje essa disciplina é mantida por convenção, validadores e revisão humana. O Panda transforma essa disciplina em **superfície de ferramenta**: o agente não consegue escrever um claim que não resolve, nem publicar um release que não fecha.

## O que é

Um agente de terminal (CLI/TUI) especializado em trabalhar com um **corpus de evidência**:

- entende o registry de estudos (`corpus/studies/`);
- escreve evidência resolvível (`source_path:source_line` + hash + licença + track);
- valida claims, números e releases de forma fail-closed;
- roda o pipeline com proveniência (`run_id`, ambiente, hash);
- consulta literatura e bases biomédicas (BioMCP / PubMed / ClinVar / gnomAD / GWAS Catalog);
- revisa com um verificador independente e read-only;
- nunca deixa um release passar se o ledger não fecha.

## O que não é

- Não é um "AI scientist" autônomo (Kosmos, Robin, Co-Scientist).
- Não é um SaaS de revisão de literatura (Elicit, Consensus, Scite).
- Não é um agente de código genérico (Claude Code, OpenCode, Codex).
- Não é um ELN (Benchling).

## Invariante central

```
Se não resolve para uma linha, um script, um run e uma licença, não publica.
```

## Comandos planejados

| Comando | Função |
|---|---|
| `panda` | sessão interativa (TUI) |
| `panda run "<tarefa>"` | execução headless com trace |
| `panda validate [--release]` | roda os checks fail-closed |
| `panda evidence add/show` | cria/inspeciona evidência resolvível |
| `panda claim check` | valida claim contra `evidence_ids` / `number_ids` |
| `panda pipeline run <script>` | executa com `run_id`, ambiente e hash |
| `panda lit search "<query>"` | literatura via BioMCP |
| `panda review` | verificador independente read-only |
| `panda release build` | manifesto + hashes + public-face |

## Arquitetura em duas camadas

1. **`panda-core`** — biblioteca + servidor MCP com ferramentas tipadas e transacionais (`evidence_add`, `claim_add`, `validate`, `pipeline_run`, `release_build`). É onde a política vive.
2. **`panda`** — CLI/TUI que usa o core e também pode ser plugada em Claude Code, Codex ou OpenCode via MCP.

Assim, a garantia não depende do prompt: depende da ferramenta.

## Licença

[MIT](LICENSE) © 2026 Arthur Araújo da Silva.
