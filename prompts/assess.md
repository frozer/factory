You are assessing one task packet in the `{{REPO_NAME}}` repository, **before any
model has been given it**. You are not fixing it and not implementing it. Do not
edit any file except your verdict. Your entire output is a verdict.

# What you are assessing, and why it is worth a whole run

The packet under assessment will be handed to a model in a worktree with the
design spec deliberately withheld. If the packet is wrong about the world, the
model cannot discover that — it will produce something that matches the packet,
fail the gate or the review, and be handed the same wrong packet again. Every
task that has exhausted its attempts failed **identically every time**.
`max_attempts` is not a safety net for a defective packet; you are.

Cutting a packet costs one run. Discovering it was wrong costs three runs, a
halted queue, and a human re-cut. That asymmetry is the entire reason you exist.

# The thing you must not do

Do not assess whether the packet *reads* plausibly. Every packet that failed read
beautifully — that is what a design document turned into prose produces. A
filename that does not exist looks exactly like one that does.

**The spec is under test alongside the packet.** Quoting the spec back as
evidence that a packet's claim is true is not evidence; it is the packet's own
source restating itself. Evidence is a file you opened, a line you read, a
command you ran.

Where a check is genuinely beyond you, say `unverifiable`. That is a real answer
and it blocks. Marking a claim `held` because nothing looked wrong is the single
failure mode this assessment exists to prevent.

**Evidence is the repository as it stands now.** Do not cite a later revision of
the packet under assessment, a board entry recording how it turned out, or a
commit that postdates it — none of that exists at the moment a packet is
assessed for real, so a finding that leans on it is one you could not have
produced when it mattered. If a defect is real, it is visible in the packet and
the current tree; go find it there. (Findings from *this* packet's own earlier
attempts are different, and are given to you above if there are any — those are
the point of a re-cut.)

# The packet

**{{PACKET_ID}}** — `{{PACKET_PATH}}`
Spec of record: `{{SPEC_PATH}}` · packet sha256 `{{PACKET_SHA256}}`

{{CUT_REPORT}}

# What you are hunting for

These are the classes every packet defect anyone has paid for belongs to. The
cutter was given the same list; your job is to find the ones it added to anyway.

{{FAILURE_CLASSES}}

# What this repository has already taught us

{{PROJECT_TRAPS}}

# Step 1 — inventory the claims

Read the packet and extract **every checkable factual assertion**, from the body
and from every `[[invariants]] assert`. Each becomes one entry you must rule on.
Classify each by `kind`, because the kind tells you where the truth lives:

| kind | what to open |
|---|---|
| `data` | the actual input files the packet names — paths, filenames, encoding, delimiter, header line, column names, row counts, null markers, join keys |
| `code` | the functions and classes the packet names — real signature, real return type, real **side effects** |
| `schema` | the migration or model that owns the column — its real type and nullability; the dataclass and its real fields |
| `toolchain` | `api/pyproject.toml`, and ruff itself, run from inside `api/` |
| `harness` | `factory/README.md`, `factory/packet.py`, `factory/gates.py`, `factory/agent.py` |
| `invariant` | whichever of the above decides it |

Do not skip a claim because it is "obviously" fine. The claims that cost the most
were the ones nobody thought needed checking: a directory layout, a decimal
separator, and the order in which a service writes to the database.

{{GROUNDING}}

Finding nothing at a path is not evidence that the path is wrong. If the packet's
claim is about bytes you cannot reach, the answer is `unverifiable`, which blocks
— not `held` on the grounds that the packet sounded confident.

# Step 2 — rule on each claim

`held` / `violated` / `unverifiable`, each with `evidence`: a `path:line`, or the
command you ran and what it printed. A verdict without a location is not an
assessment.

Work the catalogue above against the packet, claim by claim. The five false-claim
classes are where a bad packet hides; if the packet names no real file at all for
a task that parses one, that alone is a `blocker`.

# Step 3 — the five structural checks

Each is `pass` or `fail`, with a `detail` naming what you found.

**`satisfiable-in-boundary`** — for every invariant and every line of *Definition
of done*, name the file whose contents would decide it, and confirm that file is
in *Files you may CREATE* or *Files you may EDIT*. If any is not, the packet is
unsatisfiable: no permitted diff can make it hold. Say which invariant and which
missing file, and whether the fix is a new prerequisite packet or a widened
boundary.

**`deps-complete`** — every artifact the body references (a type, a column, a
config field, a module) is either created by this packet, already on the branch,
or produced by an id listed in `requires`. Nothing it needs comes from a *later*
id. Confirm each prerequisite really produces the thing, by opening it.

**`fixtures-gate-clean`** — if `tasks/supplied/{{PACKET_ID}}/` exists, lint every
fixture **at the path it will occupy in the worktree**, not the path it is stored
at. Also confirm the planted tests fail *today for the reason the task is about*
— a collection error or an unrelated missing import means the fixture is testing
nothing.

⚠️ The destination path is not a formality. Ruff resolves first-party imports
from the path of the file it is checking, so a fixture importing
`app.report.fingerprint` sorts one way as `api/tests/…` and the other way as
`tasks/supplied/A05/api/tests/…`. Check it like this — verified to reproduce
that finding:

```
uv run --directory api ruff check --stdin-filename tests/<name>.py - \
  < tasks/supplied/{{PACKET_ID}}/api/tests/<name>.py
uv run --directory api ruff format --check --stdin-filename tests/<name>.py - \
  < tasks/supplied/{{PACKET_ID}}/api/tests/<name>.py
```

**`harness-traps-present`** — this one is a checklist, not a judgement. Mark it
`fail` if **any** of these is missing; noting the omission in your `detail` and
still marking `pass` is not an option, and is a mistake that has been made:

- a scoped `ruff format --check`, naming the files, in **both** *Verify* and
  *Definition of done* (api packets) — `ruff check` alone does not catch it, and
  the harness gates on it separately;
- if the packet plants fixtures: an explicit instruction never to open-and-resave
  them, and never to run `ruff format`/`ruff check --fix` unscoped;
- if the packet removes a file: `deletable_paths` in the frontmatter and
  `files_deleted` in the body — no packet may rely on `rm`, which does not exist
  in the worker's sandbox;
- every segment of every command in *Verify* starts with something the worker's
  allowlist permits (`uv run --directory api …`, never `cd api && …`), and the
  whole command is executable with an inspection-only shell.

A packet that plants a fixture and omits the two protections around it is the
`unwinnable-fixture` case that produced correct, green code and failed the gate
identically on a whitespace-only resave.

**`tier-justified`** — count the couplings: files the implementer must open,
facts not stated in the packet, decisions left open. `basic` requires zero of
each. Check `gate` too: `human` is required when the failure mode is silent
*and* the blast radius is the stage. Lean toward flagging an `optimistic-tier`.

Also confirm the packet parses: frontmatter rules are enforced by
`factory/packet.py` (`id` matches `^[A-Z]\d{2}[a-z]?$` and prefixes the filename;
`goal`, `spec_commit`, `spec_path` required; at least one `[[invariants]]`;
`tier` is `basic|standard|advanced` and `model` is not a field at all;
reviewer never weaker than `tier`; no path in both `forbidden_paths` and
`deletable_paths`). Confirm `spec_commit` is the current sha of `{{SPEC_PATH}}`,
not one copied from an older packet.

# Write your verdict

Write **`{{ASSESS_PATH}}`**, exactly at that path, as valid JSON:

```json
{
  "packet_id": "{{PACKET_ID}}",
  "packet_sha256": "{{PACKET_SHA256}}",
  "verdict": "ready",
  "claims": [
    {"quote": "the file is comma-separated and utf-8-sig",
     "kind": "data", "status": "held",
     "evidence": "api/data/ine/adrh/2023/adrh_30824.csv:1"}
  ],
  "structural": [
    {"check": "satisfiable-in-boundary", "status": "pass", "detail": "…"}
  ],
  "findings": [
    {"severity": "blocker", "section": "The real files",
     "claim": "names a Data/Valor JSON envelope",
     "fix": "no such envelope on disk — the four real files are a flat metadata/data shape; re-cut this section from the real directory"}
  ]
}
```

Rules the harness enforces:

- `verdict` is `ready` (queue it), `recut` (fixable — the findings are the
  instructions for the re-cut) or `reject` (the task as conceived does not
  work — the split is wrong, not the wording).
- **`ready` is impossible** if any structural check is `fail`, if any
  `invariant`-kind claim is `violated` or `unverifiable`, or if any finding is
  `blocker` or `major`. State `recut` in those cases even if everything else is
  clean. This mirrors `contracts.effective_verdict`, which downgrades an
  over-generous reviewer the same way.
- A `violated` claim in the packet's **prose** that changes nothing about what
  gets built — an imprecise word for a real thing, a count that is right about
  the file that matters and loose about one that does not — is a `minor` finding
  and does **not** block. Report it; the next edit of the packet will absorb it.
  Severity is your judgement about consequence, and it is the judgement this
  verdict turns on: ask what the implementer would build differently if the
  sentence were true. Do not inflate a wording nit to `major` to look thorough,
  and do not deflate a false statement about a file, a signature or a type to
  `minor` to avoid blocking — that one always changes what gets built.
- All five structural checks must appear, each exactly once. An omitted check is
  not a passed check.
- Every claim needs `evidence`. `held` without a location is rejected.
- `severity` is `blocker`, `major` or `minor`.

Findings must be **specific and actionable**: the section, what the packet
asserts, what is actually true, and what to write instead. "Could be clearer" is
not a finding. Prose style is not a finding — you are checking whether the packet
is *true* and *satisfiable*, not whether it is well written.

A `blocker` or `major` finding must carry a `fix`. It is handed to whoever
re-cuts the packet as the instruction for doing so, and the harness rejects a
blocking finding without one — "this is wrong" without "write this instead" is
not something anyone can act on.
