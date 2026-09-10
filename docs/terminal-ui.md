# The terminal UI

What each element on screen is, every key it answers to, and what it degrades
to when the terminal cannot keep up. Screens and keystrokes here match what
ships; if a picture and the code disagree, the code wins and this page is a
bug.

## The anatomy of a turn

```
❯ summarise notes.md into summary.md        ← your prompt
  → Reading files · notes.md  ✓             ← tool trace (one line per call)
  → Searching the web · jaigent release notes  ✓
⠋ Thinking…  ▰▰▱▱▱                        4s · ↑ 1.2k tokens   ← live status line
                                             (erases itself; counters pinned right)

Done. **summary.md** now holds the…          ← the answer, redrawn as markdown
· 3 tool calls · 4,120 tokens (3,800 in / 320 out) · ~$0.0008   ← the footer
```

## The prompt line

`❯` (or `>` on a console that cannot print it). Empty Enter sends nothing —
pressing it repeatedly is free. End a line with `\` to keep typing; the
continuation marker is `…`. Leave with Ctrl-D, Ctrl-C at the prompt, or
`/exit`. Anything starting `/` is a command (see `/help`); paths like
`/tmp/notes.md` are not — they are ordinary prompts.

## The status line

While the model works, one line redraws in place: a spinner, the current
verb, and — pinned to the right edge — elapsed time and the token count so
far. When a tool runs, the verb becomes the action and its target:

```
⠙ Reading files… · src/app.py                            2s · ↑ 15.3k tokens
⠹ Searching the web… · python 3.13 release date          7s
```

The idle verbs (`Thinking`, `Orbiting`, `Weaving`, …) rotate from a pool in
`src/jaigent/ui.py` — `PHRASES`. On a narrow terminal the right side is
dropped a piece at a time, never wrapped: a status line that wraps leaves a
stale row behind on every frame.

## The tool trace

Every finished tool call leaves one quiet line: the action, the short target,
and the outcome. `✓` when it worked, `✗` when it returned an error the model
will read and recover from. This is the record of what the agent actually
did; `--verbose` trades it for the full argument dumps.

## Streaming

Answers print as raw markdown the moment each chunk arrives — a code fence is
only visible once it closes. When the stream ends, the raw text is erased and
redrawn as rendered markdown in the same place. Piped output
(`jaigent "…" > answer.md`) is never redrawn, so the file gets the source.
`--no-stream` waits for the full reply instead.

## Approvals

A mutating tool call (write, edit, delete, shell) in `ask` mode interrupts
with a unified diff of what is about to change:

```
╭───────────────────── write_file ─────────────────────╮
│ summary.md  new file                                 │
│ --- a/summary.md                                     │
│ +++ b/summary.md                                     │
│ @@ -0,0 +1,3 @@                                      │
│ +# Summary                                           │
╰──────────────────────────────────────────────────────╯
Apply this change? [y]es / [n]o / [a]lways / [q]uit:
```

`a` stops asking for that tool for the rest of the run; `q` cancels the run.
The snapshot for `undo` is taken *before* this prompt, so approving and then
reverting is always possible.

## The `ask_user` picker

When the model asks a question with options, it renders as a panel you answer
with the keyboard instead of typing numbers:

```
╭─────────────────── jAI has a question ───────────────╮
│ Which database should the migration target?          │
│                                                      │
│     ○ PostgreSQL                                     │
│   ❯ ◉ SQLite (local file)                            │
│     ○ MariaDB                                        │
│                                                      │
│ ↑↓ move · 1-3 pick · enter confirm · esc own answer  │
╰──────────────────────────────────────────────────────╯
```

The selected row carries the `❯` pointer and a marker that slowly pulses, so
the prompt reads as waiting rather than frozen. Once answered, the whole panel
collapses into one line that outlives it:

```
✓ Which database should the migration target? → SQLite (local file)
```

Open questions (no options) skip the picker and go straight to a typed
answer, with the question echoed on the prompt line so it is not lost.

### Keys

| Key | Action |
| --- | --- |
| `↑` / `↓` | move the pointer (wraps at both ends) |
| `j` / `k` | same, for vim hands |
| `1`–`6` | jump straight to that option and pick it |
| `Enter` | confirm the highlighted option |
| `Esc` / `e` / `Tab` | type your own answer instead |
| `Ctrl-C` | cancel the question — and the run, like quitting an approval |
| `Ctrl-D` | close the prompt; the model proceeds with its best judgment |

An empty typed answer means "use your judgment"; the model is told exactly
that, in the same words the non-interactive hosts get.

## Degradation — the contract

The UI must never crash over what a terminal cannot print. Every decoration
has an ASCII fallback, chosen by what the output stream can actually encode:

| You have | What you get |
| --- | --- |
| A modern UTF-8 terminal | everything above, in colour |
| `--no-color` | the same layout, unstyled; no animation, no redraw |
| A legacy Windows code page (cp1252 …) | `→` becomes `->`, `✓` becomes `OK`, `●`/`○` become `(*)`/`( )` |
| A pipe instead of a tty | plain text, no spinner, no redraw — safe to redirect |
| No tty on stdin (`serve`, schedules) | `ask_user` never prompts; the model is told nobody can answer |
| MCP | `ask_user` is not offered at all — there is no user behind the protocol |

Non-interactive hosts force approvals to `auto` (or refuse them, in dry-run)
for the same reason: there is no keyboard to consult, so pretending otherwise
would hang the run.

## Quick reference

| Where | Keys |
| --- | --- |
| Chat prompt | Enter send · `\` continue · empty Enter nothing · Ctrl-D / `/exit` leave |
| During a turn | Ctrl-C interrupts the turn, not the chat |
| Approval prompt | `y` / `n` / `a` / `q` |
| `ask_user` picker | arrows · digits · Enter · Esc · Ctrl-C |
| In chat, any time | `/help` lists every command with a one-line effect |
