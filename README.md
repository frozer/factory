# The Factory

Drains a queue of task packets: each one is implemented by a model in an
isolated git worktree, verified by gates the harness runs itself, reviewed
against the packet's declared invariants, and squash-merged onto the feature
branch if it survives all three.

Ahead of the queue sits a planning tier — `cut` and `assess` — because the one
failure the loop cannot absorb is a packet that is *wrong about the world*. That
fails identically on every attempt, since the retry hands the model the same
wrong packet back. `max_attempts` is a budget for a model having a bad run, not
for a defective packet.

Stdlib only — run it with a bare `python3`. It orchestrates the surfaces of the
repository it is vendored into and deliberately does not live inside any of
their virtualenvs. Workers are the `claude` CLI in headless mode.

```
./factory/run.py ground        # measure what a planning worktree will contain
./factory/run.py conventions   # check what the project claims about itself
./factory/run.py cut --scope "§5, the income section and its loader"
./factory/run.py assess B01    # rule on one, before it can be queued
./factory/run.py sync          # register tasks/*.md into the queue
./factory/run.py plan          # resolved order; invokes no models
./factory/run.py run           # drain the queue
./factory/run.py run --once
./factory/run.py status        # the board
./factory/run.py approve B01   # merge a task held for human review

python -m unittest discover -s factory -t factory   # the harness's own tests
```

## What it assumes about your project

Be warned before you land it: **the toolchain is not yet configurable.** This
harness was built for, and has only ever run against, a repository with two
surfaces:

| Surface | Directory | Manifest | Install | Gates |
|---|---|---|---|---|
| `api` | `api/` | `pyproject.toml` | `uv sync` | `ruff check`, `ruff format --check` (diff-scoped), `pytest` |
| `webapp` | `webapp/` | `package.json` | `npm ci` | `npm run lint`, `npm run build` |

If that is your layout, it works today. If it is not — a single-package Python
repo, `backend/` instead of `api/`, Go, a different test runner — the harness
will report an environment fault on the first gate, and adapting it means
editing these, in this order:

| File | What is baked in |
|---|---|
| `gitops.py` — `SURFACES` | directory, manifest, install command |
| `gates.py` — `run_checks` | the gate set, branched on the surface name |
| `packet.py` — `SURFACES` | which surface names a packet may declare |
| `agent.py` — `TOOLS_IMPL` | the runner each surface's implementer is allowed to call |
| `ground.py` — `probe_toolchain` | the two measurements `ground` takes |
| `run.py` — `verify_command`, `format_note`, the baseline path | what the packet tells the model to run |
| `prompts/cut.md`, `prompts/assess.md`, `prompts/implement.md` | the toolchain appears in prose, and this is the hard half |

The prompt half is genuinely hard, not a string substitution: `cut.md`'s
guidance on keeping a planted fixture gate-clean is ruff-specific *reasoning*
about import sorting, not a command that can be swapped out. A configuration
file for surfaces is the obvious next step; it is not here yet, and claiming
otherwise in this file would be an instance of exactly the defect the rest of
the design is about.

## Landing it in a project

The harness expects to sit at `factory/` in the repository it works on, with
`tasks/` beside it at the root. Vendor it with `git subtree`:

```
git subtree add --prefix factory git@github.com:frozer/factory.git main --squash
git subtree pull --prefix factory git@github.com:frozer/factory.git main --squash
```

Then add `.factory/` to your `.gitignore`, and run `ground`.

Committing the harness rather than gitignoring it is deliberate, and it is the
same argument the design makes everywhere else. Assessments, grounding and traps
are in git because a gate held only in machine-local state would let a fresh
clone queue a packet nobody vetted. The **prompts are the other half of that
pair**: an assessment pinned to a packet's `sha256` records what was ruled on,
and nothing at all about which `assess.md` ruled on it. An untracked harness
gives a fresh clone a vetted packet and an unknown vetter.

Not a submodule: `git worktree add` does not check submodules out, and this
harness lives or dies on what a worktree contains.

## Content in git, state in SQLite

| | Where | Why |
|---|---|---|
| Packets | `tasks/*.md`, in git | Immutable content, pinned to the spec commit it was cut from, diffable when the spec is amended |
| Planted files | `tasks/supplied/<ID>/`, in git | Mirrors the repo layout; copied into the worktree *before* the model runs |
| Assessments | `tasks/assessments/<ID>.json`, in git | The ruling that lets a packet be queued at all, pinned to its `sha256`. In git because `sync` gates on it, and a gate held only in machine-local state would let a fresh clone queue a packet nobody vetted |
| Grounding | `tasks/GROUNDING.md`, in git | What was measured about the repository the harness landed in, plus the one answer no probe can supply — which paths the planning tier must be able to read. `cut` and `assess` refuse to start without it |
| Project traps | `tasks/TRAPS.md`, in git | What this repository has taught the harness. Composed into every `cut` and `assess` prompt, so a fresh clone must not plan against an appendix nobody vetted — the same argument as assessments |
| State | `.factory/factory.db`, gitignored | Status, attempts, review findings, cost — mutable, append-only, queryable |
| Transcripts | `.factory/runs/<ID>/attempt-N/` | Prompts, raw CLI output, the diff, the gate report |
| Board | `tasks/README.md`, in git | Regenerated after every merge; the durable summary of machine-local state |

State is not in git because the worker agents commit to this same repository.
Tracking progress there would put bookkeeping and work in the same history, and
a rejected attempt would drag its own commits behind it forever.

## The loop

```
ready ──claim──▶ running ──▶ gating ──green──▶ reviewing ──accept──▶ merging ──▶ done
  ▲                            │                    │                    │
  └────── needs_work ◀─────────┴── red ──────┴── revise ──┘         awaiting_human
                 │
                 └── attempts exhausted ──▶ blocked ──▶ the loop halts
```

Per task: claim → check the spec has not moved → create a worktree → measure the
test baseline → plant supplied files and commit them → implement → gate →
review → merge.

## Four things the harness does not delegate

**It runs the gates itself.** The agent's `verification` block is recorded and
never believed. The gate commands are executed by `gates.py` in the worktree,
and its parsed result is what decides whether the work reaches review.

**It plants the tests.** Anything under `tasks/supplied/<ID>/` is copied in and
committed before the model starts, then declared off-limits. `git diff` against
that commit proves the model made the test pass rather than making it agree.
That turns "here is the test, make it pass" into something enforceable.

**It overrides the reviewer.** A reviewer can be talked into `accept` by work
that looks right. It cannot talk the harness out of a red gate, a violated
critical invariant, or a `blocker` finding — `contracts.effective_verdict`
downgrades all three, and records why. A pattern of over-accepting reviewers
shows up in `reviews.override_reason` rather than in folklore.

**It refuses a review that skipped a question.** Every packet declares
invariants by id; the reviewer must return `held` / `violated` / `unverifiable`
for each one, with a `file:line`. Omitting one is rejected by the contract, not
read as a pass. `unverifiable` is a real answer and blocks a critical invariant
— otherwise it becomes the safe non-answer.

## Packet format

TOML frontmatter delimited by `+++` (stdlib `tomllib`; it parses the nested
`[[invariants]]` array properly, which a hand-rolled YAML subset would not).
The body is handed to the implementer verbatim and **must be self-contained** —
a packet that says "see the spec" sends a small model into a 1,257-line design
document with no budget left for the task.

```toml
+++
id = "B01"                     # also the filename prefix: tasks/B01-adrh-loader.md
slug = "adrh-loader"
goal = "load the ADRH income CSV"
tier = "basic"                 # basic | standard | advanced
reviewer = "standard"          # optional; defaults one tier up, never weaker
gate = "auto"                  # 'human' holds the merge for an operator
surface = "api"                # picks the gate set
spec_commit = "504abb9..."     # drift check runs before the task starts
spec_path = "docs/IMPLEMENTATION-S3-reporting-baseline.md"
needs_db = false               # adds `pytest -m db`
requires = ["S08a"]
forbidden_paths = ["api/tests/conftest.py"]

[[invariants]]
id = "thousands-separator"
critical = true
assert = "16.429 loads as 16429 — the dot is a thousands separator"
+++
```

## Cutting packets

Packets are cut by a model too, from `prompts/cut.md`, and assessed by a second
one from `prompts/assess.md` before they are allowed near the queue — the
planning-side counterparts of `implement.md` and `review.md`.

They exist because the first inventory cut from a spec cost nine repair commits
and three tasks that burned every attempt. All nine were one mistake: **the
packet asserted something nobody had checked against anything but the spec's
prose** — filenames and encodings that were not on disk, a step sequence the
real API could not express, an invariant demanding fields its own type did not
have, a planted test carrying a lint error the implementer was forbidden to fix.
A model cannot discover any of that from inside the worktree, so it fails
identically every attempt.

So `cut.md` is built around a **ground-truth ladder**: files on disk, then the
code the task will call, then what earlier packets produce, then the toolchain,
then the harness — and the spec ranks below all of them, as evidence of intent
rather than of fact. Every claim in a packet carries a `path:line`, a measured
count, or a command's output; an unverifiable claim is left out rather than
softened.

### Grounding: the tree the planner sees is not yours

A planning worktree is created by `git worktree add` and carries **tracked files
only**. So the first rung of the ladder — *open the data* — can be unclimbable in
the exact environment where cutting and assessing happen, and the failure is
silent from both sides: `ls` finding nothing looks identical to the project
having no such file. That is how `unverifiable` quietly becomes `held`.

`./factory/run.py ground` measures it. One probe, `git status --ignored`, true of
every git repository and specific to none — it needs no project configuration and
knows no filenames. It writes `tasks/GROUNDING.md`: what exists for you and not
for a planner, plus `[inputs] planner_must_read`, the one answer no probe can
supply and the only thing an operator must fill in.

Two axes, because either alone misses a real case. **Divergence** catches an
input that is present but untracked. **Reachability** catches one that has never
been fetched — which has no divergence to find, and is the case this was written
from: a data directory that is on no worktree because it is on no machine here.

`cut` and `assess` then **fail fast**: ungrounded, or a declared input that no
worktree can contain, and they refuse before a worktree or a model has cost
anything. A cut at `advanced` tier is the expensive leg, and packets whose data
claims nobody could have checked are the one defect `max_attempts` cannot absorb.
`--allow-unreachable-inputs` is the escape hatch; being ungrounded has no flag,
because the remedy is one cheap command that invokes no model.

`ground` is re-runnable and carries `planner_must_read` through untouched — the
probes are re-measured, the answer is not discarded.

Four more probes run alongside the divergence one, each recording the command it
ran so a reader can re-run it rather than believe it:

| Probe | What it settles |
|---|---|
| `surface:*` | the directory is there, its manifest proves what it is, and its runner is on PATH — a missing runner turns every gate on that surface into an environment fault |
| `toolchain:format` | how many files already fail a whole-tree format check, which is *why* the gate is scoped to the diff |
| `toolchain:baseline` | the offline suite's pass count |
| `spec` | the spec of record exists and has history to pin a packet to |

Surfaces are checked, never installed: `uv sync` and `npm ci` are minutes on a
cold landing, they are what `_planning_worktree` does anyway, and their failure
already surfaces there.

The two counts are recorded under `[measured]` and **nothing reads them**. The
baseline is re-measured on each task's own fork point and the format gate is
scoped to the diff, so these are for an operator and for drift detection only.
That is the difference between a number here and the same number in prose: it is
stamped with the commit it was taken at, and `ground --check` re-measures it.
Written into a paragraph instead, it becomes a claim that reads as measured, was
measured once, and has since stopped being true.

`ground --check` re-measures without rewriting, and reports **change, not state**.
An input that was unreachable at grounding and still is has not drifted — that is
a known condition `cut` already refuses on, and a check that fires forever on it
is a check nobody reads. It exits non-zero only when something regressed: an
input newly unreachable, a surface gone, a spec that stopped resolving. Moved
counts print as information, because a suite growing as the factory merges work
into it is the ordinary case.

#### The two questions

A first `ground` at a terminal asks them; `--interview` re-asks, `--yes` never
asks, and neither does an unattended run:

1. **Which paths must the planning tier be able to read?** The unclassified
   divergences are offered by number — and free text is accepted too, because
   the case that motivated all of this is an input nobody has fetched, which is
   invisible to the probe *and* to the planner. Offering only what was found
   would make the important answer the one you cannot give.
2. **Which document is the spec of record?** Tracked markdown outside `tasks/`
   and `factory/`, ranked longest-first with its line count and last commit, so
   it is a choice between things that exist rather than a memory test.

Everything an earlier draft wanted to ask turned out not to need asking. The
target branch comes from the checkout. Billing is a per-run flag that already
announces itself. `gate = "human"` is a per-packet judgement the cutter makes
from rules in `cut.md`, not a project constant. Tier-to-model is a property of
the runtime, not of the repository. Every question that collects an answer it
did not need adds an asserted fact to a design built to refuse them — so both
surviving answers are *checked*, recorded next to what a probe found when it
went looking, and a wrong one surfaces as a refusal rather than a silent bad plan.

⚠️ `isatty()` is a hint, not an answer. On Windows `NUL` is a character device,
so the usual unattended invocation `ground < /dev/null` **reports a terminal** —
the one case the guard exists for. EOF on the first read is the authoritative
signal and falls through to the non-interactive path, so a scripted landing
completes and writes. `KeyboardInterrupt` is treated as its opposite: someone
was there and changed their mind, so nothing is written.

#### Checking what the project says about itself

```
./factory/run.py conventions        # the one part of grounding that costs a model
```

A project's conventions documents look like the obvious place to ground a
planner: project-specific, already written, already read by humans. They are
also **prose about intent** — the same status as the spec, which this harness
ranks *below* files on disk. So a convention is a claim, not a fact. A true one
is worth handing to every future cutter; a stale one is a confident sentence
that will be believed, which is the whole defect class.

`conventions` runs `assess`'s method against them: extract every checkable
sentence, rule `held`/`violated`/`unverifiable` with a `path:line` or a command,
and propose the few that would have changed what somebody built. It gets
`TOOLS_ASSESS` — no `Edit` — and writes proposals to `.factory/conventions-draft.md`,
**never** to `tasks/TRAPS.md`. A proposal is not project state, and writing
straight into the file that grounds every cut would be the edit `_cut_output`
refuses for the cutter, arriving by another route.

`ground` discovers the documents without a model and records them under
`[conventions] sources`: the well-known names first (`CLAUDE.md`, `AGENTS.md`,
`CONTRIBUTING.md`, …), then ADRs, then READMEs shallowest-first. READMEs are in
the list because a project with no `CLAUDE.md` and no `CONTRIBUTING.md`, whose
real conventions live in a subdirectory README, is the ordinary case rather than
the exception. Unlike `planner_must_read` this *is* probeable, so it is
re-derived — except when an operator has narrowed the list, which wins.

`[spec] path` is also where `cut --spec` defaults from. There is no baked-in
spec path, because nothing in a *repository* identifies its spec of record — but
grounding is the operator's answer to exactly that question, in git and probed,
which is a different thing from a constant in the source.

### Three layers, composed

Neither prompt is a single file. Both are assembled from mechanism plus two
injected parts, because the two prompts were describing the *same* catalogue of
defects from opposite sides and had begun to disagree about it:

| | Where | What it holds |
|---|---|---|
| Mechanism | `prompts/cut.md`, `prompts/assess.md` | The ladder, the evidence rule, satisfiability, the trap block, the five structural checks. True of any repository this harness serves |
| The catalogue | `prompts/fragments/failure-classes.md` | Nine named failure classes. Composed into **both** prompts as `{{FAILURE_CLASSES}}`, so the cutter is warned about exactly what the assessor hunts for |
| Project traps | `tasks/TRAPS.md` | Facts about *this* tree that cost an attempt, each with a `path:line` and the class it belongs to. Injected as `{{PROJECT_TRAPS}}` |

Three of the nine carry a worked example, and the choice is a budget decision.
"Every claim carries a `path:line`" is a rule anyone nods at and nobody follows;
"three packets described a directory layout that existed nowhere, and all three
burned every attempt" is what makes it stick — so the classes that turn on
something a careful reader would still get wrong keep their evidence, and the
ones a one-line definition already settles do not. Note what is *not* the
criterion: an example was never dropped for being project-specific. The class is
portable and the instance is cited locally, but an instance that teaches stays.

A project that has never run the harness has no `TRAPS.md`, and the prompt says
so in as many words rather than rendering an empty heading, which would read as
"nothing to watch for here" — a claim, and the wrong one.

`tasks/TRAPS.md` grounds every future cut, so the cutter is not allowed to edit
it: `_cut_output` refuses that one path and reports it, the same reason
`TOOLS_ASSESS` grants no `Edit` at all. A cutter that finds a durable new trap
reports it under **New traps** in its cut report, and an operator promotes it.
Entries carry the date they were last confirmed, because a trap about a file
that no longer exists is pure context cost on every cut, forever.

⚠️ There is no regression test for prompt *quality* — only that the pieces
compose and that every class cited by name is defined.

`assess.md` is the cutter's adversary: it inventories every checkable claim in a
packet, returns `held`/`violated`/`unverifiable` with evidence for each, and runs
five structural checks — is every invariant satisfiable inside the packet's own
file boundary, is `requires` complete, are the planted fixtures gate-clean when
the linter runs from inside the surface directory, is the harness trap block
present, is the tier honest. Any violated claim, unverifiable invariant, failed
check or `blocker` finding makes `ready` impossible. It writes `assess.json`,
pinned to the packet's `sha256`, so an assessment of a since-edited packet does
not count.

Both run through `run.py`, in their own disposable worktrees, with the shell and
runners they need to check a claim against the thing that decides it — a planner
that can only read the spec can only restate it. The cutter keeps `Edit` to
revise packets it wrote; the assessor gets none, because a run that can edit what
it is judging will eventually repair a defect quietly instead of reporting it.

```
./factory/run.py cut --spec docs/DESIGN.md \
    --scope "§5, the income section and its loader"
./factory/run.py assess B01
```

`cut` writes its packets **untracked** into `tasks/`, validates each one through
`packet.parse` before staging it, and refuses to stage one that would fail
`sync`. Nothing it produces is queued; the operator assesses and commits.

`sync` then **refuses any packet without a `ready` assessment pinned to its
current sha256**. That is the whole point of the tier: a packet that is wrong
about the world fails identically every time, because the retry hands the model
the same wrong packet back. `--allow-unassessed` is the escape hatch. Tasks
already `done` are grandfathered — the work is on the branch and cannot be re-run.

The harness overrides the assessor exactly as it overrides the reviewer:
`contracts.effective_assessment` re-derives the verdict from the evidence, so an
assessor that says `ready` while reporting a blocker does not get to queue the
packet. `sync` re-derives it too, rather than trusting the `effective` field
recorded in the file.

## Capability tiers

A packet names a **tier**, never a model. `basic | standard | advanced` is a
statement about how much capability the task needs — a property of the packet —
while which model clears that bar is a property of the runtime and changes
without any packet changing. `agent.TIER_MODELS` is the one place a concrete
model name appears in the factory; `run.py` resolves a tier at each call site.

The two are recorded separately on purpose. `tasks.model` holds the tier the
packet asked for; `attempts.model` and `reviews.reviewer_model` hold the model
that actually ran, because the cost and turn counts on those rows were measured
on that model, and repointing a tier later must not silently rewrite what past
attempts are understood to have been measured on. (Both columns keep their old
names: `db._migrate` can add a column but not rename one.)

`basic` for a packet that is fully self-contained — a pattern-copy of one named
reference implementation, no fact the implementer must go find, no decision left
open. `standard` for everything else, which in practice is anything that parses a
real file or depends on a function it must go read. Assign from coupling, not
line count: three of the four packets that exhausted their attempts were bottom-tier
by a size judgement and were correct one tier up. The reviewer is never weaker than
the implementer; `packet.py` refuses a packet that inverts that, because same-tier
self-review is theatre.

⚠️ The tier calibration in `prompts/cut.md` and `prompts/assess.md` — including
the three-of-four figure above — was measured with `basic` on haiku and
`standard` on sonnet. Repoint a tier and those numbers stop being evidence about
what they are quoted as evidence for. Re-measure, or say in the prompt which
model the number came from; the alternative is a tier table that silently
invalidates the one piece of hard-won calibration this design has.

`gate = "human"` is for tasks whose failure mode is silent *and* whose blast
radius is the whole stage. It is the honest concession in this design: an
automated green gate cannot certify the invariants that fail green, and pretending
otherwise would merge them.

## Why turns are the cost driver

Measured on the first two tasks: **875,000 cached tokens read to produce 7,000**
of output. You are not paying for code, you are paying to re-read the packet and
the conventions on every turn. So a denied tool call is not a small waste — it
costs a whole turn, meaning another full context re-read, for zero output.

That is why implementers get an inspection shell (`ls`, `cat`, `head`, `tail`,
`wc`, `find`, `grep`, `awk`, `which`, `pwd`, `cd`, `git status/diff/log/show`)
on top of their runner, and why packets are told to prefer `Read`/`Glob`/`Grep`,
which are cheaper and never refused.

⚠️ `awk` is the one entry that is not strictly read-only — `print > "f"` writes
and `system()` shells out, so it is nearer to granting a shell than to granting
`grep`. It is allowed deliberately: the worktree is disposable, the
forbidden-path gate catches modification of any protected file, and the harness
re-runs the tests itself. Worth knowing rather than assuming the whole list is
inert.

It is also why a verification command should avoid `cd`. **Compound commands are
checked per segment**, so a leading `cd` was refused and the first worker spent
three turns inventing PowerShell workarounds for a command its own packet had
instructed it to run. Every segment of a `Verify` command must begin with
something the allowlist permits; there is a test for it.

The reviewer gets no shell at all. It has logged zero denials, and its `Write` —
present only so it can produce `review.json` — is already the sharp edge.

## Billing

Workers are the `claude` CLI in headless mode, but *how the CLI authenticates
is inherited from the environment*, so it is made explicit:

```
./factory/run.py run --billing api            # default
./factory/run.py run --billing subscription
```

`api` keeps `ANTHROPIC_API_KEY` in the worker's environment, so the CLI bills
the Anthropic API per token. That is the default because it is what makes the
numbers mean something: `total_cost_usd` is then actual spend, so `--max-cost`
is enforceable and the "was the cheap model actually cheaper" comparison in
`cost_by_task()` is a real measurement. It also keeps a long unattended queue
off the operator's interactive rate limits.

`subscription` scrubs the key so the CLI falls back to logged-in credentials.
Cheaper if the plan already covers the work, but the CLI still reports a
*notional* cost, so the cap is then policing a number that corresponds to no
bill — `run` says so on startup.

Either way the worker never inherits the launching session's identity
(`CLAUDE_CODE_SESSION_ID` and friends are stripped): a worker in a disposable
worktree is not a continuation of the session that started it.

Assessments count toward the same cap and the same totals, because they are the
same money. `cost_by_task` keeps them in their own column rather than folding
them in: `impl + review` is what a task cost to *run*, and `assess` is what it
cost to be sure the packet was worth running. Summing the two would hide the
comparison the factory is supposed to answer, and the planning half is the one
this design keeps claiming is expensive.

## Stop conditions

The loop halts — non-zero, with the reason in `events` — on any of: a task
exhausting `max_attempts`; the cumulative cost cap; two consecutive environment
faults; spec drift on a claimed task; a queue with work left but nothing
runnable.

Halting rather than skipping is deliberate. A task that failed three times has
usually revealed something wrong with its packet, and running the next twenty on
the same misunderstanding wastes more than it saves.

## Measured in the repository this was built in

Everything below is history, not a specification. It is here because the design
arguments above lean on it, and an argument from evidence should say where the
evidence came from. None of it is re-checked by anything, and none of it will be
true of your project.

**Cost of the first task**, a ~60-line pure module: **$0.11 to implement**
(`basic`, then haiku, 17 turns), **$0.18 to review** (`standard`, then sonnet,
6 turns). The review was the larger half. Budget accordingly, and do not assume
the cheap implementer dominates the bill.

**Tier calibration**: three of the four packets that exhausted their attempts
were assigned bottom-tier by a size judgement and were correct one tier up.
Measured with `basic` on haiku and `standard` on sonnet.

**Why the format gate is diff-scoped**: `ruff format --check` was already red on
files no agent had touched — five of them at the last count, eight when the
figure was first written down, which is the point. A whole-tree format gate
would fail every task for reasons nobody caused.

**Why the test baseline is re-measured per task**: that suite went from 81 tests
to 422 as the factory merged 37 tasks into it. A figure stored in the source
would have been wrong within a week. `measure_baseline` takes it on the task's
own fork point instead, cached per commit under `.factory/baselines/`.

Two counts, both of which moved while this file claimed otherwise. Run the
commands if you need the numbers.

## Prerequisites

`git`, `python3` (3.11+ for `tomllib`), the `claude` CLI on PATH, plus whatever
your surfaces need — see [What it assumes about your project](#what-it-assumes-about-your-project).
Docker for `needs_db` packets.

The repository must be clean and on the target branch when `run` starts, since
that is where merges land — which is why `--branch` defaults to the branch
checked out rather than to a constant, and `--spec` falls back to grounding
rather than to a constant: nothing in a repository identifies its spec of record,
and a packet pinned to the wrong file makes the drift check watch something the
work has nothing to do with.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
