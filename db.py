"""Factory state — a thin layer over SQLite.

Task *content* lives in git (`tasks/*.md`); task *state* lives here. The split
exists because the worker agents commit to this same repository: if progress
were tracked in git too, bookkeeping would collide with the work being tracked,
and a rejected attempt would drag its own history behind it forever.

Stdlib only, on purpose. The factory orchestrates the surfaces of the project it
is vendored into and should not live inside any of their virtualenvs — it runs
on a bare `python3`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = Path(__file__).with_name("schema.sql")

# Statuses from which a task can be picked up. `needs_work` is a retry after a
# `revise` verdict; it re-enters the queue rather than blocking it, so an
# unrelated task is not held hostage by one that needs another pass.
CLAIMABLE = ("ready", "needs_work")

TERMINAL = ("done", "blocked")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class Task:
    """A row of `tasks`, as the loop sees it."""

    id: str
    slug: str
    packet_path: str
    packet_sha256: str
    spec_commit: str
    spec_path: str
    model: str
    reviewer: str
    gate: str
    extra_pytest_marker: str
    surface: str
    goal: str
    status: str
    attempts: int
    max_attempts: int
    merged_sha: str | None
    blocked_reason: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Task:
        return cls(
            id=row["id"],
            slug=row["slug"],
            packet_path=row["packet_path"],
            packet_sha256=row["packet_sha256"],
            spec_commit=row["spec_commit"],
            spec_path=row["spec_path"],
            model=row["model"],
            reviewer=row["reviewer"],
            gate=row["gate"],
            extra_pytest_marker=row["extra_pytest_marker"],
            surface=row["surface"],
            goal=row["goal"],
            status=row["status"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            merged_sha=row["merged_sha"],
            blocked_reason=row["blocked_reason"],
        )


class Factory:
    """The queue. One instance per run; not thread-safe by design."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        self._migrate()

    def _migrate(self) -> None:
        """Add columns to a database created by an earlier schema.

        `CREATE TABLE IF NOT EXISTS` silently leaves an existing table alone, so
        a new column in schema.sql never reaches a database that already exists.
        """
        for table, column, ddl in (
            ("attempts", "billing", "TEXT NOT NULL DEFAULT 'api'"),
            ("reviews", "billing", "TEXT NOT NULL DEFAULT 'api'"),
            ("tasks", "extra_pytest_marker", "TEXT NOT NULL DEFAULT ''"),
        ):
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------------- events

    def event(self, kind: str, payload: Any = None, task_id: str | None = None) -> None:
        """Append to the audit log. Never raises on a payload that will not
        serialize — a broken log entry must not take the run down."""
        try:
            blob = json.dumps(payload, default=str) if payload is not None else None
        except (TypeError, ValueError):
            blob = json.dumps({"unserializable": repr(payload)})
        self.conn.execute(
            "INSERT INTO events (ts, task_id, kind, payload) VALUES (?, ?, ?, ?)",
            (now(), task_id, kind, blob),
        )

    # ----------------------------------------------------------------- sync

    def upsert_packet(self, meta: dict[str, Any], ordinal: int) -> str:
        """Register (or refresh) a packet. Returns 'new', 'unchanged' or
        'changed'.

        A packet whose content hash moved is reported as 'changed' so the caller
        can decide what that means — for a task already `done` it is a warning
        that the record no longer matches what was built.
        """
        cur = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (meta["id"],))
        existing = cur.fetchone()
        fields = {
            "slug": meta["slug"],
            "packet_path": meta["packet_path"],
            "packet_sha256": meta["packet_sha256"],
            "spec_commit": meta["spec_commit"],
            "spec_path": meta["spec_path"],
            "model": meta["model"],
            "reviewer": meta["reviewer"],
            "gate": meta["gate"],
            "extra_pytest_marker": meta["extra_pytest_marker"],
            "surface": meta["surface"],
            "goal": meta["goal"],
            "max_attempts": meta["max_attempts"],
            "ordinal": ordinal,
            "updated_at": now(),
        }
        if existing is None:
            self.conn.execute(
                f"""INSERT INTO tasks (id, status, created_at, {",".join(fields)})
                    VALUES (?, 'ready', ?, {",".join("?" * len(fields))})""",
                (meta["id"], now(), *fields.values()),
            )
            outcome = "new"
        else:
            # Never rewind a task the loop already owns; only the descriptive
            # fields are refreshed. Status transitions belong to the loop.
            self.conn.execute(
                f"UPDATE tasks SET {','.join(f'{k}=?' for k in fields)} WHERE id=?",
                (*fields.values(), meta["id"]),
            )
            outcome = (
                "unchanged"
                if existing["packet_sha256"] == meta["packet_sha256"]
                else "changed"
            )

        self.conn.execute("DELETE FROM task_deps WHERE task_id = ?", (meta["id"],))
        for dep in meta["requires"]:
            self.conn.execute(
                "INSERT INTO task_deps (task_id, depends_on) VALUES (?, ?)",
                (meta["id"], dep),
            )
        return outcome

    def prune_missing(self, keep: set[str]) -> list[str]:
        """Drop rows whose packet file is gone, unless the task already merged —
        a merged task's history is evidence and is never deleted."""
        rows = self.conn.execute(
            "SELECT id FROM tasks WHERE status <> 'done'"
        ).fetchall()
        gone = [r["id"] for r in rows if r["id"] not in keep]
        for task_id in gone:
            self.conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return gone

    # ----------------------------------------------------------------- claim

    def claim_next(self) -> Task | None:
        """Atomically take the next runnable task.

        `BEGIN IMMEDIATE` takes the write lock before the SELECT, so two loops
        pointed at one database cannot claim the same row. Ordering is by
        explicit ordinal then id, which makes the queue deterministic and
        `--dry-run` honest about what will happen.
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT * FROM runnable ORDER BY ordinal, id LIMIT 1"
            ).fetchone()
            if row is None:
                self.conn.execute("COMMIT")
                return None
            self.conn.execute(
                "UPDATE tasks SET status='running', updated_at=? WHERE id=?",
                (now(), row["id"]),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return Task.from_row(
            self.conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (row["id"],)
            ).fetchone()
        )

    def claim(self, task_id: str) -> Task | None:
        """Atomically claim one specific task, bypassing ordinal order.

        For an operator saying "run this one next" — e.g. retrying a
        `needs_work` task out of turn. Still goes through the `runnable` view,
        so status and unmet dependencies are enforced exactly as in
        `claim_next`; only the ordering is skipped. Returns None if the task
        doesn't exist, isn't runnable, or has unmet dependencies.
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT * FROM runnable WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                self.conn.execute("COMMIT")
                return None
            self.conn.execute(
                "UPDATE tasks SET status='running', updated_at=? WHERE id=?",
                (now(), task_id),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return Task.from_row(
            self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        )

    def runnable_order(self) -> list[Task]:
        """The queue as it would be drained, for `--dry-run`. Simulates the
        dependency unlock rather than reporting only what is runnable now."""
        rows = self.conn.execute("SELECT * FROM tasks").fetchall()
        by_id = {r["id"]: r for r in rows}
        deps: dict[str, set[str]] = {r["id"]: set() for r in rows}
        for d in self.conn.execute("SELECT * FROM task_deps"):
            if d["task_id"] in deps:
                deps[d["task_id"]].add(d["depends_on"])

        # `blocked` is neither done nor pending: it cannot run, and it does not
        # satisfy anything downstream. Its dependents therefore fall out of the
        # simulation and are reported by `unreachable()`.
        done = {r["id"] for r in rows if r["status"] == "done"}
        pending = {r["id"] for r in rows if r["status"] not in TERMINAL}
        ordered: list[Task] = []
        while True:
            free = sorted(
                (i for i in pending if deps[i] <= done),
                key=lambda i: (by_id[i]["ordinal"], i),
            )
            if not free:
                break
            for i in free:
                ordered.append(Task.from_row(by_id[i]))
                done.add(i)
                pending.discard(i)
        return ordered

    def unreachable(self) -> list[tuple[str, set[str]]]:
        """Tasks that can **never** run: a missing, blocked, or cyclic dependency.

        Never, not merely not-yet. A task waiting behind one held at a human gate
        will run the moment that gate is approved, and reporting it as stalled
        turns an ordinary hold into an error — which is what happened while the
        queue stopped dead on the first `awaiting_human` and this check reported its
        dependents as stuck.

        Computed as a fixpoint rather than from `runnable_order`, because the
        distinction needs transitive reachability in both directions: a dependency
        that is `awaiting_human` can still become `done`, so its dependents are
        pending; one that is `blocked` or absent cannot, so its dependents are
        genuinely unreachable. Iterating to a fixpoint also keeps the cycle
        detection the previous implementation got from `runnable_order` — two
        tasks depending on each other never enter the set, whatever their status.
        """
        rows = self.conn.execute("SELECT * FROM tasks").fetchall()
        status = {r["id"]: r["status"] for r in rows}
        deps: dict[str, set[str]] = {r["id"]: set() for r in rows}
        for d in self.conn.execute("SELECT * FROM task_deps"):
            if d["task_id"] in deps:
                deps[d["task_id"]].add(d["depends_on"])

        # A state a task can still leave under its own steam, or with a human's
        # approval. `blocked` is not one: it needs a `reset` first, which is a
        # decision rather than a step.
        PENDING = (
            "ready",
            "needs_work",
            "running",
            "gating",
            "reviewing",
            "merging",
            "awaiting_human",
        )
        eventually = {i for i, s in status.items() if s == "done"}
        changed = True
        while changed:
            changed = False
            for task_id, required in deps.items():
                if task_id in eventually or status.get(task_id) not in PENDING:
                    continue
                if all(dep in eventually for dep in required):
                    eventually.add(task_id)
                    changed = True

        out: list[tuple[str, set[str]]] = []
        for row in rows:
            if row["status"] in TERMINAL or row["id"] in eventually:
                continue
            out.append((row["id"], deps[row["id"]]))
        return out

    def waiting_on(self, ids: set[str]) -> list[str]:
        """Ids of pending tasks that depend, directly or transitively, on *ids*.

        For the message a held gate should carry: "approving this releases four
        others" is the fact that decides whether a review is worth doing now or
        after lunch, and it is not visible from the packet.
        """
        deps: dict[str, set[str]] = {}
        for row in self.conn.execute(
            f"SELECT id FROM tasks WHERE status NOT IN {TERMINAL}"
        ):
            deps[row["id"]] = set()
        for d in self.conn.execute("SELECT * FROM task_deps"):
            if d["task_id"] in deps:
                deps[d["task_id"]].add(d["depends_on"])
        blocked = set(ids)
        changed = True
        while changed:
            changed = False
            for task_id, required in deps.items():
                if task_id not in blocked and required & blocked:
                    blocked.add(task_id)
                    changed = True
        return sorted(blocked - set(ids))

    def held(self) -> list[Task]:
        """Tasks stopped at a human gate, in queue order.

        The run loop prints these as a group when it drains, so a queue that held
        several packets says so once at the end rather than once per packet in the
        middle of unrelated work.
        """
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE status = 'awaiting_human' "
            "ORDER BY ordinal, id"
        ).fetchall()
        return [Task.from_row(r) for r in rows]

    # ---------------------------------------------------------------- status

    def set_status(
        self, task_id: str, status: str, *, blocked_reason: str | None = None
    ) -> None:
        self.conn.execute(
            "UPDATE tasks SET status=?, blocked_reason=?, updated_at=? WHERE id=?",
            (status, blocked_reason, now(), task_id),
        )
        self.event("status", {"status": status, "reason": blocked_reason}, task_id)

    def reset(self, task_id: str) -> None:
        """Wipe a task back to `ready`, discarding its attempt and review history.

        Deliberately not the same thing as a retry (`needs_work`), which keeps
        prior findings so the next attempt is told what it got wrong. A reset is
        for when that history should *not* carry forward: an `awaiting_human`
        task whose approach the operator rejects outright, or a worktree that
        needs to be rebuilt clean rather than resumed. The `events` row this
        writes is the only trace left of the discarded run.
        """
        row = self.conn.execute(
            "SELECT status, attempts, blocked_reason FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise KeyError(task_id)
        self.event(
            "reset",
            {
                "from_status": row["status"],
                "attempts": row["attempts"],
                "blocked_reason": row["blocked_reason"],
            },
            task_id,
        )
        self.conn.execute("DELETE FROM attempts WHERE task_id=?", (task_id,))
        self.conn.execute(
            """UPDATE tasks SET status='ready', attempts=0, blocked_reason=NULL,
               merged_sha=NULL, updated_at=? WHERE id=?""",
            (now(), task_id),
        )

    def mark_done(self, task_id: str, merged_sha: str) -> None:
        self.conn.execute(
            """UPDATE tasks SET status='done', merged_sha=?, blocked_reason=NULL,
               updated_at=? WHERE id=?""",
            (merged_sha, now(), task_id),
        )
        self.event("merged", {"sha": merged_sha}, task_id)

    def get(self, task_id: str) -> Task | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return Task.from_row(row) if row else None

    def all_tasks(self) -> list[Task]:
        return [
            Task.from_row(r)
            for r in self.conn.execute("SELECT * FROM tasks ORDER BY ordinal, id")
        ]

    # -------------------------------------------------------------- attempts

    def attempt_count(self, task_id: str) -> int:
        """Legs run so far, including any later reverted by `revert_attempt`.

        This is the numbering source for `.factory/runs/<id>/attempt-<n>/` —
        deliberately never decremented, so a reverted leg's transcript is
        never overwritten by the retry that follows it.
        """
        return self.conn.execute(
            "SELECT COUNT(*) AS c FROM attempts WHERE task_id=?", (task_id,)
        ).fetchone()["c"]

    def start_attempt(
        self,
        task_id: str,
        *,
        model: str,
        branch: str,
        base_sha: str,
        run_dir: str,
        billing: str = "api",
    ) -> int:
        self.conn.execute(
            "UPDATE tasks SET attempts = attempts + 1, updated_at=? WHERE id=?",
            (now(), task_id),
        )
        n = self.attempt_count(task_id) + 1
        cur = self.conn.execute(
            """INSERT INTO attempts
               (task_id, attempt_no, model, branch, base_sha, started_at, run_dir,
                billing)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, n, model, branch, base_sha, now(), run_dir, billing),
        )
        self.event("attempt_started", {"attempt": n, "model": model}, task_id)
        return int(cur.lastrowid or 0)

    def revert_attempt(
        self,
        task_id: str,
        attempt_id: int,
        reason: str,
        *,
        agent_status: str = "rate_limited",
    ) -> None:
        """Undo `start_attempt`'s budget bump for a leg the task is not at fault
        for — a rate limit the CLI reported before the model did any work, or a
        merge the operator's own uncommitted changes refused.

        The `attempts` row (and its `run_dir` transcript) is left in place and
        marked finished, so the leg is still visible in history; only the
        `tasks.attempts` cap counter — what `max_attempts` and the board check
        — is rolled back, so a string of such faults can't exhaust a task's
        real retry budget on things that were never its fault.

        `agent_status` is a parameter because the two callers mean different
        things by it. A rate limit means the model never ran. A blocked merge
        means it ran, the gate passed and the reviewer accepted — overwriting
        that with `rate_limited` would erase the only record that the work was
        finished and good.
        """
        self.conn.execute(
            "UPDATE tasks SET attempts = MAX(attempts - 1, 0), updated_at=? WHERE id=?",
            (now(), task_id),
        )
        self.finish_attempt(attempt_id, agent_status=agent_status)
        self.event("attempt_reverted", {"reason": reason}, task_id)

    def finish_attempt(self, attempt_id: int, **fields: Any) -> None:
        """Record the outcome. `agent_status` and `gate_status` are written
        together but never conflated: the first is a claim, the second is a
        measurement."""
        fields["ended_at"] = now()
        for key in ("agent_json", "gate_json"):
            if key in fields and not isinstance(fields[key], str | type(None)):
                fields[key] = json.dumps(fields[key], default=str)
        self.conn.execute(
            f"UPDATE attempts SET {','.join(f'{k}=?' for k in fields)} WHERE id=?",
            (*fields.values(), attempt_id),
        )

    def record_review(
        self,
        attempt_id: int,
        *,
        reviewer_model: str,
        verdict: str,
        effective: str,
        override_reason: str | None,
        invariants: Any,
        findings: Any,
        cost_usd: float | None,
        billing: str = "api",
    ) -> None:
        self.conn.execute(
            """INSERT INTO reviews (attempt_id, reviewer_model, verdict, effective,
                   override_reason, invariants_json, findings_json, cost_usd,
                   billing, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                attempt_id,
                reviewer_model,
                verdict,
                effective,
                override_reason,
                json.dumps(invariants, default=str),
                json.dumps(findings, default=str),
                cost_usd,
                billing,
                now(),
            ),
        )

    def record_assessment(
        self,
        task_id: str,
        *,
        packet_sha256: str,
        assessor_model: str,
        verdict: str,
        effective: str,
        override_reason: str | None,
        claims: Any,
        structural: Any,
        findings: Any,
        cost_usd: float | None,
        billing: str = "api",
    ) -> None:
        self.conn.execute(
            """INSERT INTO assessments (task_id, packet_sha256, assessor_model,
                   verdict, effective, override_reason, claims_json,
                   structural_json, findings_json, cost_usd, billing, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                packet_sha256,
                assessor_model,
                verdict,
                effective,
                override_reason,
                json.dumps(claims, default=str),
                json.dumps(structural, default=str),
                json.dumps(findings, default=str),
                cost_usd,
                billing,
                now(),
            ),
        )

    def prior_assessments(self, task_id: str) -> list[dict[str, Any]]:
        """Every assessment of this packet, newest first.

        A re-cut is the one case where the assessor is allowed to be told what a
        previous pass found — `assess.md` otherwise forbids citing anything that
        postdates the packet, and this is the carve-out it names.
        """
        rows = self.conn.execute(
            """SELECT packet_sha256, effective, findings_json, created_at
               FROM assessments WHERE task_id = ? ORDER BY id DESC""",
            (task_id,),
        ).fetchall()
        return [
            {
                "packet_sha256": r["packet_sha256"],
                "verdict": r["effective"],
                "findings": json.loads(r["findings_json"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def prior_findings(self, task_id: str) -> list[dict[str, Any]]:
        """Every finding from previous attempts, newest first.

        This is what makes a retry different from a re-run: attempt N+1's prompt
        carries attempt N's blockers verbatim, so the model is told what it got
        wrong rather than left to rediscover it.
        """
        rows = self.conn.execute(
            """SELECT a.attempt_no, r.effective, r.findings_json, r.invariants_json
               FROM reviews r JOIN attempts a ON a.id = r.attempt_id
               WHERE a.task_id = ? ORDER BY a.attempt_no DESC""",
            (task_id,),
        ).fetchall()
        return [
            {
                "attempt": r["attempt_no"],
                "verdict": r["effective"],
                "findings": json.loads(r["findings_json"]),
                "invariants": json.loads(r["invariants_json"]),
            }
            for r in rows
        ]

    def total_cost(self, *, billed_only: bool = False) -> float:
        """Spend across every attempt, review and assessment.

        Assessments are counted because they are the same money out of the same
        budget, and because leaving them out would flatter the factory exactly
        where it is most expensive: cutting and vetting a packet, not running it.

        `billed_only` restricts it to work that was actually charged. Under
        subscription auth the CLI still reports a cost — computed from token
        counts and list prices — but nothing was billed, so summing the two
        kinds together produces a number that is neither. The cost cap uses
        `billed_only`, because a cap on notional spend halts a run that cost
        nothing.
        """
        where = " WHERE billing = 'api'" if billed_only else ""
        total = 0.0
        for table in ("attempts", "reviews", "assessments"):
            row = self.conn.execute(
                f"SELECT COALESCE(SUM(cost_usd), 0) AS c FROM {table}{where}"
            ).fetchone()
            total += float(row["c"])
        return total

    def cost_split(self) -> dict[str, float]:
        """Billed vs notional, kept apart so neither is mistaken for the other."""
        return {
            "billed": self.total_cost(billed_only=True),
            "notional": self.total_cost() - self.total_cost(billed_only=True),
        }

    def cost_by_task(self) -> dict[str, dict[str, float]]:
        """Implementation vs review vs assessment spend per task — the number
        that answers whether cheap-model-plus-review actually beat doing it
        directly.

        Assessment is a separate column rather than folded into the total,
        because it answers a different question. `impl + review` is what the task
        cost to run; `assess` is what it cost to be sure the packet was worth
        running, and that is the half the design keeps claiming is the expensive
        one. Summing them would hide the comparison.
        """
        out: dict[str, dict[str, float]] = {}
        for row in self.conn.execute(
            """SELECT a.task_id,
                      COALESCE(SUM(a.cost_usd), 0) AS impl,
                      COALESCE(SUM(r.cost_usd), 0) AS review
               FROM attempts a LEFT JOIN reviews r ON r.attempt_id = a.id
               GROUP BY a.task_id"""
        ):
            out[row["task_id"]] = {
                "impl": float(row["impl"]),
                "review": float(row["review"]),
                "assess": 0.0,
            }
        for row in self.conn.execute(
            """SELECT task_id, COALESCE(SUM(cost_usd), 0) AS assess
               FROM assessments GROUP BY task_id"""
        ):
            entry = out.setdefault(
                row["task_id"], {"impl": 0.0, "review": 0.0, "assess": 0.0}
            )
            entry["assess"] = float(row["assess"])
        return out
