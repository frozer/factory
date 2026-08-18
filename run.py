#!/usr/bin/env python3
"""The factory loop.

Drains the task queue: claim a packet, run it in an isolated worktree, verify it
with gates the harness owns, review it against the packet's declared invariants,
and squash-merge what survives onto the feature branch.

Ahead of the queue sits a planning tier — `cut` and `assess` — because the one
failure the loop cannot absorb is a packet that is wrong about the world. That
fails identically on every attempt, since the retry hands the model the same
wrong packet back.

    ./factory/run.py ground          # measure what a worktree will contain
    ./factory/run.py conventions     # check what the project claims about itself
    ./factory/run.py cut --spec docs/DESIGN.md --scope "§5"  # propose packets
    ./factory/run.py assess B01      # rule on one, before it can be queued
    ./factory/run.py sync            # register tasks/*.md into the queue
    ./factory/run.py plan            # resolved order, no models invoked
    ./factory/run.py run             # drain the queue
    ./factory/run.py run --once      # a single task
    ./factory/run.py status          # the board
    ./factory/run.py approve B01     # merge a task held for human review
    ./factory/run.py reset B01       # wipe a task to ready, worktree and all

Stdlib only; run it with a bare `python3`.
"""

from __future__ import annotations

import argparse
import functools
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent  # noqa: E402
import board  # noqa: E402
import contracts  # noqa: E402
import gates  # noqa: E402
import gitops  # noqa: E402
import ground  # noqa: E402
import packetlint  # noqa: E402
from db import Factory, Task  # noqa: E402
from db import now as db_now  # noqa: E402
from packet import TIERS, Packet, PacketError, load_all  # noqa: E402
from packet import already_landed  # noqa: E402
from packet import is_packet_filename  # noqa: E402
from packet import parse as packet_parse  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
STATE = REPO / ".factory"
DB_PATH = STATE / "factory.db"
TASKS_DIR = REPO / "tasks"
SUPPLIED_DIR = TASKS_DIR / "supplied"
# Assessments live in git beside the packets they rule on, not in the database.
# `sync` gates on them, and a gate held only in machine-local state would let a
# fresh clone queue a packet nobody ever vetted.
ASSESSMENTS_DIR = TASKS_DIR / "assessments"
# What this repository has taught the harness, in git beside the packets. Grown
# by an operator from what a cut report or an assessment turned up; see
# `tasks/TRAPS.md` for what belongs in it.
TRAPS_PATH = TASKS_DIR / "TRAPS.md"
# What the harness measured about the repository it landed in. In git for the
# same reason: it decides whether a cut may start, and a gate held only in
# machine-local state would let a fresh clone plan against nothing.
GROUNDING_PATH = TASKS_DIR / "GROUNDING.md"
PROMPTS = Path(__file__).resolve().parent / "prompts"
# Composed into prompts rather than sent as one. Nothing here is rendered on its
# own, which is why it is not beside the prompts themselves.
FRAGMENTS = PROMPTS / "fragments"

# Two consecutive environment faults mean the box is broken, not the work. The
# loop stops rather than burning attempts on tasks that were never wrong.
MAX_CONSECUTIVE_ERRORS = 2

# A task in one of these is presumed to have a loop actively working it right
# now; resetting out from under that would race the very thing it is doing.
IN_FLIGHT = ("running", "gating", "reviewing", "merging")


@dataclass(frozen=True, slots=True)
class Config:
    # No default. `run` refuses to start unless this is the branch actually
    # checked out, so a baked-in one is either redundant or wrong — and in a
    # repository that does not have that branch it is wrong on the first
    # invocation, before the operator has any reason to look for it. The CLI
    # resolves it from the checkout instead.
    target_branch: str
    max_cost_usd: float = 25.0
    skip_permissions: bool = False
    keep_worktrees: bool = False
    # 'api' bills the Anthropic API per token, which is what makes max_cost_usd
    # enforceable; 'subscription' scrubs the key and the reported cost becomes
    # notional. See agent.child_env for the full trade-off.
    billing: str = "api"
    # How long a single agent call is willing to wait out a rate limit before
    # giving up and raising into the environment-fault path. See
    # agent.run_resilient / agent.DEFAULT_MAX_WAIT_S. Only applies when the CLI
    # gave no parseable reset hint — a real guess, capped tight.
    max_wait_s: float = agent.DEFAULT_MAX_WAIT_S
    # Same backstop, but for when the CLI *did* give a reset time — trusted
    # much further out since it's a fact, not a guess. See
    # agent.DEFAULT_MAX_PARSED_WAIT_S.
    max_parsed_wait_s: float = agent.DEFAULT_MAX_PARSED_WAIT_S
    # The conventional-commit scope every merge is labelled with, or "" for none.
    # Empty by default and for the same reason `target_branch` is not baked in: a
    # scope is a fact about the project the harness is vendored into, and a scope
    # baked in here labels every task any project ever merges with a name from
    # some other repository's numbering. A wrong label is worse than no label,
    # because it is the one a reader believes.
    commit_scope: str = ""


class Halt(RuntimeError):
    """A stop condition. Carries the reason to the operator and exits non-zero."""


# ------------------------------------------------------------------ helpers


def render(template: Path, **fields: str) -> str:
    text = template.read_text(encoding="utf-8")
    for key, value in fields.items():
        text = text.replace("{{" + key + "}}", value)
    return text


@functools.cache
def repo_name() -> str:
    """`{{REPO_NAME}}` for every prompt. Cached — it shells out, and it cannot
    change within a run."""
    return gitops.repo_name(REPO)


def failure_classes() -> str:
    """The catalogue both planning prompts are built around.

    Shared rather than duplicated: `cut.md` tells the cutter to avoid these and
    `assess.md` tells the assessor to hunt for them, so two copies would be two
    copies of the same list seen from opposite sides — and would drift, leaving
    the assessor hunting for something the cutter was never warned about.
    """
    return (FRAGMENTS / "failure-classes.md").read_text(encoding="utf-8").strip()


def project_traps() -> str:
    """`tasks/TRAPS.md` — facts about *this* repository that cost an attempt.

    In git beside the packets and assessments, for the same reason those are: it
    grounds every cut, so a fresh clone must not plan against an appendix nobody
    vetted. Absent is the normal state for a project that has not run the
    harness yet, and says so rather than rendering an empty section — a heading
    with nothing under it reads as "nothing to watch for here", which is a
    claim, and the wrong one.
    """
    if not TRAPS_PATH.is_file():
        return (
            "Nothing recorded yet — this repository has not run the harness long "
            "enough to have taught it anything. That is not the same as there "
            "being nothing to find, so weight the ladder below accordingly: "
            "every claim you make is one nobody has been burned by yet."
        )
    return TRAPS_PATH.read_text(encoding="utf-8").strip()


def grounding_summary() -> str:
    """`{{GROUNDING}}` — what a planning agent is told about its own worktree."""
    grounded = ground.load(GROUNDING_PATH)
    declared = grounded.planner_must_read if grounded else []
    return ground.summary(grounded, ground.check_inputs(REPO, declared))


def require_reachable_inputs(command: str, *, allow: bool) -> None:
    """Fail fast, before a worktree or a model costs anything.

    A planning run whose ground-truth ladder cannot be climbed does not produce
    a worse packet — it produces one whose data claims nobody could have
    checked, which is the single defect the planning tier exists to prevent and
    the one `max_attempts` cannot absorb. Refusing here costs an operator a
    minute; not refusing costs a cut at `advanced` tier and an inventory that
    reads perfectly.

    Ungrounded is refused too, and without a flag: the remedy is one cheap
    command that invokes no model, so an escape hatch would only ever be used to
    skip it.
    """
    grounded = ground.load(GROUNDING_PATH)
    if grounded is None:
        raise Halt(
            f"{command} needs grounding first — nothing has been measured about "
            "what a planning worktree will contain. Run:\n\n"
            "    ./factory/run.py ground\n\n"
            "then declare any input paths the planner must read in "
            f"{GROUNDING_PATH.relative_to(REPO).as_posix()}."
        )
    checks = ground.check_inputs(REPO, grounded.planner_must_read)
    blocked = [c for c in checks if c.blocks]
    if not blocked:
        return
    detail = "\n".join(f"  ⛔ {c.path} — {c.why}" for c in blocked)
    if allow:
        print(
            f"proceeding with unreachable inputs; every claim about these is "
            f"unverifiable from a worktree:\n{detail}",
            file=sys.stderr,
        )
        return
    raise Halt(
        f"{command} refuses to start: a declared input cannot be opened from a "
        f"planning worktree.\n{detail}\n\n"
        "The ground-truth ladder's first rung is 'open the data', so a packet "
        "cut now would describe files nobody checked. Make them reachable "
        "(commit them, or fetch them and re-run `ground`), or pass "
        "--allow-unreachable-inputs to cut anyway and let the assessor block "
        "the claims."
    )


def packets_by_id() -> dict[str, Packet]:
    return {p.id: p for p in load_all(TASKS_DIR)}


def supplied_for(task_id: str) -> list[tuple[Path, str]]:
    """Files the harness plants before the model starts.

    `tasks/supplied/<ID>/` mirrors the repo layout. Anything under it is copied
    into the worktree and committed *before* the implementer runs, then declared
    off-limits. That is what turns "here is the test, make it pass" into
    something enforceable: the model cannot edit the test until it agrees.
    """
    root = SUPPLIED_DIR / task_id
    if not root.is_dir():
        return []
    return [
        (p, p.relative_to(root).as_posix())
        for p in sorted(root.rglob("*"))
        if p.is_file() and not _is_build_residue(p)
    ]


def _is_build_residue(path: Path) -> bool:
    """Bytecode and caches are not supplied files.

    `supplied_for` globs the filesystem, not the index, so a `.pyc` left behind
    by running the planted test locally would be planted too — and then declared
    off-limits, so pytest regenerating it would trip the tamper gate on a file
    nobody wrote.
    """
    return path.suffix in (".pyc", ".pyo") or "__pycache__" in path.parts


def baseline_for(fork_sha: str, worktree: Path) -> int | None:
    """Offline pass count on the untouched checkout, cached per commit.

    Measured rather than assumed: "no previously-passing test now fails" is only
    a real gate if the harness knows what passed before.
    """
    cache = STATE / "baselines" / f"{fork_sha}.json"
    if cache.is_file():
        try:
            return json.loads(cache.read_text(encoding="utf-8")).get("passed")
        except (json.JSONDecodeError, OSError):
            pass
    counts = gates.measure_baseline(worktree / "api")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(counts), encoding="utf-8")
    return counts.get("passed")


def fmt_findings(prior: list[dict]) -> str:
    """Previous attempts' findings, as instructions for this one."""
    if not prior:
        return ""
    out = [
        "# What the reviewer said last time",
        "",
        "This is a retry. A previous attempt was reviewed and sent back. Address",
        "every item below. Do not start over — fix what is here.",
        "",
    ]
    for entry in prior[:2]:
        out.append(f"## Attempt {entry['attempt']} — verdict `{entry['verdict']}`")
        out.append("")
        for inv in entry["invariants"]:
            if inv["status"] != "held":
                out.append(
                    f"- invariant **{inv['id']}** was `{inv['status']}`"
                    + (f" ({inv['evidence']})" if inv.get("evidence") else "")
                )
        for f in entry["findings"]:
            loc = f.get("file", "")
            if f.get("line"):
                loc = f"{loc}:{f['line']}"
            out.append(f"- **{f['severity']}** {loc} — {f['claim']}")
            if f.get("fix"):
                out.append(f"  - fix: {f['fix']}")
        out.append("")
    return "\n".join(out)


def verify_command(pkt: Packet) -> str:
    """What the worker is told to run, phrased so the allowlist permits it.

    `uv run --directory api …` rather than `cd api && uv run …`: compound
    commands are checked per segment, so the `cd` was denied and the worker
    burned three turns inventing shell workarounds for a command the packet had
    told it to run. `cd` is allowed now too, but not needing it is better.

    **The `-m db` segment is omitted where the project registers no such
    marker.** `prompts/implement.md` hands this command over with "fix what it
    reports", and a segment that exits 5 on every run reports something the
    worker cannot fix: registering a marker means editing the project's
    `pyproject.toml`, which every packet that asks for a marker has in its
    `forbidden_paths`. `gates.py` scores the same empty run `skipped` rather
    than failing it; this is the other half, because a survivable gate still
    leaves an impossible instruction, and a worker that cannot satisfy its
    verification is a worker that does not finish.
    """
    if pkt.surface == "webapp":
        return "npm --prefix webapp run lint && npm --prefix webapp run build"
    cmd = "uv run --directory api ruff check . && uv run --directory api pytest -q"
    marker = pkt.extra_pytest_marker
    if marker and _marker_registered(marker):
        cmd += f" && uv run --directory api pytest -q -m {marker}"
    return cmd


def _marker_registered(marker: str) -> bool:
    """Whether `-m <marker>` could select anything in this project.

    `None` from `declared_markers` means the question could not be answered —
    markers configured somewhere it does not read. The segment is kept in that
    case, because the old behaviour is the safe one when nothing is known.
    """
    declared = gates.declared_markers(REPO / "api")
    return declared is None or marker in declared


def format_note(pkt: Packet) -> str:
    """Why `ruff check` above is not enough, for api packets.

    A08 shipped a migration that passed `ruff check` and every test, then sat
    red for two more attempts on `ruff format --check` alone — the verify
    command never ran it, so nothing told the model the file was wrong, and it
    kept re-declaring success over an unchanged diff. The gate scopes the
    format check to files the diff touched (whole-tree is already red on
    pre-existing files, per `gates.py`), so the instruction here mirrors that
    scope rather than telling the model to reformat the tree.
    """
    if pkt.surface != "api":
        return ""
    return (
        "\n`ruff check` is a linter, not a formatter — it will not catch a file "
        "that only fails `ruff format`, and the harness gates on that "
        "separately. Before you finish, also run `ruff format --check` on "
        "every file you created or modified, e.g.:\n\n"
        "```\n"
        "uv run --directory api ruff format --check <each file you touched>\n"
        "```\n\n"
        "If it says a file would be reformatted, run `uv run --directory api "
        "ruff format <that file>` to fix it, then re-run `--check` to confirm. "
        "Do not run `ruff format` over the whole tree — several files outside "
        "this task are already non-compliant and are not yours to fix.\n"
    )


# --------------------------------------------------------------------- run


def _make_on_wait(fac: Factory, task_id: str):
    """Console + durable-log visibility for a rate-limit wait.

    `agent.py` doesn't import `db` — this closure is how a wait becomes both
    a live progress line and an `events` row without widening that boundary.
    """

    def on_wait(message: str, resume_at, elapsed_s: float) -> None:
        print(f"  … {message}", file=sys.stderr)
        fac.event(
            "rate_limited_wait",
            {
                "message": message,
                "resume_at": resume_at.isoformat() if resume_at else None,
                "elapsed_s": elapsed_s,
            },
            task_id,
        )

    return on_wait


def run_task(fac: Factory, pkt: Packet, task: Task, cfg: Config) -> str:
    """One task, start to finish. Returns the outcome for the loop to act on."""
    worktree = STATE / "worktrees" / task.id
    branch = f"task/{task.id}"
    attempt_no = fac.attempt_count(task.id) + 1
    run_dir = STATE / "runs" / task.id / f"attempt-{attempt_no}"
    run_dir.mkdir(parents=True, exist_ok=True)
    on_wait = _make_on_wait(fac, task.id)

    try:
        drift = gitops.spec_moved(REPO, task.spec_commit, task.spec_path)
    except gitops.GitError as exc:
        # Blocked rather than raised: a bad pin is a defect in one packet, and
        # taking the whole loop down would punish every other task in the queue.
        fac.set_status(
            task.id,
            "blocked",
            blocked_reason=f"spec of record cannot be read: {exc}",
        )
        fac.event("spec_unreadable", {"error": str(exc)}, task.id)
        return "blocked"
    if drift:
        fac.set_status(
            task.id,
            "blocked",
            blocked_reason=(
                f"spec moved since this packet was cut at {task.spec_commit}: "
                + "; ".join(drift[:3])
            ),
        )
        fac.event("spec_drift", {"commits": drift}, task.id)
        return "blocked"

    landed = already_landed(pkt, REPO)
    if landed:
        fac.set_status(
            task.id,
            "blocked",
            blocked_reason=(
                "the work is already on the branch: "
                + ", ".join(landed[:3])
                + " exist and this packet is to create them"
            ),
        )
        fac.event("already_landed", {"paths": landed}, task.id)
        return "blocked"

    fresh = not worktree.exists()
    if fresh:
        gitops.worktree_add(REPO, worktree, branch, cfg.target_branch)
        try:
            gitops.install_deps(worktree, pkt.surface)
        except gitops.DepsError:
            # Leaving a half-set-up worktree on disk would make the next claim
            # of this task see `fresh=False` and skip the install a second
            # time, walking straight into the same failure deeper in the
            # pipeline instead of retrying the fix.
            gitops.worktree_remove(REPO, worktree)
            raise

    # The baseline must be measured on the untouched fork point — before any
    # supplied test is planted, since those are expected to fail. On a retry the
    # worktree has moved on, so the number is read back from the event log
    # rather than re-measured against work in progress.
    if fresh:
        baseline = baseline_for(gitops.rev_parse(worktree, "HEAD"), worktree)
        fac.event("baseline", {"passed": baseline}, task.id)
    else:
        baseline = _recorded_baseline(fac, task.id)

    planted: list[str] = []
    if fresh:
        for src, rel in supplied_for(task.id):
            dst = worktree / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            planted.append(rel)
        if planted:
            gitops.commit_all(worktree, f"test({task.id}): plant supplied tests")
            fac.event("planted", {"files": planted}, task.id)

    base_sha = gitops.rev_parse(worktree, "HEAD") if fresh else _base_sha(fac, task.id)
    if not planted:
        planted = _planted_paths(fac, task.id)

    # `task.model` is a tier. Resolve it once and record the model that
    # actually ran: the tier is what the packet asked for, and the model is
    # what the cost and turn counts on this row were measured on. Storing the
    # tier in both places would make every past attempt un-attributable the
    # first time a tier is repointed at a different model.
    impl_model = agent.resolve(task.model)

    attempt_id = fac.start_attempt(
        task.id,
        model=impl_model,
        branch=branch,
        base_sha=base_sha,
        run_dir=str(run_dir.relative_to(REPO)),
        billing=cfg.billing,
    )
    fac.event("planted_paths", {"files": planted}, task.id)

    forbidden = sorted(set(pkt.forbidden_paths) | set(planted))
    planted_block = (
        "# Tests have been written for you\n\n"
        "These files are already in the worktree and are expected to FAIL right\n"
        "now. Your job is to make them pass by implementing the task — not by\n"
        "changing them. They are the specification in executable form:\n\n"
        + "\n".join(f"- `{p}`" for p in planted)
        if planted
        else ""
    )

    prompt = render(
        PROMPTS / "implement.md",
        REPO_NAME=repo_name(),
        TASK_ID=task.id,
        PACKET=pkt.body,
        FORBIDDEN="\n".join(f"- `{p}`" for p in forbidden) or "- (none declared)",
        DELETABLE="\n".join(f"- `{p}`" for p in pkt.deletable_paths)
        or "- (none declared — do not delete anything)",
        PLANTED_BLOCK=planted_block,
        PRIOR_FEEDBACK=fmt_findings(fac.prior_findings(task.id)),
        VERIFY_CMD=verify_command(pkt),
        FORMAT_NOTE=format_note(pkt),
        RESULT_PATH=".factory/result.json",
    )

    artifact = worktree / ".factory" / "result.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    try:
        impl, telemetry, err = agent.run_with_contract(
            worktree=worktree,
            prompt=prompt,
            model=impl_model,
            allowed_tools=agent.TOOLS_IMPL[pkt.surface],
            run_dir=run_dir,
            label="implement",
            artifact=artifact,
            validate=lambda obj: contracts.validate_impl(obj, pkt),
            skip_permissions=cfg.skip_permissions,
            billing=cfg.billing,
            max_wait_s=cfg.max_wait_s,
            max_parsed_wait_s=cfg.max_parsed_wait_s,
            on_wait=on_wait,
        )
    except agent.RateLimited as exc:
        fac.revert_attempt(task.id, attempt_id, str(exc))
        raise

    fac.finish_attempt(
        attempt_id,
        agent_status=(impl.status if impl else "failed"),
        agent_json=(impl.raw if impl else {"contract_error": err}),
        **telemetry.telemetry,
    )

    if impl is None:
        fac.event("contract_failed", {"error": err}, task.id)
        return _retry_or_block(
            fac, task, f"the model's result.json was unusable: {err}"
        )

    if impl.status != "complete":
        reason = impl.blocked_reason or "the model reported it could not finish"
        fac.event(
            "agent_incomplete", {"status": impl.status, "reason": reason}, task.id
        )
        if impl.status == "blocked":
            fac.set_status(task.id, "blocked", blocked_reason=reason)
            return "blocked"
        return _retry_or_block(fac, task, reason)

    # The model has no shell capable of `rm`/`git rm` — deletion is a harness
    # act, gated on the packet's own `deletable_paths`, not a model one. The
    # contract already refused any path outside that list, so this is just
    # carrying out what was declared and validated.
    for rel in impl.files_deleted:
        (worktree / rel).unlink(missing_ok=True)
    if impl.files_deleted:
        fac.event("deleted", {"files": impl.files_deleted}, task.id)

    head_sha = gitops.commit_all(worktree, f"wip({task.id}): attempt {attempt_no}")
    if head_sha is None:
        # The model claimed complete but left nothing to commit — most often a
        # retry that re-inspected a prior attempt's work and agreed with it.
        # That is not the same as verified: gate the unchanged worktree anyway,
        # so a task that is still red comes back with the real reason instead
        # of a generic one, and a task that is genuinely fixed can still reach
        # review. Silently retrying from here previously spent two attempts
        # re-declaring success over a diff that never moved (A08).
        fac.event("no_new_changes", {"attempt": attempt_no}, task.id)
        head_sha = gitops.rev_parse(worktree, "HEAD")

    return _gate_and_review(
        fac,
        pkt,
        task,
        cfg,
        worktree=worktree,
        branch=branch,
        attempt_id=attempt_id,
        run_dir=run_dir,
        base_sha=base_sha,
        baseline=baseline,
        forbidden=forbidden,
        head_sha=head_sha,
        impl_claim=json.dumps(impl.raw, indent=2)[:6000],
    )


def _gate_and_review(
    fac: Factory,
    pkt: Packet,
    task: Task,
    cfg: Config,
    *,
    worktree: Path,
    branch: str,
    attempt_id: int,
    run_dir: Path,
    base_sha: str,
    baseline: int | None,
    forbidden: list[str],
    head_sha: str,
    impl_claim: str,
) -> str:
    """Gate `head_sha` against `base_sha` and, if green, run review and act on
    the verdict. Shared by the normal implement-then-gate path and `resume`,
    which supplies a worktree HEAD a human already fixed by hand instead of a
    fresh model attempt."""
    fac.set_status(task.id, "gating")
    changed = gitops.changed_files(worktree, base_sha)
    tampered = gitops.touched(worktree, base_sha, forbidden)
    result = gates.run_checks(
        worktree,
        surface=pkt.surface,
        changed=changed,
        tampered=tampered,
        extra_marker=pkt.extra_pytest_marker,
        baseline=baseline,
    )
    # A gate that repaired formatting leaves the worktree dirty, and the attempt
    # was committed before the gate ran. Fold the rewrite into the attempt's HEAD
    # now: `is_dirty` further down is the meddling check, and an uncommitted
    # rewrite would read as a human editing the worktree mid-review.
    if result.autoformatted:
        formatted_sha = gitops.commit_all(
            worktree, f"style({task.id}): ruff format, applied by the gate"
        )
        if formatted_sha is not None:
            head_sha = formatted_sha
        fac.event("autoformatted", {"files": result.autoformatted}, task.id)
        print(f"  gate formatted {len(result.autoformatted)} file(s); attempt not spent")

    (run_dir / "gate.json").write_text(result.to_json(), encoding="utf-8")
    fac.finish_attempt(
        attempt_id,
        head_sha=head_sha,
        gate_status=result.status,
        gate_json=result.to_json(),
    )
    fac.event("gate", {"status": result.status, "summary": result.summary()}, task.id)

    if result.status == "error":
        return "error"
    if result.status == "red":
        return _retry_or_block(
            fac,
            task,
            "the harness gate failed: "
            + "; ".join(f"{c.name} — {c.output_tail[:300]}" for c in result.failures),
        )

    # ------------------------------------------------------------- review
    fac.set_status(task.id, "reviewing")
    diff_rel = ".factory/diff.patch"
    (worktree / diff_rel).write_text(
        gitops.diff_text(worktree, base_sha), encoding="utf-8"
    )
    inv_lines = "\n".join(
        f"- **`{i.id}`**{' (critical)' if i.critical else ''} — {i.assertion}"
        for i in pkt.invariants
    )
    review_prompt = render(
        PROMPTS / "review.md",
        REPO_NAME=repo_name(),
        TASK_ID=task.id,
        GOAL=pkt.goal,
        PACKET=pkt.body,
        INVARIANTS=inv_lines,
        IMPL_CLAIM=impl_claim,
        GATE=result.summary()
        + "\n"
        + json.dumps({c.name: c.detail for c in result.checks}, indent=2)[:2000],
        DIFF_PATH=diff_rel,
        REVIEW_PATH=".factory/review.json",
    )

    review_artifact = worktree / ".factory" / "review.json"
    reviewer_model = agent.resolve(task.reviewer)
    review, rev_telemetry, rev_err = agent.run_with_contract(
        worktree=worktree,
        prompt=review_prompt,
        model=reviewer_model,
        allowed_tools=agent.TOOLS_REVIEW,
        run_dir=run_dir,
        label="review",
        artifact=review_artifact,
        validate=lambda obj: contracts.validate_review(obj, pkt),
        skip_permissions=cfg.skip_permissions,
        billing=cfg.billing,
        max_wait_s=cfg.max_wait_s,
        max_parsed_wait_s=cfg.max_parsed_wait_s,
        on_wait=_make_on_wait(fac, task.id),
    )

    if review is None:
        fac.event("review_contract_failed", {"error": rev_err}, task.id)
        return _retry_or_block(fac, task, f"the review was unusable: {rev_err}")

    # A reviewer that edited the code is not a reviewer. Write access exists
    # only so it can produce review.json; anything else invalidates the verdict.
    meddled = gitops.is_dirty(worktree)
    if meddled:
        fac.event("reviewer_meddled", {"paths": meddled}, task.id)
        gitops.git("checkout", "--", ".", cwd=worktree, check=False)
        return _retry_or_block(
            fac,
            task,
            f"the reviewer modified the worktree ({len(meddled)} path(s)); "
            "its verdict was discarded",
        )

    effective, override = contracts.effective_verdict(
        review, gate_status=result.status, pkt=pkt
    )
    fac.record_review(
        attempt_id,
        reviewer_model=reviewer_model,
        verdict=review.verdict,
        effective=effective,
        override_reason=override,
        invariants=review.invariants,
        findings=review.findings,
        cost_usd=rev_telemetry.cost_usd,
        billing=cfg.billing,
    )
    fac.event(
        "review",
        {"verdict": review.verdict, "effective": effective, "override": override},
        task.id,
    )

    if effective == "reject":
        fac.set_status(
            task.id, "blocked", blocked_reason="reviewer rejected the approach"
        )
        return "blocked"
    if effective == "revise":
        return _retry_or_block(fac, task, override or "reviewer asked for changes")

    if pkt.gate == "human":
        fac.set_status(
            task.id,
            "awaiting_human",
            blocked_reason=f"passed review; held for human approval (branch {branch})",
        )
        return "awaiting_human"

    return _merge(fac, task, pkt, branch, worktree, cfg, attempt_id)



#: `tasks/README.md` is the board, and the harness writes it — `cmd_run` and
#: `cmd_resume` regenerate it, and `_merge` amends it into the merge commit. So it
#: is dirty as a matter of course, and counting it as the operator's work
#: deadlocked both `approve` and `resume`: the command that leaves it modified is
#: the same one that then refuses over it, and committing it by hand lasts until
#: the next regeneration.
BOARD_REL = "tasks/README.md"


def operator_dirty(repo: Path) -> list[str]:
    """`git status --porcelain` entries the operator is responsible for.

    Everything `is_dirty` reports except the board. The distinction matters
    because the guards built on it refuse to merge over uncommitted work, and a
    guard that fires on the harness's own bookkeeping stops the harness instead
    of protecting anybody.
    """
    return [
        entry
        for entry in gitops.is_dirty(repo)
        if not entry.split(maxsplit=1)[-1].strip().endswith(BOARD_REL)
    ]


def _merge(
    fac: Factory,
    task: Task,
    pkt: Packet,
    branch: str,
    worktree: Path,
    cfg: Config,
    attempt_id: int | None = None,
) -> str:
    fac.set_status(task.id, "merging")

    # The loop checks the repository is clean when it *claims*, and the merge
    # happens an implementation, a gate and a review later. An operator working
    # in the tree during that window is ordinary — and it costs a task a
    # completed, green-gated, review-accepted attempt: `git merge --squash`
    # refuses with "your local changes would be overwritten", the task goes to
    # `needs_work`, and the next attempt re-does work that was already right.
    #
    # So this is checked here rather than discovered from a merge error, and it
    # does not spend the task's budget. A dirty tree is the operator's state,
    # not a defect in the work, and `max_attempts` is a budget for a model
    # having a bad run.
    # `tasks/README.md` is excluded because the harness writes it itself — every
    # path that reaches here has just regenerated the board (`resume` at :1136,
    # the loop at :2234), and this function amends that very file into the merge
    # commit twenty lines below. Counting it as operator work deadlocked
    # `approve`: resume leaves the board dirty, approve refuses over it, and
    # committing it by hand only lasts until the next regeneration.
    dirty = operator_dirty(REPO)
    if dirty:
        paths = [entry.split(maxsplit=1)[-1] for entry in dirty[:10]]
        reason = (
            f"merge onto {cfg.target_branch} was not attempted: the repository has "
            "uncommitted changes that the merge would overwrite"
        )
        fac.event("merge_blocked_dirty_tree", {"paths": paths}, task.id)
        if attempt_id is not None:
            fac.revert_attempt(
                task.id, attempt_id, reason, agent_status="merge_blocked"
            )
        fac.set_status(task.id, "needs_work", blocked_reason=reason)
        print(
            f"\n  {task.id} passed its gate and its review and was NOT merged: the "
            "repository has uncommitted changes.\n"
            + "".join(f"    {p}\n" for p in paths)
            + "  The attempt was not counted against its budget. Commit or stash "
            f"those, then:\n    ./factory/run.py resume {task.id}\n"
            "  which re-gates the work already on its branch instead of building it "
            "again.",
            file=sys.stderr,
        )
        return "merge_blocked"

    scope = f"({cfg.commit_scope})" if cfg.commit_scope else ""
    message = f"feat{scope}: {pkt.goal} [{task.id}]"
    try:
        sha = gitops.squash_merge(REPO, branch, message)
    except gitops.GitError as exc:
        gitops.abort_merge(REPO)
        fac.event("merge_conflict", {"error": str(exc)}, task.id)
        return _retry_or_block(
            fac, task, f"merge onto {cfg.target_branch} failed: {exc}"
        )

    fac.mark_done(task.id, sha)

    # Regenerate the board and fold it into the task's own commit. Written
    # afterwards so it can name the merge sha; amended in rather than committed
    # separately so the tree is left clean — the loop refuses to start on a
    # dirty tree, so a board left modified would block the next run.
    board.write(fac, TASKS_DIR / "README.md", REPO)
    try:
        sha = gitops.amend_with(REPO, ["tasks/README.md"])
        fac.conn.execute("UPDATE tasks SET merged_sha=? WHERE id=?", (sha, task.id))
    except gitops.GitError as exc:
        # Not fatal: the work is merged, only the summary is stale.
        fac.event("board_amend_failed", {"error": str(exc)}, task.id)

    if not cfg.keep_worktrees:
        gitops.worktree_remove(REPO, worktree)
        gitops.branch_delete(REPO, branch)
    return "done"


def _base_sha(fac: Factory, task_id: str) -> str:
    row = fac.conn.execute(
        "SELECT base_sha FROM attempts WHERE task_id=? ORDER BY attempt_no LIMIT 1",
        (task_id,),
    ).fetchone()
    return row["base_sha"] if row else "HEAD"


def _recorded_baseline(fac: Factory, task_id: str) -> int | None:
    payload = _last_event(fac, task_id, "baseline")
    return payload.get("passed") if payload else None


def _last_event(fac: Factory, task_id: str, kind: str) -> dict | None:
    row = fac.conn.execute(
        """SELECT payload FROM events WHERE task_id=? AND kind=?
           ORDER BY id DESC LIMIT 1""",
        (task_id, kind),
    ).fetchone()
    if not row or not row["payload"]:
        return None
    try:
        return json.loads(row["payload"])
    except json.JSONDecodeError:
        return None


def _planted_paths(fac: Factory, task_id: str) -> list[str]:
    payload = _last_event(fac, task_id, "planted_paths")
    return payload.get("files", []) if payload else []


def _retry_or_block(fac: Factory, task: Task, reason: str) -> str:
    """Send a task back for another pass, or stop the queue.

    Exhausting the attempts is deliberately a halt and not a skip: a task that
    failed three times has usually revealed something wrong with the packet, and
    running the next twenty on the same misunderstanding wastes more than it
    saves.
    """
    if task.attempts + 1 >= task.max_attempts:
        fac.set_status(
            task.id,
            "blocked",
            blocked_reason=f"{task.max_attempts} attempts exhausted. Last: {reason}",
        )
        return "blocked"
    fac.set_status(task.id, "needs_work", blocked_reason=reason)
    return "needs_work"


# ---------------------------------------------------------------- commands


def cmd_sync(fac: Factory, args) -> int:
    try:
        packets = load_all(TASKS_DIR)
    except PacketError as exc:
        print(f"packet error: {exc}", file=sys.stderr)
        return 2
    refused: list[tuple[Packet, str, str]] = []
    for i, pkt in enumerate(packets):
        existing = fac.get(pkt.id)
        # A task that already merged is grandfathered: its packet was vetted by
        # whatever process was in use at the time, the work is on the branch, and
        # refusing it now would only corrupt the board with a task that cannot be
        # re-run anyway.
        settled = existing is not None and existing.status == "done"
        if not args.allow_unassessed and not settled:
            # Free checks first, on every packet. They cost milliseconds and
            # catch the defect classes that dominated assessment's findings.
            mechanical = packetlint.lint(pkt, REPO)
            if mechanical:
                refused.append((pkt, "; ".join(str(f) for f in mechanical), "edit"))
                continue
            # An assessment already on disk for this exact packet is honoured
            # whatever the tier policy says. Skipping the requirement is a
            # spend decision; ignoring a recorded verdict would be throwing
            # away a finding somebody has already paid an assessment call for.
            standing = standing_verdict(pkt)
            if standing is not None and standing[0] != "ready":
                refused.append(
                    (pkt, f"assessed `{standing[0]}` — {standing[1]}", "assess")
                )
                continue
            if standing is None and (args.assess_all or needs_model_assessment(pkt)):
                assessed, why = load_assessment(pkt)
                if assessed is None:
                    refused.append((pkt, why, "assess"))
                    continue
        outcome = fac.upsert_packet(pkt.to_meta(REPO), ordinal=i)
        marker = {"new": "+", "changed": "~", "unchanged": " "}[outcome]
        print(f" {marker} {pkt.id:<5} {pkt.tier:<8} {pkt.goal[:60]}")

    gone = fac.prune_missing({p.id for p in packets})
    for task_id in gone:
        print(f" - {task_id:<5} packet removed")
    unreachable = fac.unreachable()
    for task_id, deps in unreachable:
        print(f" ! {task_id:<5} unreachable — needs {sorted(deps)}", file=sys.stderr)

    if refused:
        # The board is regenerated from the database, so writing it now would
        # publish a picture with every refused packet missing — and on a fresh
        # machine, where nothing is `done` yet, that is all of them. The board in
        # git is the only record that survives losing this database; a sync that
        # refused work must not be the thing that erases it.
        print(
            f"\n{len(packets) - len(refused)} packet(s) registered; "
            "tasks/README.md left alone (the sync was incomplete)"
        )
    else:
        board.write(fac, TASKS_DIR / "README.md", REPO)
        print(
            f"\n{len(packets)} packet(s) registered; board written to tasks/README.md"
        )

    if refused:
        print(
            f"\n{len(refused)} packet(s) NOT queued — nothing certifies they are "
            "worth running:",
            file=sys.stderr,
        )
        for pkt, why, kind in refused:
            print(f"  ✗ {pkt.id:<5} {why}", file=sys.stderr)
            if kind == "assess":
                print(f"        ./factory/run.py assess {pkt.id}", file=sys.stderr)
            else:
                # A mechanical finding names its own remedy — the fix is an edit
                # to the packet, and re-running sync re-checks it for free.
                print(f"        edit {pkt.path.name}, then sync again", file=sys.stderr)
        print(
            "\nA packet that is wrong about the world fails identically on every "
            "attempt, so the queue is the wrong place to find that out. Mechanical "
            "findings are always checked; a model assessment is required of "
            "`advanced`-tier and human-gated packets, and `--assess-all` extends "
            "it to the rest. `--allow-unassessed` skips both.",
            file=sys.stderr,
        )
    return 2 if refused else (1 if unreachable else 0)


def cmd_plan(fac: Factory, args) -> int:
    order = fac.runnable_order()
    if not order:
        print("nothing to run")
        return 0
    print(f"{len(order)} task(s), in the order the loop would take them:\n")
    for i, task in enumerate(order, 1):
        gate = " [HUMAN GATE]" if task.gate == "human" else ""
        print(
            f" {i:>3}. {task.id:<5} {task.model:<8} → {task.reviewer:<8} "
            f"{task.goal[:52]}{gate}"
        )
    for task_id, deps in fac.unreachable():
        print(f"\n  ! {task_id} can never run — needs {sorted(deps)}", file=sys.stderr)
    return 0


def cmd_status(fac: Factory, args) -> int:
    print(board.render(fac, REPO))
    return 0


def cmd_approve(fac: Factory, args) -> int:
    task = fac.get(args.task_id)
    if task is None:
        print(f"unknown task {args.task_id}", file=sys.stderr)
        return 2
    if task.status != "awaiting_human":
        print(f"{task.id} is {task.status}, not awaiting_human", file=sys.stderr)
        return 2
    pkt = packets_by_id().get(task.id)
    if pkt is None:
        print(f"packet for {task.id} is gone", file=sys.stderr)
        return 2
    cfg = Config(
        target_branch=args.branch,
        keep_worktrees=args.keep_worktrees,
        commit_scope=args.commit_scope,
    )
    outcome = _merge(
        fac, task, pkt, f"task/{task.id}", STATE / "worktrees" / task.id, cfg
    )
    board.write(fac, TASKS_DIR / "README.md", REPO)
    print(f"{task.id}: {outcome}")
    return 0 if outcome == "done" else 1


def cmd_resume(fac: Factory, args) -> int:
    """Gate+review a worktree HEAD a human already fixed by hand, instead of
    spending an attempt on an implementer that has nothing left to change.

    Only meaningful once a normal attempt has run at least once — it reads
    `base_sha` and the planted-test list back from that attempt's events
    rather than re-deriving them, exactly like a retry in the normal loop
    would.
    """
    task_id = args.task_id
    task = fac.get(task_id)
    if task is None:
        print(f"unknown task {task_id}", file=sys.stderr)
        return 2

    worktree = STATE / "worktrees" / task_id
    if not worktree.exists():
        print(
            f"{task_id} has no worktree to resume from; run it normally first",
            file=sys.stderr,
        )
        return 2

    dirty = gitops.is_dirty(worktree)
    if dirty and not args.allow_dirty:
        print(
            f"{task_id}'s worktree has uncommitted changes; commit the fix "
            "there first so it becomes part of the attempt:\n  "
            + "\n  ".join(dirty[:10]),
            file=sys.stderr,
        )
        return 2

    pkt = packets_by_id().get(task_id)
    if pkt is None:
        print(f"packet for {task_id} is gone", file=sys.stderr)
        return 2

    base_sha = _base_sha(fac, task_id)
    head_sha = gitops.rev_parse(worktree, "HEAD")
    if head_sha == base_sha:
        print(
            f"{task_id}'s worktree HEAD matches its base commit — nothing was "
            "changed there to gate",
            file=sys.stderr,
        )
        return 2

    repo_dirty = operator_dirty(REPO)
    if repo_dirty and not args.allow_dirty:
        print(
            "the repository has uncommitted changes; the factory merges into "
            f"{args.branch} here and will not run over your work:\n  "
            + "\n  ".join(repo_dirty[:10]),
            file=sys.stderr,
        )
        return 2

    task = fac.claim(task_id)
    if task is None:
        print(
            f"{task_id} is not resumable: wrong status or unmet dependencies",
            file=sys.stderr,
        )
        return 2

    cfg = Config(
        target_branch=args.branch,
        skip_permissions=args.dangerously_skip_permissions,
        keep_worktrees=args.keep_worktrees,
        billing=args.billing,
        commit_scope=args.commit_scope,
    )
    branch = f"task/{task_id}"
    attempt_no = fac.attempt_count(task_id) + 1
    run_dir = STATE / "runs" / task_id / f"attempt-{attempt_no}"
    run_dir.mkdir(parents=True, exist_ok=True)

    baseline = _recorded_baseline(fac, task_id)
    planted = _planted_paths(fac, task_id)
    forbidden = sorted(set(pkt.forbidden_paths) | set(planted))

    attempt_id = fac.start_attempt(
        task_id,
        model="human",
        branch=branch,
        base_sha=base_sha,
        run_dir=str(run_dir.relative_to(REPO)),
        billing=cfg.billing,
    )
    fac.finish_attempt(
        attempt_id,
        agent_status="human_fix",
        agent_json={
            "note": "human-applied fix on top of the prior attempt's worktree; "
            "the implementer was not invoked for this attempt"
        },
    )

    outcome = _gate_and_review(
        fac,
        pkt,
        task,
        cfg,
        worktree=worktree,
        branch=branch,
        attempt_id=attempt_id,
        run_dir=run_dir,
        base_sha=base_sha,
        baseline=baseline,
        forbidden=forbidden,
        head_sha=head_sha,
        impl_claim=json.dumps(
            {
                "note": "human-applied fix; the implementer was not invoked "
                "for this attempt.",
                "head_sha": head_sha,
            },
            indent=2,
        ),
    )
    board.write(fac, TASKS_DIR / "README.md", REPO)
    print(f"{task_id}: {outcome}")

    if outcome == "blocked":
        reason = (fac.get(task_id) or task).blocked_reason
        print(f"  reason: {reason}", file=sys.stderr)
    if outcome == "awaiting_human":
        print(
            f"  review:  git diff {cfg.target_branch}...{branch}\n"
            f"  approve: ./factory/run.py approve {task_id}",
            file=sys.stderr,
        )
    return 0 if outcome in ("done", "awaiting_human") else 1


def cmd_reset(fac: Factory, args) -> int:
    task = fac.get(args.task_id)
    if task is None:
        print(f"unknown task {args.task_id}", file=sys.stderr)
        return 2
    if task.status == "done" and not args.force:
        print(
            f"{task.id} is done (merged {task.merged_sha}); its commit is already "
            f"on the target branch and a reset will not revert it. Pass --force "
            "if you really mean to wipe its record and redo it.",
            file=sys.stderr,
        )
        return 2
    if task.status in IN_FLIGHT and not args.force:
        print(
            f"{task.id} is {task.status} — a loop may be actively working it. "
            "Pass --force if you're sure it isn't.",
            file=sys.stderr,
        )
        return 2

    worktree = STATE / "worktrees" / task.id
    branch = f"task/{task.id}"
    gitops.worktree_remove(REPO, worktree)
    if worktree.exists():
        # `worktree remove` is `check=False`: a worktree the git metadata lost
        # track of (killed mid-add, moved by hand) must not survive a reset,
        # since "from scratch" means the next claim sees a genuinely fresh dir.
        shutil.rmtree(worktree, ignore_errors=True)
    gitops.branch_delete(REPO, branch)

    fac.reset(task.id)
    board.write(fac, TASKS_DIR / "README.md", REPO)
    print(f"{task.id}: reset to ready; worktree and branch {branch} removed")
    return 0


# ------------------------------------------------------- the planning tier
#
# `cut` and `assess` run before the queue, not inside it. They are the answer to
# the failure the loop cannot absorb: a packet that is wrong about the world
# fails identically on every attempt, because the retry feeds the same wrong
# packet back in. `max_attempts` is a budget for a model having a bad run, not
# for a defective specification.


def _planning_worktree(name: str, surface: str) -> tuple[Path, str]:
    """A disposable checkout of the target branch for a planning run.

    Isolated for the same reason a task is: both of these agents get a shell and
    a runner so they can check claims against the real tree, and the tree they
    are checking against must not be the one the operator is sitting in.
    """
    worktree = STATE / "worktrees" / name
    branch = f"plan/{name}"
    gitops.worktree_remove(REPO, worktree)
    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)
    gitops.branch_delete(REPO, branch)
    gitops.worktree_add(REPO, worktree, branch, gitops.current_branch(REPO))
    gitops.ensure_excluded(REPO)
    try:
        gitops.install_deps(worktree, surface)
    except gitops.DepsError:
        gitops.worktree_remove(REPO, worktree)
        gitops.branch_delete(REPO, branch)
        raise
    return worktree, branch


def _plant(src: Path, root: Path, rel: str) -> None:
    dst = root / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _dossier(fac: Factory, pkt: Packet, cut_report: Path | None) -> str:
    """What the assessor is allowed to be told beyond the packet itself.

    `assess.md` forbids citing anything that postdates the packet — a later
    revision, a board entry, a commit — because a finding that leans on those is
    one nobody could have produced at the moment it mattered. Two things are
    exempt and belong here: the cut report, which is the cutter's own account of
    what it opened, and this packet's own earlier assessments, which are the
    entire point of a re-cut.
    """
    parts: list[str] = []
    if cut_report is not None and cut_report.is_file():
        parts.append(
            "## The cutter's report\n\n"
            "What the cutter says it opened to verify this packet. Claims in the "
            "packet that cannot be traced to something here are claims nobody "
            "checked — that is the defect, not a gap in the report.\n\n"
            + cut_report.read_text(encoding="utf-8").strip()
        )
    prior = [a for a in fac.prior_assessments(pkt.id) if a["findings"]]
    if prior:
        lines = [
            "## What a previous assessment of this packet found",
            "",
            "This is a re-cut. These findings are from an earlier revision of "
            "this same packet, so they are evidence you may use. Check whether "
            "each was actually addressed rather than assuming it was.",
            "",
        ]
        for entry in prior[:3]:
            lines.append(
                f"**{entry['created_at'][:10]} · {entry['verdict']}** "
                f"(packet {entry['packet_sha256'][:12]}…)"
            )
            for f in entry["findings"]:
                lines.append(
                    f"- `{f.get('severity', '?')}` {f.get('claim', '')} "
                    f"→ {f.get('fix', '')}"
                )
            lines.append("")
        parts.append("\n".join(lines))
    return "\n\n".join(parts) if parts else ""


def assessment_path(task_id: str) -> Path:
    return ASSESSMENTS_DIR / f"{task_id}.json"


def needs_model_assessment(pkt: Packet) -> bool:
    """Whether this packet must be read by an assessor before it may be queued.

    Assessment is the most expensive phase in this harness by a wide margin, and
    the margin is structural rather than a tuning problem. An assessment call
    costs several times an implementation attempt and an order of magnitude more
    than a review, because a review reads a packet and one diff where an assessor
    re-reads the packet against every spec section and repo file it cites.
    Assessment cost is context-volume-bound.

    Against that, `max_attempts` (3 by default) already detects the failure mode
    assessment exists to prevent: **a packet wrong about the world fails
    identically on every attempt.** Exhausting the retry budget costs less than
    an assessment and says the same thing. Implementation is also usually right
    first time, so the retry budget is rarely spent at all.

    So the trigger moves downstream for most packets, and assessment is kept
    where being wrong is expensive to unwind rather than merely annoying:

    * **`advanced` tier** — assigned from coupling, so a wrong premise here is
      one that fails across modules and is expensive to bisect out.
    * **`gate = "human"`** — the operator is about to spend their own attention.
      Handing them a packet nothing vetted wastes the scarcest thing in the loop,
      and the human gate has already caught a false docstring claim that an
      assessment passed.

    What this accepts is real and worth stating. Implementation failure catches a
    packet that is *wrong*; it does not catch one that is *vacuous*. A packet can
    carry a Definition-of-done line turning on a docstring "no longer" saying
    something the file never said — satisfiable by touching nothing, failable by
    no diff.
    `contracts` blocks a merge on an unverifiable **critical** invariant, which
    covers the invariant half of that class, and `packetlint` catches restated
    counts and citations past end-of-file. Neither catches an unfalsifiable
    *prose* criterion. That residue lands on the human gate.
    """
    return pkt.tier == "advanced" or pkt.gate == "human"


def standing_verdict(pkt: Packet) -> tuple[str, str] | None:
    """The effective verdict of an assessment of *this exact packet*, or `None`.

    `None` means there is nothing to honour: never assessed, assessed at a
    different revision, or malformed. It does **not** mean approval.

    This exists so that skipping the *requirement* for an assessment never
    becomes ignoring one that was already paid for. When the tier policy stopped
    requiring assessments of `standard` packets, two packets carrying recorded
    `recut` verdicts — one of them failing `deps-complete`, which means it names
    artifacts that do not exist — became queueable purely because nobody asked.
    Evidence already bought is free to read, and a recorded `recut` is the
    cheapest finding in the system.
    """
    path = assessment_path(pkt.id)
    if not path.is_file():
        return None
    try:
        obj = contracts.read_json(path)
        if str(obj.get("packet_sha256") or "") != pkt.sha256:
            return None
        assessed = contracts.validate_assess(obj, pkt)
    except contracts.ContractError:
        return None
    effective, override = contracts.effective_assessment(assessed)
    return effective, override or "as the assessor ruled"


def load_assessment(pkt: Packet) -> tuple[contracts.AssessResult | None, str]:
    """The gate `sync` applies. Returns `(assessment, why_it_does_not_count)`.

    The sha comparison is the load-bearing half. An assessment of a packet that
    has since been edited is not an assessment of the packet that will run, and
    the realistic way that happens is an operator fixing the very finding the
    assessment raised and then reusing the file that raised it.
    """
    path = assessment_path(pkt.id)
    if not path.is_file():
        return None, "never assessed"
    try:
        obj = contracts.read_json(path)
    except contracts.ContractError as exc:
        return None, str(exc)

    recorded = str(obj.get("packet_sha256") or "")
    if recorded != pkt.sha256:
        return None, (
            f"assessed at a different revision of this packet "
            f"({recorded[:12] or '?'}… vs {pkt.sha256[:12]}… on disk) — it has "
            "been edited since, so re-assess it"
        )

    try:
        assessed = contracts.validate_assess(obj, pkt)
    except contracts.ContractError as exc:
        return None, f"the assessment is malformed: {exc}"

    effective, override = contracts.effective_assessment(assessed)
    if effective != "ready":
        return None, f"assessed `{effective}` — {override or 'as the assessor ruled'}"
    return assessed, ""


def cmd_assess(fac: Factory, args) -> int:
    require_reachable_inputs("assess", allow=args.allow_unreachable_inputs)

    pkt = packets_by_id().get(args.task_id)
    if pkt is None:
        print(f"no packet for {args.task_id} in tasks/", file=sys.stderr)
        return 2

    if not args.force:
        existing, _ = load_assessment(pkt)
        if existing is not None:
            print(
                f"{pkt.id} already has a `ready` assessment pinned to this exact "
                f"packet ({pkt.sha256[:12]}…). Nothing to do; --force to re-run."
            )
            return 0

    # Before the worktree and before the model. An assessor was asked to notice
    # this and caught two packets of four; the two it missed had their whole
    # CREATE list already in the tree, one of them created by a commit whose
    # subject named the same specification section as the packet's goal.
    landed = already_landed(pkt, REPO)
    if landed:
        print(
            f"{pkt.id}: the work is already on the branch. These are listed under "
            f"*Files you may CREATE* and exist:\n"
            + "".join(f"  {p}\n" for p in landed)
            + "A packet cannot create a file that exists. Re-cut it against the "
            "current tree, or retire it — assessing it buys a verdict about work "
            "nobody is going to do.",
            file=sys.stderr,
        )
        return 2

    cut_report = (
        Path(args.cut_report) if args.cut_report else TASKS_DIR / "CUT-REPORT.md"
    )
    if not cut_report.is_absolute():
        cut_report = REPO / cut_report

    run_dir = STATE / "runs" / pkt.id / "assess"
    run_dir.mkdir(parents=True, exist_ok=True)

    worktree, branch = _planning_worktree(f"assess-{pkt.id}", pkt.surface)
    try:
        # The packet and its fixtures may not be committed yet — a fresh cut is
        # untracked by design, and assessing only what is already in git would
        # mean assessing nothing at the moment it matters.
        # Remembered, because the meddling check below cannot otherwise tell a
        # file this harness copied in from one the assessor wrote.
        planted_rels = {pkt.path.relative_to(REPO).as_posix()}
        _plant(pkt.path, worktree, pkt.path.relative_to(REPO).as_posix())
        for src, rel in supplied_for(pkt.id):
            planted = f"tasks/supplied/{pkt.id}/{rel}"
            planted_rels.add(planted)
            _plant(src, worktree, planted)

        prompt = render(
            PROMPTS / "assess.md",
            REPO_NAME=repo_name(),
            FAILURE_CLASSES=failure_classes(),
            PROJECT_TRAPS=project_traps(),
            GROUNDING=grounding_summary(),
            PACKET_ID=pkt.id,
            PACKET_PATH=pkt.path.relative_to(REPO).as_posix(),
            SPEC_PATH=pkt.spec_path,
            PACKET_SHA256=pkt.sha256,
            CUT_REPORT=_dossier(fac, pkt, cut_report),
            ASSESS_PATH=".factory/assess.json",
        )

        assessor_model = agent.resolve(args.tier)
        assessed, telemetry, err = agent.run_with_contract(
            worktree=worktree,
            prompt=prompt,
            model=assessor_model,
            allowed_tools=agent.TOOLS_ASSESS,
            run_dir=run_dir,
            label="assess",
            artifact=worktree / ".factory" / "assess.json",
            validate=lambda obj: contracts.validate_assess(obj, pkt),
            skip_permissions=args.dangerously_skip_permissions,
            billing=args.billing,
        )

        if assessed is None:
            fac.event("assess_contract_failed", {"error": err}, pkt.id)
            print(f"{pkt.id}: the assessment was unusable — {err}", file=sys.stderr)
            return 1

        # An assessor that edited the tree was doing something other than
        # assessing, and whatever it concluded was concluded about a repository
        # nobody else will ever see.
        # A planted path is dirty because the plant above made it dirty. That is
        # invisible while a packet is untracked -- a fresh cut is, by design, so
        # the copy lands as an untracked file that `--untracked-files=no` skips --
        # and it appears the moment a packet is tracked and its working copy
        # differs from the commit at all. Which is the ordinary state after a
        # correction round, and on Windows also after nothing more than a line
        # ending: four assessments were paid for and discarded that way, each
        # reported as the assessor having edited the packet it was judging.
        meddled = [
            entry
            for entry in gitops.is_dirty(worktree)
            if entry.split(maxsplit=1)[-1].strip().strip('"') not in planted_rels
        ]
        if meddled:
            fac.event("assessor_meddled", {"paths": meddled}, pkt.id)
            print(
                f"{pkt.id}: the assessor modified {len(meddled)} tracked path(s); "
                "its verdict was discarded:\n  " + "\n  ".join(meddled[:10]),
                file=sys.stderr,
            )
            return 1

        effective, override = contracts.effective_assessment(assessed)
        fac.record_assessment(
            pkt.id,
            packet_sha256=pkt.sha256,
            assessor_model=assessor_model,
            verdict=assessed.verdict,
            effective=effective,
            override_reason=override,
            claims=assessed.claims,
            structural=assessed.structural,
            findings=assessed.findings,
            cost_usd=telemetry.cost_usd,
            billing=args.billing,
        )

        # Written into git, not just the database. `sync` gates on this file, and
        # a gate that lives only in machine-local state would let a fresh clone
        # queue an unassessed packet without noticing.
        record = dict(assessed.raw)
        record["harness"] = {
            "effective": effective,
            "override_reason": override,
            "assessor_model": assessor_model,
            "assessed_at": db_now(),
        }
        out = assessment_path(pkt.id)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(record, indent=2, ensure_ascii=False), "utf-8")
    finally:
        if not args.keep_worktrees:
            gitops.worktree_remove(REPO, worktree)
            gitops.branch_delete(REPO, branch)

    print(_assessment_summary(pkt, assessed, effective, override, out))
    return 0 if effective == "ready" else 1


def _assessment_summary(
    pkt: Packet,
    assessed: contracts.AssessResult,
    effective: str,
    override: str | None,
    out: Path,
) -> str:
    lines = [
        "",
        f"{pkt.id}: assessor said `{assessed.verdict}`, harness records "
        f"`{effective}`" + (f" — {override}" if override else ""),
        f"  {len(assessed.claims)} claim(s) inventoried; "
        f"{sum(1 for c in assessed.claims if c['status'] != 'held')} not held",
    ]
    for check in assessed.structural:
        mark = "✓" if check["status"] == "pass" else "✗"
        lines.append(f"  {mark} {check['check']:<24} {check['detail'][:60]}")
    for finding in assessed.findings:
        lines.append(f"  · {finding['severity']:<8} {str(finding.get('claim'))[:70]}")
    lines.append(f"\n  written to {out.relative_to(REPO).as_posix()}")
    if effective != "ready":
        lines.append(
            f"  {pkt.id} will not be queued by `sync` until it is re-cut and "
            "assessed again."
        )
    return "\n".join(lines)


class Abandoned(RuntimeError):
    """The operator broke off the interview. Nothing is written."""


def _interview(
    *,
    divergences: list,
    candidates: list,
    declared: list[str],
    spec_path: str,
    ask,
) -> tuple[list[str], str]:
    """The two questions no probe can answer.

    Everything else an earlier draft of this wanted to ask turned out not to
    need asking. The target branch comes from the checkout. Billing is a
    per-run flag that already announces itself. `gate = "human"` is a judgement
    the cutter makes per packet from rules in `cut.md`, not a project constant.
    Tier-to-model is a property of the runtime, not of the repository. Asking
    about any of them would be collecting answers to look thorough, and every
    answer collected is an asserted fact this design otherwise refuses.

    What is left is genuinely unprobeable: which paths the planner must read,
    and which document is the spec of record. Both are then *checked* — the
    answer is recorded next to what a probe found when it went looking, so a
    wrong one shows up as a refusal rather than as a silent bad plan.
    """
    print("\nTwo questions. Both are recorded in git and both get verified.\n")

    print("1. Which paths must the planning tier be able to read?")
    print(
        "   A worktree carries tracked files only, so an untracked input is "
        "invisible\n   to a cutter. Anything named here is checked on every "
        "cut and refused if\n   it cannot be reached.\n"
    )
    unclassified = [d for d in divergences if not d.noise]
    if unclassified:
        print("   Present for you but not for a planner:")
        for i, div in enumerate(unclassified, 1):
            print(f"     {i}. {div.path}")
        print("   Enter numbers (e.g. `1,3`), and/or type paths, comma separated.")
    else:
        print(
            "   Nothing ignored is present here that is not build residue — but\n"
            "   that is not the same as this project having no inputs. An input\n"
            "   nobody has fetched is invisible to the probe *and* to the planner,\n"
            "   which is the case worth catching. Type paths if there are any."
        )
    if declared:
        print(f"   Currently declared: {', '.join(declared)}")
    answer = ask("   > ").strip()

    if answer:
        chosen: list[str] = []
        for token in (t.strip() for t in answer.split(",")):
            if not token:
                continue
            if token.isdigit() and 1 <= int(token) <= len(unclassified):
                chosen.append(unclassified[int(token) - 1].path)
            else:
                chosen.append(token)
        declared = sorted(dict.fromkeys(chosen))

    print("\n2. Which document is the spec of record?")
    print(
        "   Packets pin to its sha, and the drift check watches it. Pointing at\n"
        "   the wrong file makes that check guard something the work has nothing\n"
        "   to do with — it will not error, it will simply never fire.\n"
    )
    for i, cand in enumerate(candidates, 1):
        print(f"     {i}. {cand.path}  ({cand.lines} lines, {cand.last_commit})")
    if spec_path:
        print(f"   Currently: {spec_path}")
    print("   Enter a number or a path; blank to leave unset.")
    answer = ask("   > ").strip()
    if answer:
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            spec_path = candidates[int(answer) - 1].path
        else:
            spec_path = answer

    return declared, spec_path


def _should_interview(args, grounded) -> bool:
    """Ask only when there is someone there to answer.

    An unattended `ground` — CI, a scripted landing, the loop — must never block
    on a prompt, so a non-TTY stdin skips the interview and says what it would
    have asked. That is the same reason `--yes` exists rather than the interview
    being the default.
    """
    if args.yes:
        return False
    if not sys.stdin.isatty():
        return False
    return args.interview or grounded is None


def cmd_ground(fac: Factory, args) -> int:
    """Measure what a planning worktree will and will not contain.

    Invokes no model and costs nothing, which is why the planning commands are
    allowed to insist on it having been run.
    """
    existing = ground.load(GROUNDING_PATH)
    declared = existing.planner_must_read if existing else []
    spec_path = existing.spec_path if existing else ""

    divergences = ground.diverging_paths(REPO)

    asked = False
    if _should_interview(args, existing):
        try:
            declared, spec_path = _interview(
                divergences=divergences,
                candidates=ground.spec_candidates(REPO),
                declared=declared,
                spec_path=spec_path,
                ask=input,
            )
            asked = True
        except EOFError:
            # Nobody was there after all. `isatty` is a hint, not an answer:
            # on Windows `NUL` is a character device, so the usual unattended
            # invocation `ground < /dev/null` reports a terminal and would
            # otherwise hang here or abandon a landing that was fine. EOF on
            # the first read is the authoritative signal, so fall through to
            # the non-interactive path rather than failing.
            print("\n   (no input available — skipping the questions)\n")
        except KeyboardInterrupt:
            # Someone *was* there and changed their mind, which is different.
            print("\nabandoned; nothing written", file=sys.stderr)
            return 1
    if not asked and existing is None and not args.yes:
        print(
            "not a terminal, so the two questions grounding cannot probe are "
            "going unanswered:\n"
            "  · which paths the planning tier must be able to read\n"
            "  · which document is the spec of record\n"
            f"Set them under [inputs] and [spec] in "
            f"{GROUNDING_PATH.relative_to(REPO).as_posix()}, or re-run "
            "interactively.\n",
            file=sys.stderr,
        )

    # Discovered, but an operator's narrowing wins: unlike `planner_must_read`
    # this *is* probeable, so re-deriving it every run would quietly undo a
    # deliberate choice to check fewer documents.
    conventions = (existing.conventions if existing else []) or (
        ground.convention_candidates(REPO)
    )

    checks = ground.check_inputs(REPO, declared)
    surfaces, probes = ground.probe_surfaces(REPO)
    measured, tool_probes = ground.probe_toolchain(REPO, surfaces)
    probes += tool_probes + ground.probe_spec(REPO, spec_path)
    probes.append(
        ground.Probe(
            name="conventions",
            command="git ls-files",
            ok=bool(conventions),
            detail=(
                f"{len(conventions)} document(s) state how this project works; "
                "`run.py conventions` checks their claims"
                if conventions
                else "none found — nothing states how this project works, so "
                "there is nothing to check and nothing to ground a planner on"
            ),
        )
    )
    at = db_now()[:10]
    commit = gitops.rev_parse(REPO, "HEAD")[:7]

    if args.check:
        # Re-measure without rewriting. Only structural change is reported as
        # drift: counts move on every merged task, and a command that cries
        # about the suite growing is one an operator stops running.
        if existing is None:
            raise Halt("nothing to check — this repository has never been grounded")
        fresh = ground.Grounding(at, commit, declared, spec_path, measured, surfaces)
        regressions, improvements = ground.drift(existing, fresh, checks)
        for name, was in sorted(existing.measured.items()):
            now = measured.get(name)
            if now is not None and now != was:
                print(f"  · {name}: {was} → {now}")
        for note in improvements:
            print(f"  ✅ {note}")
        if not regressions:
            print(f"\ngrounding still holds (measured at {existing.grounded_commit})")
            return 0
        for problem in regressions:
            print(f"  ⛔ {problem}", file=sys.stderr)
        raise Halt(
            f"grounding has drifted since {existing.grounded_commit}. Re-run "
            "`./factory/run.py ground` once the cause is understood."
        )

    GROUNDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    GROUNDING_PATH.write_text(
        ground.render(
            grounded_at=at,
            grounded_commit=commit,
            declared=declared,
            divergences=divergences,
            checks=checks,
            spec_path=spec_path,
            conventions=conventions,
            measured=measured,
            surfaces=surfaces,
            probes=probes,
        ),
        encoding="utf-8",
    )
    fac.event(
        "ground",
        {
            "commit": commit,
            "declared": declared,
            "blocking": [c.path for c in checks if c.blocks],
        },
    )

    rel = GROUNDING_PATH.relative_to(REPO).as_posix()
    print(f"grounded at {commit} — wrote {rel}\n")
    for probe in probes:
        print(f"  {'✅' if probe.ok else '⚠️'} {probe.name}: {probe.detail}")
    print()
    unclassified = [d for d in divergences if not d.noise]
    print(
        f"  {len(divergences)} ignored path(s) present here and absent from every "
        f"worktree; {len(unclassified)} not obviously build residue"
    )
    for div in unclassified:
        print(f"    ? {div.path}")

    if not declared:
        print(
            "\n  no input paths declared. If the planning tier is meant to open "
            f"data files, list them under [inputs] in {rel} —\n  nothing can "
            "probe that, and until it is answered the ladder's first rung is "
            "unenforced."
        )
        return 0

    blocked = [c for c in checks if c.blocks]
    for check in checks:
        print(f"    {'⛔' if check.blocks else '✅'} {check.path} — {check.why}")
    if blocked:
        print(
            f"\n  cut and assess will refuse to start while {len(blocked)} "
            "declared input cannot be reached."
        )
    return 0


def cmd_conventions(fac: Factory, args) -> int:
    """Check what the project says about itself against what is true of it.

    The last thing grounding does, and the only part of it that costs a model.
    Everything else here is a command whose output is its own evidence; a
    convention is a sentence, and telling a true one from a stale one needs
    something that can read code.
    """
    grounded = ground.load(GROUNDING_PATH)
    sources = list(args.source) or (grounded.conventions if grounded else [])
    if not sources:
        print(
            "no conventions documents. Run `./factory/run.py ground` to discover "
            "them, set [conventions] sources in "
            f"{GROUNDING_PATH.relative_to(REPO).as_posix()}, or pass --source.",
            file=sys.stderr,
        )
        return 2

    missing = [s for s in sources if not (REPO / s).is_file()]
    if missing:
        print(f"no such document(s): {', '.join(missing)}", file=sys.stderr)
        return 2

    stamp = db_now().replace(":", "").replace("-", "")[:15]
    run_dir = STATE / "runs" / "conventions" / stamp
    run_dir.mkdir(parents=True, exist_ok=True)

    worktree, branch = _planning_worktree(f"conventions-{stamp}", args.surface)
    try:
        listed = "\n".join(f"- `{s}`" for s in sources)
        prompt = render(
            PROMPTS / "conventions.md",
            REPO_NAME=repo_name(),
            SOURCES=listed,
            RESULT_PATH=".factory/conventions.json",
        )
        artifact = worktree / ".factory" / "conventions.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        claims, telemetry, err = agent.run_with_contract(
            worktree=worktree,
            prompt=prompt,
            model=agent.resolve(args.tier),
            allowed_tools=agent.TOOLS_ASSESS,
            run_dir=run_dir,
            label="conventions",
            artifact=artifact,
            validate=lambda obj: contracts.validate_conventions(obj, sources),
            max_turns=args.max_turns,
            skip_permissions=args.dangerously_skip_permissions,
            billing=args.billing,
        )
        fac.event(
            "conventions",
            {
                "sources": sources,
                "cost_usd": telemetry.cost_usd,
                "turns": telemetry.turns,
                "run_dir": str(run_dir.relative_to(REPO)),
            },
        )
        if claims is None:
            print(f"the run produced no usable verdict: {err}", file=sys.stderr)
            return 1

        draft = STATE / "conventions-draft.md"
        draft.write_text(_conventions_draft(sources, claims), encoding="utf-8")

        print(f"\nchecked {len(sources)} document(s) — {len(claims)} claim(s)")
        print(f"  ${telemetry.cost_usd or 0:.2f}, {telemetry.turns or 0} turns\n")
        for claim in claims:
            mark = {"held": "✅", "violated": "⛔", "unverifiable": "?"}[claim.status]
            print(f"  {mark} {claim.source}: {claim.quote[:70]}")
            if claim.status != "held":
                print(f"       {claim.evidence or '(no evidence given)'}")
        proposed = [c for c in claims if c.proposed_trap]
        if proposed:
            print(
                f"\n  {len(proposed)} proposed trap(s) in "
                f"{draft.relative_to(REPO).as_posix()} — promote what earns its "
                "place into tasks/TRAPS.md by hand."
            )
        return 0
    finally:
        if not args.keep_worktrees:
            gitops.worktree_remove(REPO, worktree)
            gitops.branch_delete(REPO, branch)


def _conventions_draft(sources: list[str], claims: list) -> str:
    """Promotable text, written outside `tasks/` on purpose.

    A proposal is not project state. Putting it straight into `tasks/TRAPS.md`
    would ground every future cut on something no operator had read, which is
    the thing `_cut_output` refuses for the cutter and `TOOLS_ASSESS` refuses
    for the assessor.
    """
    lines = [
        "# Proposed traps, from the conventions check",
        "",
        "Nothing here is in effect. Move what earns its place into `tasks/TRAPS.md`,",
        "and delete the rest — a trap that restates something obvious costs context on",
        "every future cut, forever.",
        "",
        f"Checked: {', '.join(f'`{s}`' for s in sources)}",
        "",
    ]
    stale = [c for c in claims if c.status == "violated"]
    if stale:
        lines += [
            "## The documents are wrong about these",
            "",
            "Worth fixing at the source as well — a stale convention is a "
            "confident sentence that gets believed.",
            "",
        ]
        for claim in stale:
            lines += [
                f"- **{claim.source}** — “{claim.quote}”",
                f"  - {claim.evidence}",
            ]
        lines.append("")
    for claim in (c for c in claims if c.proposed_trap):
        lines += [
            f"## `{claim.source}` · {claim.kind} · {claim.status}",
            "",
            f"> {claim.quote}",
            "",
            f"**Evidence:** {claim.evidence or '(none)'}",
            "",
            claim.proposed_trap,
            "",
        ]
    return "\n".join(lines)


def cmd_cut(fac: Factory, args) -> int:
    require_reachable_inputs("cut", allow=args.allow_unreachable_inputs)

    # `--spec` beats grounding, grounding beats nothing. Layer 1 removed the
    # baked-in default because nothing in a *repository* identifies its spec of
    # record — but `tasks/GROUNDING.md` is the operator's answer to exactly that,
    # in git and probed, which is a different thing from a constant in the source.
    grounded = ground.load(GROUNDING_PATH)
    args.spec = args.spec or (grounded.spec_path if grounded else "")
    if not args.spec:
        print(
            "no spec of record. Pass --spec, or set [spec] path in "
            f"{GROUNDING_PATH.relative_to(REPO).as_posix()} and re-run `ground`.",
            file=sys.stderr,
        )
        return 2

    spec = REPO / args.spec
    if not spec.is_file():
        print(f"no spec at {args.spec}", file=sys.stderr)
        return 2

    scope = args.scope
    if args.scope_file:
        scope_file = Path(args.scope_file)
        if not scope_file.is_absolute():
            scope_file = REPO / scope_file
        scope = scope_file.read_text(encoding="utf-8")
    if not (scope or "").strip():
        print("a cut needs a --scope (or --scope-file)", file=sys.stderr)
        return 2

    # The sha of the spec as it stands, which is what every packet from this cut
    # gets pinned to. Taken here rather than left to the model: it is the one
    # fact in a packet the drift check later depends on being exactly right.
    spec_commit = gitops.git(
        "log", "-1", "--format=%h", "--", args.spec, cwd=REPO
    ).strip()
    if not spec_commit:
        print(f"{args.spec} has no commit history to pin to", file=sys.stderr)
        return 2

    stamp = db_now().replace(":", "").replace("-", "")[:15]
    run_dir = STATE / "runs" / "cut" / stamp
    run_dir.mkdir(parents=True, exist_ok=True)

    worktree, branch = _planning_worktree(f"cut-{stamp}", args.surface)
    try:
        prompt = render(
            PROMPTS / "cut.md",
            REPO_NAME=repo_name(),
            FAILURE_CLASSES=failure_classes(),
            PROJECT_TRAPS=project_traps(),
            GROUNDING=grounding_summary(),
            SPEC_COMMIT=spec_commit,
            SPEC_PATH=args.spec,
            SCOPE=scope,
            OUT_DIR="tasks/",
        )
        result = agent.run_resilient(
            worktree=worktree,
            prompt=prompt,
            model=agent.resolve(args.tier),
            allowed_tools=agent.TOOLS_CUT,
            run_dir=run_dir,
            label="cut",
            max_turns=args.max_turns,
            skip_permissions=args.dangerously_skip_permissions,
            billing=args.billing,
        )
        fac.event(
            "cut",
            {
                "spec_commit": spec_commit,
                "cost_usd": result.cost_usd,
                "turns": result.turns,
                "run_dir": str(run_dir.relative_to(REPO)),
            },
        )

        produced, refused = _cut_output(worktree)
        for rel in refused:
            print(
                f"the cutter modified {rel}, which grounds every future cut. "
                "Left alone — promote any new trap by hand from the cut report.",
                file=sys.stderr,
            )
        if not produced:
            print(
                "the cutter produced no files under tasks/. Its transcript is at "
                f"{run_dir.relative_to(REPO).as_posix()}",
                file=sys.stderr,
            )
            return 1

        # A packet that `packet.parse` rejects can never be synced, so it is
        # caught here rather than after it has been copied into the repo and
        # committed. This is the cut's contract, in the same place the other two
        # stages have theirs.
        staged, rejected = [], []
        for rel in produced:
            if _is_packet(rel):
                try:
                    packet_parse(worktree / rel)
                except PacketError as exc:
                    rejected.append((rel, str(exc)))
                    continue
            staged.append(rel)

        for rel in staged:
            _plant(worktree / rel, REPO, rel)

        print(f"\ncut against {args.spec} @ {spec_commit}")
        print(f"  ${result.cost_usd or 0:.2f}, {result.turns or 0} turns\n")
        for rel in staged:
            print(f"  + {rel}")
        for rel, why in rejected:
            print(f"  ! {rel} — not staged: {why}", file=sys.stderr)

        ids = sorted(Path(r).stem.split("-")[0] for r in staged if _is_packet(r))
        if ids:
            print(
                "\nThese are untracked in your working tree and are NOT queued. "
                "Assess each one before it can be:\n"
                + "\n".join(f"  ./factory/run.py assess {i}" for i in ids)
            )
        return 1 if rejected else 0
    finally:
        if not args.keep_worktrees:
            gitops.worktree_remove(REPO, worktree)
            gitops.branch_delete(REPO, branch)


def _is_packet(rel: str) -> bool:
    """`tasks/B01-x.md` yes; `tasks/CUT-REPORT.md` and `tasks/supplied/…` no.

    Delegates to `packet.is_packet_filename` rather than keeping a second copy
    of the rule. The two copies had already diverged — `load_all` was excluding
    non-packets by a denylist of one filename while this used the id prefix —
    and the weaker one decided whether `tasks/` could hold any other document.
    """
    path = Path(rel)
    return len(path.parts) == 2 and is_packet_filename(path.name)


def _cut_output(worktree: Path) -> tuple[list[str], list[str]]:
    """`(copied back, refused)` — paths under `tasks/` the cutter created or
    changed.

    Read from `git status` rather than by globbing the directory: the worktree
    forks from the target branch and so already contains every existing packet,
    and telling the new ones apart by hand is exactly the kind of bookkeeping
    that silently misses one.

    `tasks/TRAPS.md` is refused rather than copied. It grounds every future cut,
    so a cutter editing it is the planning-tier version of a reviewer editing
    the code it is judging — which `TOOLS_ASSESS` prevents outright, and which a
    line in the prompt asking nicely does not. The cutter proposes new traps in
    its report; an operator promotes them.
    """
    out = gitops.git(
        "status", "--porcelain", "--untracked-files=all", "--", "tasks/", cwd=worktree
    )
    paths, refused = [], []
    traps_rel = TRAPS_PATH.relative_to(REPO).as_posix()
    for line in out.splitlines():
        line = line.rstrip()
        if not line:
            continue
        path = line[3:].strip().strip('"')
        if path.endswith("/"):
            continue
        (refused if path == traps_rel else paths).append(path)
    return sorted(paths), sorted(refused)


def run_spend(fac: Factory, spent_before: float) -> float:
    """What *this invocation* has been billed, in dollars.

    The cap is a budget for one run of the queue, so it is measured against a
    baseline taken before the first claim rather than against the project's
    lifetime total. Read the other way the flag is unusable on any project past
    its first few tasks — the default halts the loop before it claims anything,
    and the run reports success having merged nothing.

    Only money that was actually charged counts; see `Factory.total_cost`.
    """
    return fac.total_cost(billed_only=True) - spent_before


def cmd_run(fac: Factory, args) -> int:
    cfg = Config(
        target_branch=args.branch,
        max_cost_usd=args.max_cost,
        skip_permissions=args.dangerously_skip_permissions,
        keep_worktrees=args.keep_worktrees,
        billing=args.billing,
        max_wait_s=args.max_rate_limit_wait,
        max_parsed_wait_s=args.max_parsed_rate_limit_wait,
        commit_scope=args.commit_scope,
    )
    print(
        f"billing: {cfg.billing}"
        + (
            ""
            if cfg.billing == "api"
            else "  (reported costs are notional; --max-cost is advisory)"
        )
    )
    dirty = operator_dirty(REPO)
    if dirty and not args.allow_dirty:
        print(
            "the repository has uncommitted changes; the factory merges into "
            f"{cfg.target_branch} here and will not run over your work:\n  "
            + "\n  ".join(dirty[:10]),
            file=sys.stderr,
        )
        return 2
    if gitops.current_branch(REPO) != cfg.target_branch and not args.allow_dirty:
        print(
            f"checked out {gitops.current_branch(REPO)}, expected {cfg.target_branch}",
            file=sys.stderr,
        )
        return 2

    gitops.ensure_excluded(REPO)

    packets = packets_by_id()
    consecutive_errors = 0
    completed = 0
    stalled = False
    # The cap bounds what *this invocation* spends, not what the project has
    # ever spent. Measured against the lifetime total it is unusable after the
    # first few tasks: the default would halt every established queue before it
    # claimed anything, and an operator who passes `--max-cost 12` meaning "at
    # most twelve dollars this run" gets a run that merges nothing and exits 0.
    # That happened here, which is why this reads a baseline first.
    spent_before = fac.total_cost(billed_only=True)

    while True:
        # Only money that was actually charged counts against the cap. Halting
        # a subscription run on notional spend would stop a queue that cost
        # nothing.
        spent = run_spend(fac, spent_before)
        if spent >= cfg.max_cost_usd:
            print(
                f"\ncost cap reached: ${spent:.2f} this run "
                f">= ${cfg.max_cost_usd:.2f}"
            )
            break

        task = fac.claim(args.task) if args.task else fac.claim_next()
        if task is None:
            if args.task:
                print(
                    f"\n{args.task} is not runnable: unknown id, wrong status, "
                    "or unmet dependencies",
                    file=sys.stderr,
                )
                return 2
            stuck = fac.unreachable()
            if stuck:
                print("\nqueue stalled — unreachable tasks:", file=sys.stderr)
                for task_id, deps in stuck:
                    print(f"  {task_id} needs {sorted(deps)}", file=sys.stderr)
                # Fall through rather than returning: the summary below names
                # the packets waiting at a human gate, and those are the
                # actionable ones. A stall on an unrelated blocked task used to
                # swallow that list entirely, so one task sitting behind a
                # blocked dependency hid every packet waiting to be reviewed.
                stalled = True
            print("\nqueue drained" if not stuck else "")
            break

        pkt = packets.get(task.id)
        if pkt is None:
            fac.set_status(task.id, "blocked", blocked_reason="packet file is missing")
            continue

        print(f"\n▶ {task.id}  {task.model} → {task.reviewer}  {pkt.goal[:60]}")
        try:
            outcome = run_task(fac, pkt, task, cfg)
        except agent.RateLimited as exc:
            fac.event(
                "rate_limit_exhausted",
                {
                    "waited_s": exc.waited_s,
                    "resume_at": exc.resume_at.isoformat() if exc.resume_at else None,
                    "error": str(exc),
                },
                task.id,
            )
            fac.set_status(task.id, "needs_work", blocked_reason=str(exc))
            outcome = "error"
        except (gitops.GitError, gitops.DepsError, agent.AgentError) as exc:
            fac.event("harness_error", {"error": str(exc)}, task.id)
            fac.set_status(task.id, "needs_work", blocked_reason=str(exc))
            outcome = "error"

        split = fac.cost_split()
        print(
            f"  └─ {outcome}  (${split['billed']:.2f} billed"
            + (f", ${split['notional']:.2f} notional)" if split["notional"] else ")")
        )
        board.write(fac, TASKS_DIR / "README.md", REPO)

        if outcome == "error":
            consecutive_errors += 1
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                print(
                    f"\n{consecutive_errors} consecutive environment faults — "
                    "stopping. This is the box, not the work.",
                    file=sys.stderr,
                )
                return 1
        else:
            consecutive_errors = 0

        if outcome == "merge_blocked":
            # Not an environment fault and not retried here: the tree is dirty
            # for every task, so the next one would implement and review work
            # that cannot land either. `_merge` has already said what to do.
            print(
                f"\nthe queue stops here: {task.id} could not merge over your "
                "uncommitted changes.",
                file=sys.stderr,
            )
            return 1
        if outcome == "blocked":
            print(f"\n{task.id} is blocked; the queue stops here.", file=sys.stderr)
            reason = (fac.get(task.id) or task).blocked_reason
            print(f"  reason: {reason}", file=sys.stderr)
            return 1
        if outcome == "awaiting_human":
            # Hold this packet, not the queue. `awaiting_human` is not a claimable
            # status, so the loop will not pick it up again, and anything that
            # depends on it stays un-runnable on its own terms. Everything else
            # continues: holding the queue leaves independent packets idle through
            # a review they have no dependency on, which is a review turnaround
            # added to unrelated work for nothing. The summary is printed once
            # when the queue drains, rather than interrupting the middle of it.
            print(f"  held at its gate; the rest of the queue continues")
        if outcome == "done":
            completed += 1
        if args.once or args.task:
            break

    split = fac.cost_split()
    print(
        f"\n{completed} task(s) merged; ${split['billed']:.2f} billed"
        + (
            f" + ${split['notional']:.2f} notional (subscription)"
            if split["notional"]
            else ""
        )
    )

    held = fac.held()
    if held:
        print(
            f"\n{len(held)} packet(s) passed review and are held for you:",
            file=sys.stderr,
        )
        for task in held:
            print(
                f"\n  {task.id}  {task.goal[:64]}\n"
                f"    review:  git diff {cfg.target_branch}...task/{task.id}\n"
                f"    approve: ./factory/run.py approve {task.id}",
                file=sys.stderr,
            )
        waiting = fac.waiting_on({t.id for t in held})
        if waiting:
            print(
                f"\n  {len(waiting)} more will queue once those are approved: "
                + ", ".join(sorted(waiting)),
                file=sys.stderr,
            )
    # A stall is still a non-zero exit — something in the queue cannot run and
    # somebody has to decide what to do about it — but it now reports the held
    # packets first, because approving those is usually the next useful act and
    # the stall is often about an unrelated task nobody was waiting on.
    return 1 if stalled else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factory", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sy = sub.add_parser("sync", help="register tasks/*.md into the queue")
    sy.add_argument(
        "--allow-unassessed",
        action="store_true",
        help="queue packets that have no `ready` assessment, and skip the "
        "mechanical packet lint too. The escape hatch, not the workflow",
    )
    sy.add_argument(
        "--assess-all",
        action="store_true",
        help="require a `ready` assessment from every packet, not only "
        "`advanced`-tier and human-gated ones. The pre-measurement behaviour: "
        "safer per packet and roughly 8x the cost of letting `max_attempts` "
        "find a wrong packet instead",
    )
    sub.add_parser("plan", help="print the resolved order without running anything")
    sub.add_parser("status", help="print the board")
    gr = sub.add_parser(
        "ground",
        help="measure what a planning worktree will and will not contain; "
        "writes tasks/GROUNDING.md. Invokes no model",
    )
    gr.add_argument(
        "--interview",
        action="store_true",
        help="ask the two questions no probe can answer, even if already "
        "grounded. Implied on a first grounding at a terminal",
    )
    gr.add_argument(
        "--yes",
        action="store_true",
        help="never ask; keep whatever is already declared. For an unattended "
        "landing, where a prompt would hang the run",
    )
    gr.add_argument(
        "--check",
        action="store_true",
        help="re-measure and report drift without rewriting. Non-zero if a "
        "declared input has become unreachable or a surface has gone",
    )

    ct = sub.add_parser(
        "cut",
        help="cut new packets from a slice of the spec (writes them untracked, "
        "for you to assess and commit)",
    )
    ct.add_argument(
        "--scope",
        help="which part of the spec to cut, in prose — e.g. '§5, the income "
        "section and its loader'",
    )
    ct.add_argument("--scope-file", help="read --scope from a file instead")
    # Required, with no default. Nothing in the repository identifies the spec
    # of record, and a wrong one is not a wrong flag — every packet from the cut
    # gets pinned to its sha, so the drift check would then be watching a file
    # the work has nothing to do with, and would stay silent when the real
    # design moved.
    ct.add_argument(
        "--spec",
        default=None,
        help="the spec of record, repo-relative; its current sha is what every "
        "packet from this cut is pinned to. Defaults to [spec] path in "
        "tasks/GROUNDING.md, and is required if that is unset",
    )
    ct.add_argument(
        "--surface",
        choices=("api", "webapp"),
        default="api",
        help="which toolchain to install in the cutter's worktree, so it can "
        "actually run the checks it is told to run",
    )
    # Cutting is where a mistake is cheapest to make and most expensive to keep:
    # a bad packet costs three identical failures and a halted queue. It is the
    # last place to economise on the tier.
    ct.add_argument("--tier", default="advanced", choices=TIERS)
    ct.add_argument("--max-turns", type=int, default=agent.DEFAULT_MAX_TURNS * 3)
    ct.add_argument("--keep-worktrees", action="store_true")
    ct.add_argument("--billing", choices=("api", "subscription"), default="api")
    ct.add_argument(
        "--allow-unreachable-inputs",
        action="store_true",
        help="plan anyway when a declared input cannot be opened from a "
        "worktree. The escape hatch, not the workflow — claims about "
        "those paths are unverifiable by construction",
    )
    ct.add_argument("--dangerously-skip-permissions", action="store_true")

    cv = sub.add_parser(
        "conventions",
        help="check what the project's own documents claim about it against "
        "what is true; proposes traps for an operator to promote",
    )
    cv.add_argument(
        "--source",
        action="append",
        default=[],
        help="a document to check (repeatable). Defaults to [conventions] "
        "sources in tasks/GROUNDING.md",
    )
    cv.add_argument(
        "--surface",
        choices=tuple(gitops.SURFACES),
        default="api",
        help="which toolchain to install, so claims about it can be run",
    )
    # Adversarial, like `assess`: a weaker model reads a confident sentence and
    # agrees with it, which is the one outcome that makes this run worthless.
    cv.add_argument("--tier", default="advanced", choices=TIERS)
    cv.add_argument("--max-turns", type=int, default=agent.DEFAULT_MAX_TURNS * 2)
    cv.add_argument("--keep-worktrees", action="store_true")
    cv.add_argument("--billing", choices=("api", "subscription"), default="api")
    cv.add_argument("--dangerously-skip-permissions", action="store_true")

    asp = sub.add_parser(
        "assess",
        help="rule on a packet before it can be queued; writes "
        "tasks/assessments/<ID>.json",
    )
    asp.add_argument("task_id")
    asp.add_argument(
        "--force",
        action="store_true",
        help="re-assess even if a ready assessment already pins this packet",
    )
    asp.add_argument(
        "--cut-report",
        help="the cutter's report to check the packet against "
        "(default: tasks/CUT-REPORT.md if it exists)",
    )
    # The assessor is the adversary of the cutter; a weaker one just agrees.
    asp.add_argument("--tier", default="advanced", choices=TIERS)
    asp.add_argument("--keep-worktrees", action="store_true")
    asp.add_argument("--billing", choices=("api", "subscription"), default="api")
    asp.add_argument(
        "--allow-unreachable-inputs",
        action="store_true",
        help="plan anyway when a declared input cannot be opened from a "
        "worktree. The escape hatch, not the workflow — claims about "
        "those paths are unverifiable by construction",
    )
    asp.add_argument("--dangerously-skip-permissions", action="store_true")

    run_p = sub.add_parser("run", help="drain the queue")
    run_p.add_argument("--once", action="store_true", help="stop after one task")
    run_p.add_argument(
        "--task",
        help="claim this task specifically instead of the next by queue order "
        "(still requires it to be ready/needs_work with deps satisfied); "
        "implies --once",
    )
    run_p.add_argument(
        "--branch",
        default=None,
        help="the branch merges land on (default: the branch checked out, "
        "which is the one `run` requires anyway)",
    )
    run_p.add_argument(
        "--commit-scope",
        default="",
        help="conventional-commit scope for merge commits, e.g. 'auth' gives "
        "`feat(auth): …`. Default: none, since a scope is a fact about the "
        "project this harness is vendored into and a wrong one is worse than "
        "no scope",
    )
    run_p.add_argument(
        "--max-cost",
        type=float,
        default=25.0,
        help="stop claiming tasks once THIS invocation has been billed this "
        "much. Not a lifetime project budget — see `status` for that. Checked "
        "between tasks, so the task in flight when the cap is reached still "
        "finishes and the run can exceed it by one task",
    )
    run_p.add_argument(
        "--max-rate-limit-wait",
        type=float,
        default=agent.DEFAULT_MAX_WAIT_S,
        help="seconds a single agent call will wait out a rate limit with no "
        "parseable reset hint before giving up and raising into the "
        f"environment-fault path (default: {agent.DEFAULT_MAX_WAIT_S:.0f}s)",
    )
    run_p.add_argument(
        "--max-parsed-rate-limit-wait",
        type=float,
        default=agent.DEFAULT_MAX_PARSED_WAIT_S,
        help="same backstop, but for when the CLI gave an exact reset time — "
        "trusted much further out since it's a fact, not a guess (default: "
        f"{agent.DEFAULT_MAX_PARSED_WAIT_S:.0f}s)",
    )
    run_p.add_argument("--allow-dirty", action="store_true")
    run_p.add_argument("--keep-worktrees", action="store_true")
    run_p.add_argument(
        "--billing",
        choices=("api", "subscription"),
        default="api",
        help="'api' keeps ANTHROPIC_API_KEY so spend is real and the cost cap "
        "is enforceable; 'subscription' scrubs it and bills the logged-in plan",
    )
    run_p.add_argument(
        "--dangerously-skip-permissions",
        action="store_true",
        help="only on a dedicated factory box",
    )

    ap = sub.add_parser("approve", help="merge a task held for human review")
    ap.add_argument("task_id")
    ap.add_argument("--branch", default=None, help="default: the branch checked out")
    ap.add_argument("--commit-scope", default="", help="see `run --commit-scope`")
    ap.add_argument("--keep-worktrees", action="store_true")

    rs = sub.add_parser(
        "resume",
        help="gate+review a task's worktree HEAD as-is, without invoking the "
        "implementer — for a fix applied by hand after a red gate",
    )
    rs.add_argument("task_id")
    rs.add_argument("--branch", default=None, help="default: the branch checked out")
    rs.add_argument("--commit-scope", default="", help="see `run --commit-scope`")
    rs.add_argument("--allow-dirty", action="store_true")
    rs.add_argument("--keep-worktrees", action="store_true")
    rs.add_argument(
        "--billing",
        choices=("api", "subscription"),
        default="api",
        help="passed through to the reviewer call",
    )
    rs.add_argument(
        "--dangerously-skip-permissions",
        action="store_true",
        help="only on a dedicated factory box",
    )

    rp = sub.add_parser(
        "reset", help="wipe a task back to ready and rebuild its worktree from scratch"
    )
    rp.add_argument("task_id")
    rp.add_argument(
        "--force",
        action="store_true",
        help="also reset a 'done' task or one that looks in-flight",
    )

    return parser


def main() -> int:
    args = build_parser().parse_args()

    # The branch merges land on, defaulted from the checkout rather than from a
    # constant. `cmd_run` already refuses to start unless this is the branch
    # checked out, so the checkout is the authoritative answer and a literal
    # here could only ever disagree with it.
    if getattr(args, "branch", None) is None:
        args.branch = gitops.current_branch(REPO)

    # The board and the progress lines use box-drawing and arrows. A Windows
    # console defaults to cp1252 and would raise on the first one.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    STATE.mkdir(parents=True, exist_ok=True)
    fac = Factory(DB_PATH)
    try:
        return {
            "sync": cmd_sync,
            "plan": cmd_plan,
            "status": cmd_status,
            "ground": cmd_ground,
            "cut": cmd_cut,
            "conventions": cmd_conventions,
            "assess": cmd_assess,
            "run": cmd_run,
            "approve": cmd_approve,
            "resume": cmd_resume,
            "reset": cmd_reset,
        }[args.cmd](fac, args)
    except Halt as halt:
        # `Halt` documents itself as carrying the reason to the operator and
        # exiting non-zero, and until now nothing raised it and nothing caught
        # it — so the behaviour its docstring describes did not exist. A stop
        # condition that surfaces as a traceback reads as a crash, which is the
        # wrong thing to read when the harness has correctly refused.
        print(f"\nhalted: {halt}", file=sys.stderr)
        return 3
    finally:
        fac.close()


if __name__ == "__main__":
    raise SystemExit(main())
