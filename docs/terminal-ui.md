# The terminal UI

What each element on screen is, every key it answers to, and what it degrades
to when the terminal cannot keep up. Screens and keystrokes here match what
ships; if a picture and the code disagree, the code wins and this page is a
bug.

## The opening screen

`jaigent chat` draws the wordmark before anything else. On a real terminal with
colour, unicode and enough width, a bright edge sweeps across it once (about
half a second) and settles into exactly the static logo, which is then printed
in the banner panel:

```
     ██╗  █████╗  ███████╗  ██████╗  ███████╗ ███╗   ██╗ ████████╗
     ██║ ██╔══██╗ ╚═██╔══╝ ██╔════╝  ██╔════╝ ████╗  ██║ ╚══██╔══╝
     ██║ ███████║   ██║    ██║  ███╗ █████╗   ██╔██╗ ██║    ██║
██   ██║ ██╔══██║   ██║    ██║   ██║ ██╔══╝   ██║╚██╗██║    ██║
╚█████╔╝ ██║  ██║ ███████╗ ╚██████╔╝ ███████╗ ██║ ╚████║    ██║
 ╚════╝  ╚═╝  ╚═╝ ╚══════╝  ╚═════╝  ╚══════╝ ╚═╝  ╚═══╝    ╚═╝
                    all your agents in one place   v0.5.8
Using AGENTS.md, CLAUDE.md for this project · JAIGENT_INSTRUCTIONS=0 to ignore
Ready when you are. Ask me to read, explain or update your files.
```

The sweep is decoration, so it is the first thing to go when anything is
unusual:

| Situation | What happens |
| --- | --- |
| stdout is a pipe or file | no animation, no escape codes |
| `--no-animation` | no animation |
| `JAIGENT_NO_ANIMATION=1` | no animation |
| `CI` set, or `TERM=dumb` | no animation |
| `--no-color` | no animation (colour is what it draws with) |
| terminal narrower than 44 columns | no animation |
| console cannot encode the block glyphs | no animation |
| Ctrl-C during the sweep | stops, leaves the static logo |
| anything raises inside it | caught; the session continues normally |

Either way the static banner is printed afterwards, so the screen ends up
identical. `JAIGENT_INSTRUCTIONS=0` only hides the file list; it does not
affect the animation.

## The anatomy of a turn

```
❯ summarise notes.md into summary.md        ← your prompt
  → Reading files · notes.md  ✓             ← tool trace (one line per call)
  → Searching the web · jaigent release notes  ✓
⠋ Thinking…  ▰▰▱▱▱                        4s · ↑ 1.2k tokens   ← live status line
                                             (erases itself; counters pinned right)

Done. summary.md now holds the…              ← the answer, rendered live as markdown
· 3 tool calls · 4,120 tokens (3,800 in / 320 out) · ~$0.0008 · 6.2s   ← the footer
  ← undo 2 files: jaigent undo  (or /revert in chat)
```

## The chat box

The prompt is a frame, not a bare `❯`:

```
╭──────────────────────────────────────────────────────────╮
│ ❯ Summarise what changed in the tools and write notes.md │
╰ gpt-4o · auto ────────────────────────────────── Enter: send ╯
```

While a turn runs the frame dims, a highlight travels along its top border,
and the footer changes what it answers: the model is already on the status
line above, so the same space goes to what is waiting and to what Enter does.

```
                                                       queued │
╭──────────────────────────────────────────────────────────╮
│ ❯ Type ahead: Enter queues it for the next turn          │
╰ 2 queued ───────────────────────────────────── Enter: queue ╯
```

That `queued` note is an acknowledgement, not a record: it appears inside the
frame for a couple of seconds and goes, taking no row from the transcript.

The footer sheds pieces whole as the terminal narrows (the label, then the
queue count, then the model) rather than cutting a word in half, and the
corners never move. Width is measured in cells, so a line of CJK or emoji
keeps the frame square, and a resize is noticed within a quarter second.

The line is editable with the keys you already expect: `←` `→`, Home/End,
Ctrl-`←`/`→` (or Alt-B/Alt-F) by word, Backspace/Delete, Ctrl-W to kill a
word, Ctrl-U and Ctrl-K to kill to either end, Ctrl-A/Ctrl-E for the line
ends. `↑`/`↓` (or Ctrl-P/Ctrl-N) walk the history of what you sent, and come
back to whatever you were in the middle of writing. The box is one line tall
however long the line is: it scrolls under the cursor instead of growing,
because a box that gains a row per keystroke walks the transcript off the top
of the screen.

Empty Enter sends nothing, so pressing it repeatedly is free. End a line with
`\` to keep typing only in the plain fallback; the box gives you the full
line. Leave with Ctrl-D (twice, or on an empty line), Ctrl-C on an empty box,
or `/exit`. Anything starting `/` is a command (see `/help`); paths like
`/tmp/notes.md` are not: they are ordinary prompts.

### Typing while the agent works

Enter during a turn **queues** the line. It waits in order and is sent as
soon as the current turn ends, one turn each, so a thought that arrives
mid-answer is not lost:

```
you start the long job
you stop and do this instead (queued, sends next)
⠹ Thinking…  ▰▰▱▱▱                                3s
```

* **Ctrl-C** stops the turn at the next safe point *and* sends what you have
  typed. A turn that only stops leaves a session that looks dead; a turn that
  stops and immediately picks up your correction is what you meant.
* **Esc** on an empty box takes the newest queued message back into it,
  editable.
* A half-written *answer* is cut, not kept in the history: the model would
  otherwise be handed a sentence that breaks off mid-thought and told to
  continue it. Tool calls that finished stay.

While a turn runs the box stays exactly where it is, framed dimmer, with
`Enter: queue` in its footer; the footer counts what is waiting. Nothing is
locked and nothing typed is discarded. The old behaviour, where the keyboard
was silenced and the typeahead flushed, is gone.

There is no keyboard legend under the box: `/help` has it, and a permanent
cheat sheet under the prompt is clutter in the place a person looks all day.
The motion follows the same rules as the splash: `--no-animation`,
`JAIGENT_NO_ANIMATION`, `CI` and `TERM=dumb` leave the frame still. A window
that is resized is redrawn at the new width without waiting for a keystroke.

Everything here is drawn by `src/jaigent/composer.py` with no new
dependency: raw keys from `termios`/`msvcrt`, one `rich` Live region holding
the status line, the streaming answer and the box. Where it cannot run
(a pipe, `--no-color`, a console that refuses cbreak), the plain prompt comes
back unchanged, `\` continuation and all, so scripts and schedules are
unaffected.

## The status line

While the model works, one line redraws in place: a spinner, the current
verb, and, pinned to the right edge, elapsed time and the token count so
far. When a tool runs, the verb becomes the action and its target:

```
⠙ Reading files… · src/app.py                            2s · ↑ 15.3k tokens
⠹ Searching the web… · python 3.13 release date          7s
⠸ Thinking…  ▰▱▱▱▱                                4s · ↑ 1.2k tokens
```

The idle verbs (`Thinking`, `Orbiting`, `Weaving`, …) rotate from a pool in
`src/jaigent/ui.py` as `PHRASES`. On a narrow terminal the right side is
dropped a piece at a time, never wrapped: a status line that wraps leaves a
stale row behind on every frame. At any moment exactly one thing owns the
screen (the status line, a live answer block, the chat box, or a question
panel), and each
one yields cleanly to the next, so the spinner keeps spinning during tools
even after text has already streamed.

## The tool trace

Every finished tool call leaves one quiet line: the action, the short target,
and the outcome. `✓` when it worked, `✗` when it returned an error the model
will read and recover from. This is the record of what the agent actually
did; `--verbose` trades it for the full argument dumps.

## The task plan

For multi-step work the model keeps a checklist with `write_todos`, and every
update prints the whole plan while the agent works: the header counts what
is done, `✓` marks finished tasks, `◉` the one in progress, `○` the rest:

```
  → Plan · 1 of 3 done  ✓
    ✓ Scaffold the routes
    ◉ Wire up login
    ○ Add tests
```

## Streaming

Answers render as markdown live, while each chunk arrives, so a code fence or
table takes shape on screen instead of flashing as raw markup first. A reply
that narrates and *then* calls a tool ("Let me check the files…") suspends
its live block for the tool and resumes below a blank line, so narration and
answer never run together. `--no-stream` waits for the full reply instead.

## What goes on which stream

The answer is the payload; the trace lines, the footer and the limit panels
are progress about getting to it. On a terminal there is one stream, so they
sit together. When stdout is a pipe they separate: the answer goes to stdout
and everything else to stderr, so `jaigent "…" > answer.md` writes a file
containing the answer and nothing else. Piped output is never rendered as
markdown either, so the file gets the source.

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

What counts as mutating is the tool's own declaration, not a fixed list of
names: anything that has not declared itself read-only is asked about. A
plugin that writes shows its name and arguments instead of a diff, because
there is no file path to diff against:

```
╭──────────────────── plugin_write ─────────────────────╮
│ run plugin_write                                      │
│ path='notes.txt'                                      │
╰───────────────────────────────────────────────────────╯
```

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
| `1` to `6` | jump straight to that option and pick it |
| `Enter` | confirm the highlighted option |
| `Esc` / `e` / `Tab` | type your own answer instead |
| `Ctrl-C` | cancel the question, and the run, like quitting an approval |
| `Ctrl-D` | close the prompt; the model proceeds with its best judgment |

An empty typed answer means "use your judgment"; the model is told exactly
that, in the same words the non-interactive hosts get.

## Degradation: the contract

The UI must never crash over what a terminal cannot print. Every decoration
has an ASCII fallback, chosen by what the output stream can actually encode:

| You have | What you get |
| --- | --- |
| A modern UTF-8 terminal | everything above, in colour |
| `--no-color` | the same layout, unstyled; no animation, raw streaming text |
| A legacy Windows code page (cp1252 …) | `→` becomes `->`, `✓` becomes `OK`, `●`/`○`/`◉` become `(*)`/`( )`/`(+)` |
| A pipe instead of a tty | plain text, no spinner, no live rendering; the answer on stdout, progress on stderr |
| No tty on stdin (`serve`, schedules) | `ask_user` never prompts; the model is told nobody can answer |
| MCP | `ask_user` is not offered at all, since no user sits behind the protocol |

Non-interactive hosts force approvals to `auto` (or refuse them, in dry-run)
for the same reason: there is no keyboard to consult, so pretending otherwise
would hang the run.

## Quick reference

| Where | Keys |
| --- | --- |
| Chat box | Enter send · arrows/Home/End/word jumps edit · `↑`/`↓` history · empty Enter nothing · Ctrl-D / `/exit` leave |
| During a turn | Enter queues the line · Ctrl-C stops the turn and sends it now · Esc retracts the newest queued line |
| Approval prompt | `y` / `n` / `a` / `q` |
| `ask_user` picker | arrows · digits · Enter · Esc · Ctrl-C |
| In chat, any time | `/help` lists every command with a one-line effect |
