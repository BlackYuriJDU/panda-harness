# Design

Panda's interface is a terminal cockpit, not a chat toy. The visual language is
quiet: one surface, two accents, and a mascot that reports state instead of
performing personality. Everything below is a rule for new UI work, not a
description of a mockup.

## Visual system

| Token | Value | Use |
|---|---|---|
| `ink` | `#141414` | App background, approval dialog background. |
| `black` | `#2A2A2A` | Borders, dividers, composer frame. |
| `black.hi` | `#3D3D3D` | Rare emphasis on dark surfaces. |
| `white` | `#F2F2F2` | Primary text, tool names, model id. |
| `white.shadow` | `#C9C9D9` | Secondary text, labels, hints, metrics. |
| `green` | `#2ECC71` | Success, `allow`, active focus, OK tools. |
| `green.dim` | `#1B7F4B` | Completed borders, dim accent text. |
| `amber` | `#F5A623` | `ask`, warning, blocked/failed, risky context. |
| `amber.dim` | `#8A5A12` | Blocked/failed borders. |

**No red.** A blocked action, a failed validation, and a denial all use amber.
Red reads as an alarm and would compete with the mascot; amber reads as "look
here", which is what a fail-closed harness needs. Colour is never the only
signal: every state also has an icon and a word (`blocked`, `failed`, `needs
key`, `unsupported`).

Typography is the terminal's. Panda uses weight and colour for hierarchy, not
font sizes. Numbers are right-aligned in bars and token counts. Paths are shown
workspace-relative; absolute paths appear only in diagnostics.

## Cockpit layout

```text
┌ header ──────────────────────────────────────────────┐
│ PANDA 0.2.0a4   root · branch · session · model      │
├ transcript ─────────────────────────┬ sidebar ───────┤
│ user / assistant markdown           │ mascot         │
│ tool cards (running/ok/blocked)     │ state          │
│ validation panel                    │ effort  ███░   │
│ approval modal (centred, amber)     │ cache   ███░   │
│                                     │ context ███░   │
│                                     │ tokens / cost  │
├ composer ───────────────────────────┴────────────────┤
│ > ask Panda, or /help for commands                   │
└ hints: Enter send · /help · ctrl+q quit ─────────────┘
```

- The header shows root, git branch, session id, provider/model, and the key
  state. It never shows the key.
- The transcript is the only scrolling region. Tool calls become cards; a card
  is `running` (dim arrow), `ok` (green check), `blocked` (amber ×), or `failed`
  (amber !). Long arguments are truncated at 72 characters in the card; the
  ledger keeps the bounded truth.
- Validation is a panel, not a toast: summary counts plus the first two errors of
  each failing check. A failing report keeps `failed` state until the next run.
- The composer stays enabled while a turn streams. A submit during a running
  turn is rejected with a visible notice, never silently queued as consent.
- `/help`, `/model`, `/models`, `/study`, `/status`, `/validate`, `/policy`,
  `/session`, `/clear`, `/quit`, and `!<argv>` are the only commands. `!` goes
  through policy and approval exactly like `run_shell`.

## Responsive breakpoints

| Width | Name | Behaviour |
|---|---|---|
| ≥ 120 | wide | Full sidebar (44 cells), 12-row mascot, cost line, longest header. |
| 100–119 | medium | Sidebar compacts to 20 cells, 7-row mascot, cost hidden. |
| 80–99 | narrow | Sidebar hidden; header normal; hints visible; full-width transcript. |
| < 80 | tiny | Header collapses to one line; hints hidden; transcript only. |

Breakpoints are width thresholds on the app, not media queries. The sidebar is
removed before the transcript is squeezed; the composer never goes below one
usable line. Reflow happens on resize, without restarting the session or losing
scroll position.

## Mascot state machine

The mascot is rasterized from primitives on a 28×24 grid and drawn as 28×12
half-block cells. Cell layers are explicit characters: `W/w` white, `K/k` black,
`G/g/H` green, `A/a` amber; transparency is `.`. No image assets, no font
dependence.

```text
idle ──submit──▶ thinking ──tool call──▶ working ──validate──▶ verifying
  ▲                  │                       │                    │
  │                  └─────────answer────────┴────────ok──────────┘
  │                                          │
  └────────────── success / blocked ◀────────┘
blur ──▶ sleeping ──focus──▶ previous rest state
policy denial / provider error ──▶ alert ──next event──▶ previous state
```

- `success`, `blocked`, and `alert` are resting states: they persist until the
  next user message or tool event, so a finished turn remains readable.
- `blocked` and `alert` are amber; `success` and `working` are green; `sleeping`
  is dim.
- `PANDA_REDUCED_MOTION=1` freezes the frame. Blur also stops the timer; focus
  resumes from the current tick without a jump.

## Animation gates

1. **Fixed timestep.** The animation advances at `12 fps` from a monotonic clock,
   not from render callbacks. Frame index = elapsed × fps.
2. **Catch-up cap.** After a stall, at most 4 frames are applied per tick. The
   mascot never sprints to "catch up" and never drifts into a faster cycle.
3. **Reduced motion.** `PANDA_REDUCED_MOTION` (any non-empty value) disables
   frame advancement entirely; the current pose still renders.
4. **Focus gate.** A blurred app does not animate and switches to `sleeping`.
5. **Cheap redraw.** Only the sidebar is repainted on a frame; the transcript is
   repainted on content changes, not on animation ticks.
6. **No layout shift.** Poses occupy the same 28×12 box. Animation must not
   resize the sidebar or reflow the transcript.

## Telemetry semantics

| Field | Formula | Amber when |
|---|---|---|
| `effort` | `last_steps / max_steps` for the last turn | never by itself; it is context for the step budget |
| `cache` | `cached_tokens / prompt_tokens` | `< 35%` |
| `context` | `context_tokens / context_window` | `≥ 85%` |
| `tokens` | cumulative `total_tokens` | never |
| `cost` | provider-reported `cost_usd` | never; `—` when unreported |
| `model` | bare model id, truncated to 20 chars | never |

`effort` is not progress. It says how much of the step budget the last turn
consumed, so a capped turn is visible as `100%` plus a blocked state. `cache` is
a ratio of reported prompt tokens, not a promise of savings. `context` is an
estimate from the same character budget the agent trims against; it is labelled
as an estimate in `/status`.

## Approval UX

- An `ask` opens a centred modal with an amber border and the title
  `APPROVAL REQUIRED`.
- The dialog shows `tool`, `action` + `resource`, matching `rule`, `risk`,
  `reason`, and a bounded preview of the payload. Paths are workspace-relative.
- Decisions: **Allow** (`y`) or **Deny** (`n`, `Esc`). There is no "always
  allow", no "allow for session", and no bulk approval. Consent is per action.
- `Esc` denies. Denial is a normal tool result
  (`blocked`), not a crash; the agent is told the action was denied and must not
  retry it through another tool.
- Headless `panda run` has no modal: an `ask` is denied unless `--auto` was
  passed explicitly. `--auto` is documented as an operator decision, not a
  default.
- Every request and decision is appended to the ledger before the tool runs, so
  the transcript and the chain agree even if the process dies mid-action.

## Command and composer clarity

Borrowed as principles only, never as code:

- **`sst/opencode`** — a single composer with explicit commands, visible mode
  (`/model`, `/study`), and a consistent "what will run" preview. Panda's `!`
  prefix and `/` commands follow that clarity; the composer is never a hidden
  state machine.
- **`charmbracelet/crush`** — terminal-first, multi-model, session-oriented.
  Panda keeps the session id in the header and the model switch visible in
  `/status` and `/models`; no separate GUI is required to understand state.
- **`stackedlol/ascii-sharks`** — fixed-timestep ASCII animation, reduced-motion
  respect, and explicit cell layers. Panda uses the same discipline for its
  mascot: deterministic grid, capped catch-up, opt-out flag.
- **Textual** — layout, focus, and modal primitives. Panda uses Textual's
  containers and screens; it does not reimplement terminal layout.

## Copy rules

- Verbs are literal: `validate`, `allow`, `deny`, `blocked`, `unsupported`.
- Errors name the thing to fix and the variable or path involved, never a value.
- Empty states say what to do next, not "no data".
- The mascot never speaks in the transcript; its state is label plus icon.
- No exclamation marks in system copy. Enthusiasm is not evidence.
