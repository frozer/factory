"""Headless Claude, invoked in a task's worktree.

One function runs a model against a prompt and returns telemetry; the caller
reads whatever file the model was told to write. Nothing here interprets the
model's prose — the structured response is always a file on disk, validated by
`contracts`, because a model's final message is the least reliable part of its
output and the most tempting thing to parse.

Cost and turn counts come back from `--output-format json` and are recorded per
attempt. That is not incidental telemetry: the entire premise of this factory is
that a cheap model plus a review is cheaper than doing the work directly, and
these numbers are what will eventually confirm or refute it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# A task that has not finished in this many turns is not one turn away.
DEFAULT_MAX_TURNS = 60

TIMEOUT_S = 3600

# How long `run_resilient` is willing to wait out a rate limit before giving
# up and raising — the backstop for when the box really is broken rather than
# just between session windows. Two budgets, because a reset hint the CLI
# actually gave us is a fact, not a guess: B07's fault named a same-evening
# session reset ("resets 7pm"); C07's named a weekly-limit reset almost 13h
# out, which blew through this cap even though the CLI told us exactly when
# it would clear. Backoff with no parseable hint stays on the tight budget —
# that case is genuine uncertainty about whether the box is broken at all.
DEFAULT_MAX_WAIT_S = 6 * 3600
DEFAULT_MAX_PARSED_WAIT_S = 8 * 24 * 3600

_BACKOFF_START_S = 30.0
_BACKOFF_MAX_S = 30 * 60.0
_BACKOFF_FACTOR = 2

_ONE_DAY = timedelta(days=1)

# Which model buys a tier. Packets name a tier and never a model: `basic` is a
# claim about how much capability the task needs, which is a property of the
# packet, while which model supplies it is a property of this runtime and will
# change without any packet changing.
#
# This table is the only place in the factory where a concrete model name
# appears. Everything upstream of it — packets, the queue, the board — speaks
# tiers; `run.py` resolves once per call site, and records what it resolved.
#
# ⚠️ The tier calibration in `prompts/cut.md` and `prompts/assess.md` was
# measured against exactly these three models, and the numbers there name them
# for that reason. Repoint a tier and the corresponding measurement stops being
# evidence about the thing it is quoted as evidence for — re-measure, or say in
# the prompt which model the old number came from.
TIER_MODELS = {
    "basic": "haiku",
    "standard": "sonnet",
    "advanced": "opus",
}


def resolve(tier: str) -> str:
    """The model that currently implements `tier`.

    Raises on an unknown tier rather than falling back to a default: a typo
    that silently ran the cheapest model would surface as a task that failed
    for no visible reason.
    """
    try:
        return TIER_MODELS[tier]
    except KeyError:
        raise AgentError(
            f"unknown tier {tier!r}; expected one of {tuple(TIER_MODELS)}"
        ) from None


# The implementer needs to run the project's own verification; the reviewer must
# not be able to run anything at all. `Write` is on the reviewer's list only so
# it can produce review.json — the harness separately asserts that the worktree
# is unchanged after a review, which is what actually keeps a reviewer honest.
# Read-only shell, added after the first run.
#
# Cost on this workload is dominated by context re-read, not by code written —
# S08a read 875,000 cached tokens to produce 7,000. Each turn is another full
# re-read, so a denied tool call is not a small waste: it costs a whole turn for
# no output. The first implement leg logged four denials and burned three turns
# inventing PowerShell workarounds for a command the allowlist had made
# impossible.
#
# Compound commands are checked per segment — which is how `cd api && uv run
# pytest` came to be denied for its first segment — so nothing below can be
# chained into a command that is not itself allowed.
INSPECTION_SHELL = (
    "Bash(ls *)",
    "Bash(cat *)",
    "Bash(head *)",
    "Bash(tail *)",
    "Bash(wc *)",
    "Bash(find *)",
    "Bash(grep *)",
    "Bash(which *)",
    "Bash(pwd)",
    "Bash(cd *)",
    # ⚠️ `awk` is the one entry here that is not strictly read-only: `print >
    # "f"` writes files and `system()` runs arbitrary commands, so allowing it
    # is closer to granting a shell than to granting `grep`. Included
    # deliberately — the worktree is disposable, the forbidden-path gate catches
    # any modification to a protected file, and the harness re-runs the tests
    # itself — but it is not in the same class as the rest of this list.
    "Bash(awk *)",
    # The worker is told not to commit, but seeing its own work is reasonable.
    "Bash(git status *)",
    "Bash(git diff *)",
    "Bash(git log *)",
    "Bash(git show *)",
)

_BASE = ("Read", "Write", "Edit", "Glob", "Grep")

TOOLS_IMPL = {
    "api": ",".join((*_BASE, *INSPECTION_SHELL, "Bash(uv run *)")),
    "webapp": ",".join((*_BASE, *INSPECTION_SHELL, "Bash(npm run *)", "Bash(npm ci)")),
}

# The reviewer keeps no shell at all. It logged zero denials, so there is no
# evidence it needs one, and its `Write` is already the sharp edge — the harness
# separately asserts the worktree is unchanged after a review. Widening a
# surface without evidence is how a review stops being read-only.
TOOLS_REVIEW = "Read,Glob,Grep,Write"

# The planning tier — cutting a packet, and assessing one — is the opposite case
# from the reviewer. Both prompts are built around checking claims against the
# thing that actually decides them: the bytes of a data file, a real signature,
# what ruff does when run from inside `api/`. A planner without a shell can only
# restate the spec, which is precisely the failure both were written to stop. So
# they get the implementer's inspection shell and both runners.
_PLANNING = (*INSPECTION_SHELL, "Bash(uv run *)", "Bash(npm run *)")

# The cutter authors packets and fixtures, so it keeps `Edit` to revise its own
# output — a file it wrote a moment ago is not the repository. It works in a
# disposable worktree and only `tasks/` is copied back out.
TOOLS_CUT = ",".join(("Read", "Write", "Edit", "Glob", "Grep", *_PLANNING))

# The assessor gets no `Edit` at all. Its entire output is one verdict file, and
# a run that can edit the thing it is judging will eventually fix a defect
# quietly instead of reporting it — which reads as a clean assessment of a
# packet that is still broken for everyone who runs it later.
TOOLS_ASSESS = ",".join(("Read", "Write", "Glob", "Grep", *_PLANNING))

# Parent-session variables that must not reach a worker. Left in place they tell
# the child it is a continuation of the session that launched it, which is not
# what a fresh worker in a disposable worktree should believe.
_SESSION_VARS = (
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_PID",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
)


def child_env(billing: str) -> dict[str, str]:
    """The environment a worker runs in.

    `billing` decides how the CLI authenticates, and it is an explicit choice
    because the default is not obvious:

    * `api` keeps `ANTHROPIC_API_KEY`, so the CLI bills the Anthropic API per
      token. `total_cost_usd` is then real money — which is what makes the cost
      cap enforceable and the "was the cheap model actually cheaper" comparison
      worth running. It also keeps an unattended queue off the operator's
      interactive rate limits.
    * `subscription` removes the key so the CLI falls back to logged-in
      credentials. Cheaper if the plan already covers it, but the reported cost
      becomes notional, so `--max-cost` is then policing a number that does not
      correspond to a bill.

    Inheriting the key silently — which is what happened on the first real run —
    is the one option that is not a decision.
    """
    env = dict(os.environ)
    for name in _SESSION_VARS:
        env.pop(name, None)
    if billing == "subscription":
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


class AgentError(RuntimeError):
    """The CLI could not be run at all — an environment fault, not a bad result."""


class RateLimited(AgentError):
    """A 429/session-limit fault the CLI reported at the API level, still
    unresolved after `run_resilient` waited up to its configured cap.

    Subclasses `AgentError` on purpose: the harness already routes that
    exception to a non-attempt-burning outcome with its own circuit breaker
    (`cmd_run`'s `consecutive_errors`), which is the correct backstop here —
    "the box is still rate-limited after waiting" is an environment fault,
    not evidence the task or the model is wrong.
    """

    def __init__(self, message: str, *, resume_at: datetime | None, waited_s: float):
        super().__init__(message)
        self.resume_at = resume_at
        self.waited_s = waited_s


@dataclass(frozen=True, slots=True)
class AgentRun:
    ok: bool
    session_id: str | None
    cost_usd: float | None
    duration_s: float | None
    turns: int | None
    result_text: str
    raw_path: Path
    api_error_status: int | None = None
    terminal_reason: str | None = None

    @property
    def telemetry(self) -> dict[str, float | int | None]:
        return {
            "cost_usd": self.cost_usd,
            "duration_s": self.duration_s,
            "turns": self.turns,
        }


def _cli() -> str:
    exe = shutil.which("claude")
    if exe is None:
        raise AgentError(
            "`claude` is not on PATH. The factory drives the Claude Code CLI in "
            "headless mode; install it or adjust PATH before running."
        )
    return exe


def _parse(stdout: str) -> dict:
    """Read the `--output-format json` envelope, tolerating leading noise.

    Falls back to scanning for the last JSON object in the stream, because a
    warning printed before the payload should not lose us the telemetry.
    """
    stdout = stdout.strip()
    if not stdout:
        return {}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        pass
    start = stdout.find("{")
    while start != -1:
        try:
            return json.loads(stdout[start:])
        except json.JSONDecodeError:
            start = stdout.find("{", start + 1)
    return {}


def _is_rate_limit(status: int | None, terminal_reason: str | None) -> bool:
    """An API-level fault (429/5xx) the CLI reported, not a bad model result.

    `terminal_reason == "api_error"` is checked unconditionally because it was
    present on every one of B07's failed legs and is the more robust signal —
    a future CLI version could plausibly drop `api_error_status` from the
    envelope but keep this field.
    """
    return terminal_reason == "api_error" or (status is not None and status >= 429)


# Free-text from the CLI, e.g. "You've hit your session limit · resets 7pm
# (America/New_York)" — not a contract, so the parser below is deliberately
# best-effort and every caller must treat `None` as "could not parse" rather
# than as "no reset was given". Derived from the literal fixture at
# `.factory/runs/B07/attempt-1/implement.stdout.json`; if a future CLI wording
# change breaks this, that file is the known-good sample to diff against.
_RESET_RE = re.compile(
    r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*\(([^)]+)\)", re.IGNORECASE
)


def _parse_reset_hint(text: str, *, now: datetime) -> datetime | None:
    """The next occurrence of the CLI's stated reset time, as aware UTC.

    Returns `None` on anything that doesn't match the expected shape —
    callers fall back to backoff rather than trust this blindly.
    """
    match = _RESET_RE.search(text)
    if match is None:
        return None
    hour_s, minute_s, ampm, tzname = match.groups()
    hour = int(hour_s)
    minute = int(minute_s) if minute_s else 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    if ampm:
        ampm = ampm.lower()
        if not (1 <= hour <= 12):
            return None
        hour = hour % 12
        if ampm == "pm":
            hour += 12
    try:
        zone = ZoneInfo(tzname)
    except ZoneInfoNotFoundError:
        return None

    local_now = now.astimezone(zone)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += _ONE_DAY
    return candidate.astimezone(UTC)


def run(
    *,
    worktree: Path,
    prompt: str,
    model: str,
    allowed_tools: str,
    run_dir: Path,
    label: str,
    max_turns: int = DEFAULT_MAX_TURNS,
    resume: str | None = None,
    skip_permissions: bool = False,
    billing: str = "api",
) -> AgentRun:
    """Invoke the CLI once. Never raises on a bad *result*, only on a bad *run*.

    The prompt goes in on stdin rather than argv: packets carry verbatim SQL and
    whole test files, and Windows' 32k command-line limit would truncate them
    silently.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        _cli(),
        "-p",
        "--model",
        model,
        "--output-format",
        "json",
        "--max-turns",
        str(max_turns),
    ]
    if resume:
        cmd += ["--resume", resume]
    if skip_permissions:
        # Appropriate only on a dedicated factory box: the worktree is
        # disposable and nothing outside it is reachable. Off by default.
        cmd += ["--dangerously-skip-permissions"]
    else:
        cmd += ["--permission-mode", "acceptEdits", "--allowedTools", allowed_tools]

    (run_dir / f"{label}.prompt.md").write_text(prompt, encoding="utf-8")

    try:
        proc = subprocess.run(
            cmd,
            cwd=worktree,
            input=prompt,
            env=child_env(billing),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raw = run_dir / f"{label}.stdout.json"
        raw.write_text(
            json.dumps({"error": "timeout", "timeout_s": TIMEOUT_S}), "utf-8"
        )
        return AgentRun(False, None, None, float(TIMEOUT_S), None, "timed out", raw)
    except OSError as exc:
        raise AgentError(f"could not start the CLI: {exc}") from exc

    raw = run_dir / f"{label}.stdout.json"
    raw.write_text(proc.stdout or "", encoding="utf-8")
    if proc.stderr:
        (run_dir / f"{label}.stderr.txt").write_text(proc.stderr, encoding="utf-8")

    payload = _parse(proc.stdout or "")
    duration_ms = payload.get("duration_ms")
    return AgentRun(
        ok=proc.returncode == 0 and not payload.get("is_error", False),
        session_id=payload.get("session_id"),
        cost_usd=payload.get("total_cost_usd"),
        duration_s=(duration_ms / 1000.0)
        if isinstance(duration_ms, int | float)
        else None,
        turns=payload.get("num_turns"),
        result_text=str(payload.get("result", ""))[:4000],
        raw_path=raw,
        api_error_status=payload.get("api_error_status"),
        terminal_reason=payload.get("terminal_reason"),
    )


def run_resilient(
    *,
    worktree: Path,
    prompt: str,
    model: str,
    allowed_tools: str,
    run_dir: Path,
    label: str,
    max_turns: int = DEFAULT_MAX_TURNS,
    resume: str | None = None,
    skip_permissions: bool = False,
    billing: str = "api",
    max_wait_s: float = DEFAULT_MAX_WAIT_S,
    max_parsed_wait_s: float = DEFAULT_MAX_PARSED_WAIT_S,
    on_wait: Callable[[str, datetime | None, float], None] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> AgentRun:
    """`run()`, but a rate-limit fault is waited out instead of surfacing as a
    bad result.

    A 429/API-level fault is not a bad model response — it means the CLI
    never got to try. Each retried leg is a **fresh call**, never `--resume`:
    the session-continuation feature exists for `run_with_contract`'s
    same-session contract-correction retry, and reusing a session id across a
    wait that can run for hours is untested territory this doesn't need,
    since the prompt is identical either way.

    Raises `RateLimited` (an `AgentError`) if still rate-limited after the
    applicable cap — `max_parsed_wait_s` when the CLI gave us an exact reset
    time, the tighter `max_wait_s` when it didn't and we're only backing off
    on a guess. The caller's existing environment-fault handling is the
    correct backstop for that case.
    """
    start = now_fn()
    backoff = _BACKOFF_START_S
    while True:
        result = run(
            worktree=worktree,
            prompt=prompt,
            model=model,
            allowed_tools=allowed_tools,
            run_dir=run_dir,
            label=label,
            max_turns=max_turns,
            resume=resume,
            skip_permissions=skip_permissions,
            billing=billing,
        )
        if result.ok or not _is_rate_limit(
            result.api_error_status, result.terminal_reason
        ):
            return result

        elapsed = (now_fn() - start).total_seconds()
        resume_at = _parse_reset_hint(result.result_text, now=now_fn())
        if resume_at is not None:
            wait_s = max((resume_at - now_fn()).total_seconds(), 0.0) + 60.0
            message = f"rate limited; CLI reports reset at {resume_at.isoformat()}"
            cap = max_parsed_wait_s
        else:
            wait_s = min(backoff, _BACKOFF_MAX_S)
            backoff *= _BACKOFF_FACTOR
            message = "rate limited; no parseable reset hint, backing off"
            cap = max_wait_s

        if elapsed + wait_s > cap:
            raise RateLimited(
                f"{message}; wall-clock cap of {cap:.0f}s exceeded",
                resume_at=resume_at,
                waited_s=elapsed,
            )
        if on_wait:
            on_wait(message, resume_at, elapsed)
        sleep_fn(wait_s)


def run_with_contract(
    *,
    worktree: Path,
    prompt: str,
    model: str,
    allowed_tools: str,
    run_dir: Path,
    label: str,
    artifact: Path,
    validate,
    max_turns: int = DEFAULT_MAX_TURNS,
    skip_permissions: bool = False,
    billing: str = "api",
    max_wait_s: float = DEFAULT_MAX_WAIT_S,
    max_parsed_wait_s: float = DEFAULT_MAX_PARSED_WAIT_S,
    on_wait: Callable[[str, datetime | None, float], None] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
):
    """Run, then validate the file the model was told to write.

    On a contract violation the complaint is handed back in the same session for
    exactly one correction. A model given a precise schema error usually fixes
    it; one that fails twice is not converging, and burning further turns on it
    costs more than failing the attempt.

    The first leg goes through `run_resilient`, so a rate-limit fault is
    waited out (or raised as `RateLimited`) before this function ever sees a
    result — which also means the contract-correction retry below is only
    ever reached for a genuine bad/missing response from a run that actually
    completed, never as a second attempt into the same rate limit.

    Returns `(validated, AgentRun, error_or_None)`.
    """
    artifact.unlink(missing_ok=True)
    first = run_resilient(
        worktree=worktree,
        prompt=prompt,
        model=model,
        allowed_tools=allowed_tools,
        run_dir=run_dir,
        label=label,
        max_turns=max_turns,
        skip_permissions=skip_permissions,
        billing=billing,
        max_wait_s=max_wait_s,
        max_parsed_wait_s=max_parsed_wait_s,
        on_wait=on_wait,
        sleep_fn=sleep_fn,
        now_fn=now_fn,
    )

    from contracts import ContractError, read_json

    try:
        return validate(read_json(artifact)), first, None
    except ContractError as exc:
        complaint = str(exc)

    retry_prompt = (
        f"Your response file was rejected by the harness:\n\n    {complaint}\n\n"
        f"Fix it. Rewrite `{artifact.name}` at exactly the same path so it "
        "satisfies the contract. Change nothing else — do not touch any source "
        "file, and do not redo the work. Only the response file is wrong."
    )
    second = run(
        worktree=worktree,
        prompt=retry_prompt,
        model=model,
        allowed_tools=allowed_tools,
        run_dir=run_dir,
        label=f"{label}-retry",
        max_turns=8,
        resume=first.session_id,
        skip_permissions=skip_permissions,
        billing=billing,
    )
    merged = AgentRun(
        ok=second.ok,
        session_id=second.session_id or first.session_id,
        cost_usd=(first.cost_usd or 0) + (second.cost_usd or 0),
        duration_s=(first.duration_s or 0) + (second.duration_s or 0),
        turns=(first.turns or 0) + (second.turns or 0),
        result_text=second.result_text,
        raw_path=second.raw_path,
    )
    try:
        return validate(read_json(artifact)), merged, None
    except ContractError as exc:
        return None, merged, f"{complaint} — and after one correction: {exc}"
