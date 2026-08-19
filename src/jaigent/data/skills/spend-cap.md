---
name: spend-cap
description: Stay inside a dollar budget for this run. Load when the user mentions cost, budget or spend.
---

The user has a spend cap. Honour it.

1. Call fewer tools. Prefer one search over five. Do not re-read a file you already have.
2. Prefer cheap models when the user has not named one (`--model auto` / `free`).
3. After each tool batch, glance at the cost line. If you are close to the cap, stop calling tools and answer with what you have.
4. Never start a long research loop "just to be thorough" when a budget is set.
5. If you cannot finish inside the cap, say so in one sentence and list what is left.

The hard stop is enforced by jaigent itself when `JAIGENT_BUDGET` (or
`jaigent settings set budget 0.50`) is greater than zero. This skill is the
soft side: spend less *before* the run is killed.
