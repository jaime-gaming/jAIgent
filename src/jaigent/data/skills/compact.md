---
name: compact
description: Shrink a long conversation so later turns still fit. Load when the chat is getting long or the user says compact.
---

The conversation is getting long. Compact it without losing the useful bits.

1. Keep the user's current goal, any file paths you already touched, and decisions already made.
2. Drop greetings, failed tool calls that were later corrected, and repeated file dumps.
3. If the user is in chat, tell them they can run `/compact` to collapse older turns into a short summary. That is the real shrink; this skill is the briefing.
4. After compacting, do not re-read files you summarised unless you need a line you no longer have.

`/compact` in chat (or `auto_compact` in settings) does the mechanical work.
This skill tells you *what* to preserve.
