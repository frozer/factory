"""Verification the harness runs itself.

The agent's own report of "tests pass" is recorded but never believed. Every
gate here is executed by the factory, in the task's worktree, and its parsed
result is what decides whether the work reaches review.

Two gates are shaped by the fact that the tree is not clean to begin with, and
neither records what it measured — a count written down here is a count that
rots, and the whole point of both mechanisms is to measure at run time:

* `ruff format --check` is scoped to the files the diff touched. Some files are
  unformatted for reasons no agent caused, and a whole-tree format gate would
  fail every task for them.
* "No previously-passing test now fails" is checked against a baseline
  `measure_baseline` takes on the task's own base commit, never against a
  figure stored in the source.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Generous, because a cold `uv sync` or a Next build is slow; low enough that a
# hung process cannot wedge an unattended run.
TIMEOUT_S = 900

# pytest's own exit codes, and which of them are the *work's* fault.
#
# The distinction matters more than it looks: an `error` is treated as an
# environment fault, and two consecutive ones stop the whole factory. Exit 2 is
# the trap — a test that cannot import the module the task was supposed to
# create is "interrupted", not "failed", and that is the single most common
# state of a task an agent got wrong. Blaming the box for it would halt the
# queue after two bad tasks.
PYTEST_OK = 0
PYTEST_FAILED = 1
PYTEST_INTERRUPTED = 2
PYTEST_INTERNAL = 3
PYTEST_USAGE = 4
PYTEST_NO_TESTS = 5

_COLLECTION_ERROR = re.compile(
    r"error[s]? during collection|ERROR\s+\S+::|ImportError|ModuleNotFoundError"
)


def _pytest_verdict(returncode: int, output: str) -> str:
    """`pass` | `fail` | `error` — see the exit-code note above."""
    if returncode == PYTEST_OK:
        return "pass"
    if returncode == PYTEST_FAILED:
        return "fail"
    if returncode == PYTEST_NO_TESTS:
        # The suite vanished. Whatever caused that, it is not the machine.
        return "fail"
    if returncode == PYTEST_INTERRUPTED:
        # A collection error is the work's fault; a genuine interrupt is not.
        return "fail" if _COLLECTION_ERROR.search(output) else "error"
    return "error"  # 3 internal, 4 usage, anything unexpected


_SUMMARY = re.compile(
    r"(\d+)\s+(passed|failed|errors?|skipped|deselected|xfailed|xpassed)"
)


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: str  # pass | fail | error | skipped
    exit_code: int | None = None
    detail: dict = field(default_factory=dict)
    output_tail: str = ""


@dataclass(frozen=True, slots=True)
class GateResult:
    status: str  # green | red | error
    checks: list[Check]
    baseline_passed: int | None = None

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status in ("fail", "error")]

    def summary(self) -> str:
        return " · ".join(f"{c.name}:{c.status}" for c in self.checks)

    def to_json(self) -> str:
        return json.dumps(
            {
                "status": self.status,
                "baseline_passed": self.baseline_passed,
                "checks": [asdict(c) for c in self.checks],
            },
            indent=2,
        )


def _tool(name: str) -> str | None:
    """Resolve an executable, tolerating Windows' `.cmd` shims for npm."""
    return shutil.which(name)


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_S,
    )


def _tail(proc: subprocess.CompletedProcess[str], lines: int = 40) -> str:
    blob = (proc.stdout or "") + (proc.stderr or "")
    return "\n".join(blob.strip().splitlines()[-lines:])


def parse_pytest(output: str) -> dict[str, int]:
    """Counts from pytest's terminal summary.

    Scans the whole output and keeps the last occurrence of each label, so the
    real summary line wins over any count that appeared in captured output.
    """
    counts: dict[str, int] = {}
    for value, label in _SUMMARY.findall(output):
        counts[label.rstrip("s") if label.startswith("error") else label] = int(value)
    return counts


def measure_baseline(api_dir: Path) -> dict[str, int]:
    """Run the offline suite on an untouched checkout.

    Returns the counts. A baseline that cannot be measured returns an empty dict
    and the regression check is then skipped rather than guessed at — refusing to
    compare is honest; comparing against zero would pass everything.
    """
    uv = _tool("uv")
    if uv is None or not api_dir.is_dir():
        return {}
    try:
        proc = _run(
            [uv, "run", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider"], api_dir
        )
    except (subprocess.TimeoutExpired, OSError):
        return {}
    blob = proc.stdout + proc.stderr
    if _pytest_verdict(proc.returncode, blob) == "error":
        return {}
    return parse_pytest(blob)


def check_untouched(tampered: list[str]) -> Check:
    """Files the packet declared off-limits, or tests the harness planted.

    This is the one gate that catches a model being helpful in the wrong place:
    editing the supplied test until it passes, or "improving" a seam file that
    another task owns.
    """
    if tampered:
        return Check(
            name="untouched",
            status="fail",
            detail={"modified": tampered},
            output_tail=(
                "The packet forbids modifying these paths, and they were "
                "modified:\n  " + "\n  ".join(tampered)
            ),
        )
    return Check(name="untouched", status="pass")


def _ruff_check(api_dir: Path, uv: str) -> Check:
    proc = _run([uv, "run", "ruff", "check", "."], api_dir)
    return Check(
        name="ruff-check",
        status="pass" if proc.returncode == 0 else "fail",
        exit_code=proc.returncode,
        output_tail=_tail(proc),
    )


def _ruff_format(api_dir: Path, uv: str, changed: list[str]) -> Check:
    """Scoped to the diff — see the module docstring for why."""
    targets = [
        c[len("api/") :] for c in changed if c.startswith("api/") and c.endswith(".py")
    ]
    if not targets:
        return Check(
            name="ruff-format", status="skipped", detail={"reason": "no python in diff"}
        )
    proc = _run([uv, "run", "ruff", "format", "--check", *targets], api_dir)
    return Check(
        name="ruff-format",
        status="pass" if proc.returncode == 0 else "fail",
        exit_code=proc.returncode,
        detail={"scope": targets},
        output_tail=_tail(proc),
    )


def _pytest(
    api_dir: Path, uv: str, *, marker: str | None, baseline: int | None
) -> Check:
    cmd = [uv, "run", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider"]
    if marker:
        cmd += ["-m", marker]
    proc = _run(cmd, api_dir)
    blob = proc.stdout + proc.stderr
    counts = parse_pytest(blob)
    name = f"pytest[{marker}]" if marker else "pytest"

    verdict = _pytest_verdict(proc.returncode, blob)
    if verdict != "pass":
        return Check(
            name=name,
            status=verdict,
            exit_code=proc.returncode,
            detail=counts,
            output_tail=_tail(proc),
        )

    failed = counts.get("failed", 0) + counts.get("error", 0)
    passed = counts.get("passed", 0)
    detail: dict = {**counts, "baseline_passed": baseline}

    if failed:
        return Check(
            name=name,
            status="fail",
            exit_code=proc.returncode,
            detail=detail,
            output_tail=_tail(proc),
        )

    # The regression rule. A task may add tests; it may never lose one. Only
    # applied to the default (unmarked) run, since the baseline is measured
    # there.
    if marker is None and baseline is not None and passed < baseline:
        return Check(
            name=name,
            status="fail",
            exit_code=proc.returncode,
            detail={**detail, "regression": baseline - passed},
            output_tail=(
                f"{passed} tests passed but the base commit had {baseline}. "
                f"{baseline - passed} previously-passing test(s) are gone or "
                "no longer run."
            ),
        )
    return Check(
        name=name,
        status="pass",
        exit_code=proc.returncode,
        detail=detail,
        output_tail=_tail(proc, 10),
    )


def _npm(webapp: Path, npm: str, script: str) -> Check:
    proc = _run([npm, "run", script], webapp)
    return Check(
        name=f"npm-{script}",
        status="pass" if proc.returncode == 0 else "fail",
        exit_code=proc.returncode,
        output_tail=_tail(proc),
    )


def run_checks(
    worktree: Path,
    *,
    surface: str,
    changed: list[str],
    tampered: list[str],
    needs_db: bool,
    baseline: int | None,
) -> GateResult:
    """Every gate for this task, in cheapest-first order.

    `error` is kept distinct from `fail` throughout: a missing toolchain or a
    pytest that could not start is an environment problem, and blaming the agent
    for it would burn an attempt and eventually block a task that was never
    wrong.
    """
    checks: list[Check] = [check_untouched(tampered)]

    if surface == "api":
        uv = _tool("uv")
        api_dir = worktree / "api"
        if uv is None:
            checks.append(
                Check(
                    name="toolchain", status="error", output_tail="`uv` is not on PATH"
                )
            )
            return GateResult(status="error", checks=checks, baseline_passed=baseline)
        checks.append(_ruff_check(api_dir, uv))
        checks.append(_ruff_format(api_dir, uv, changed))
        checks.append(_pytest(api_dir, uv, marker=None, baseline=baseline))
        if needs_db:
            checks.append(_pytest(api_dir, uv, marker="db", baseline=None))
    else:
        npm = _tool("npm")
        webapp = worktree / "webapp"
        if npm is None:
            checks.append(
                Check(
                    name="toolchain", status="error", output_tail="`npm` is not on PATH"
                )
            )
            return GateResult(status="error", checks=checks, baseline_passed=baseline)
        # The only two gates the frontend has. There is no test runner, by the
        # spec's own decision — TypeScript exhaustiveness is the enforcement.
        checks.append(_npm(webapp, npm, "lint"))
        checks.append(_npm(webapp, npm, "build"))

    if any(c.status == "error" for c in checks):
        status = "error"
    elif any(c.status == "fail" for c in checks):
        status = "red"
    else:
        status = "green"
    return GateResult(status=status, checks=checks, baseline_passed=baseline)
