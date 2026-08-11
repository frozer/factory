You are checking what the `{{REPO_NAME}}` repository *says about itself* against
what is actually true of it. You are not fixing anything and not implementing
anything. Your entire output is one verdict file.

# Why this is worth a run

A project's conventions documents are the obvious place to ground a planner: they
are project-specific, already written, already read by humans. They are also
**prose about intent**, which is the same epistemic status as the spec — and the
spec is the thing this harness ranks *below* files on disk, because an inventory
cut from one cost nine repair commits and three exhausted tasks.

So a convention is a **claim**, not a fact. A true one is worth handing to every
future cutter. A stale one is worse than nothing: it is a confident sentence
that will be believed, and believed sentences that are false are the entire
defect class this factory exists to prevent.

Your job is to tell them apart, with evidence.

# The documents

{{SOURCES}}

# What to extract

Only claims a command or a file could settle. A convention is checkable when it
says what the code does, where something lives, what a tool is configured to do,
or what must never happen — and someone could go look.

Extract, with the sentence quoted:

- **structural** — "loaders live in `ingest/`", "never inline in a request"
- **toolchain** — "tests run with X", "we lint with Y", any command the document
  tells a newcomer to run
- **behavioural** — "the polygon must contain the clicked point", "`ST_Contains`
  against a GiST index, never geohash"
- **prohibition** — "don't hammer it", "not loaded, on purpose"

Skip anything unfalsifiable. "Keep it simple", "write good tests" and "be
careful" are not claims; ruling on them wastes the run and dilutes the output.
A document that yields three checkable claims should produce three.

# How to rule

For each claim: `held`, `violated`, or `unverifiable`, with `evidence` — a
`path:line` you opened, or the command you ran and what it printed.

- **`held`** — you went and looked, and it is true *now*. Not "it sounds right",
  not "the document says so". The document is the thing under test.
- **`violated`** — you went and looked, and it is false or has gone stale. This
  is the valuable output. A `CONTRIBUTING.md` that tells a newcomer to run a
  command that no longer exists is a real defect with a real cost.
- **`unverifiable`** — you could not settle it from this worktree. A real answer.
  Say what you would have needed. Do not downgrade it to `held` because nothing
  looked wrong.

Remember your worktree carries **tracked files only**, so a claim about
gitignored data may be unverifiable here for that reason alone — say so rather
than guessing.

# Propose traps, do not write them

A claim that is `held` **and** load-bearing — one a cutter could get wrong, that
would cost an attempt — is a candidate for `tasks/TRAPS.md`. So is a `violated`
one, phrased as the correction.

Give it as `proposed_trap`: a couple of sentences in the imperative, aimed at
someone writing a packet, naming the `path:line` that decides it. Most claims do
not deserve one; a trap that restates something obvious costs context on every
future cut forever. Propose the few that would have changed what somebody built.

**You may not edit `tasks/TRAPS.md`.** An operator promotes these. A run that can
edit what grounds every future cut will eventually quietly fix something instead
of reporting it.

# Write your verdict

Write **`{{RESULT_PATH}}`**, exactly at that path, as valid JSON:

```json
{
  "claims": [
    {"source": "api/README.md",
     "quote": "loaders are offline and live in `ingest/` — never inline in a request",
     "kind": "structural",
     "status": "held",
     "evidence": "api/ingest/adrh_load.py:1 — every loader is a module under ingest/; no router imports one",
     "proposed_trap": "Ingestion belongs in `api/ingest/`, never in a request path. A packet that adds a loader to a router is wrong however green it runs."},
    {"source": "CONTRIBUTING.md",
     "quote": "run `make test` before pushing",
     "kind": "toolchain",
     "status": "violated",
     "evidence": "no Makefile in the repository root; the suite is `uv run --directory api pytest -q`",
     "proposed_trap": "There is no Makefile. The suite is `uv run --directory api pytest -q`."}
  ]
}
```

Rules the harness enforces:

- `claims` is a non-empty list. If a document genuinely yields nothing
  checkable, say so with one `unverifiable` entry quoting its most
  convention-like sentence — an empty result is indistinguishable from a run
  that did not look.
- `source` must be one of the documents listed above.
- `kind` is `structural`, `toolchain`, `behavioural` or `prohibition`.
- `status` is `held`, `violated` or `unverifiable`.
- `held` and `violated` both require `evidence`. A ruling without a location is
  not a ruling.
- `proposed_trap` is optional and should stay rare.
