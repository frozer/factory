# Operating the factory

For an agent working in a repository that has vendored this harness at
`factory/`. This file is about *driving* it. `README.md` is about why it is
built the way it is; read that before changing anything here.

## The order the subcommands go in

```
./factory/run.py ground        # first, once — writes tasks/GROUNDING.md
./factory/run.py conventions   # optional; the one part of grounding that costs a model
./factory/run.py cut --scope "…"   # propose packets into tasks/, untracked
./factory/run.py assess B01    # rule on each one
./factory/run.py sync          # register assessed packets into the queue
./factory/run.py plan          # resolved order; invokes no models
./factory/run.py run           # drain the queue
./factory/run.py status
./factory/run.py approve B01   # merge a task held for human review
./factory/run.py reset B01     # wipe a task back to ready, worktree and all
```

## Things that will otherwise surprise you

- **`cut` and `assess` refuse to start ungrounded.** Run `ground` first. It
  invokes no model and is re-runnable; `--check` re-measures without rewriting.
- **`cut` writes packets untracked.** Nothing it produces is queued. The
  operator assesses and commits.
- **`sync` refuses a packet without a `ready` assessment** pinned to its current
  `sha256`. Editing a packet invalidates its assessment. `--allow-unassessed`
  is the escape hatch and is a deliberate act, not a workaround.
- **The harness overrides both the reviewer and the assessor.** A `ready` or
  `accept` that contradicts its own evidence is downgraded and the reason
  recorded. Do not try to satisfy the verdict field; satisfy the evidence.
- **`tasks/TRAPS.md` is off-limits to `cut`.** `_cut_output` refuses that path.
  A new trap is reported under **New traps** in the cut report; an operator
  promotes it.
- **The repository must be clean and on the target branch when `run` starts.**
  That is where merges land.

## Tests

```
python -m unittest discover -s factory -t factory
```

Stdlib only, no virtualenv, no network. They must be green before anything in
`factory/` is committed. There is no test for prompt *quality* — only that the
prompts compose and that every failure class cited by name is defined.
