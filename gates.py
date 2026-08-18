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
import tomllib
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


def declared_markers(api_dir: Path) -> set[str] | None:
    """Marker names the project registers, or `None` where that cannot be read.

    Only `[tool.pytest.ini_options] markers` in `pyproject.toml` is consulted.
    A project that registers markers in `pytest.ini` or `setup.cfg` reads as
    `None` here, which is the same answer as "no configuration at all" on
    purpose: both mean *this cannot be checked*, and the caller treats an
    unanswerable question differently from a negative answer.
    """
    config = api_dir / "pyproject.toml"
    if not config.is_file():
        return None
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    ini = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
    markers = ini.get("markers")
    if markers is None:
        return None
    # Registered as `"name: description"`; the name is what `-m` selects on.
    return {str(entry).split(":", 1)[0].strip() for entry in markers}


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

    @property
    def autoformatted(self) -> list[str]:
        """Files a check rewrote in place, for the caller to commit.

        The gate runs after the attempt is committed, so a rewrite leaves the
        worktree dirty. Whoever called `run_checks` has to fold that into the
        attempt's HEAD — an uncommitted change would be read as meddling by the
        review path and would not reach the merge at all.
        """
        out: list[str] = []
        for c in self.checks:
            out.extend(c.detail.get("autoformatted", ()))
        return out

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


def _ruff_format(
    api_dir: Path, uv: str, changed: list[str], tampered: list[str]
) -> Check:
    """Scoped to the diff — see the module docstring for why.

    **A formatting difference is repaired, not punished.** `ruff format` is
    deterministic and total: there is exactly one formatting of a given AST, the
    tool computes it, and no judgement is involved. So a red gate here told an
    agent to reproduce by hand an output the harness could have written itself,
    and that is what it did — a packet can spend most of its attempts on
    whitespace while `ruff check` and the whole test suite pass on every one.

    On a `--check` failure this runs the formatter in write mode over the same
    scoped targets and re-checks. Passing after that is a `pass`, with the
    rewritten files named in `detail["autoformatted"]` so it is never silent:
    the caller commits them and advances `head_sha`, because the work was
    already committed before the gate ran and an uncommitted rewrite would read
    as reviewer meddling.

    Two things it will not do. It never writes to a path the packet forbade —
    `tampered` is excluded from the write set, so a forbidden file that is also
    misformatted stays misformatted and `untouched` keeps failing the gate on
    its own terms. And a file that is *still* unformatted after a write-mode run
    is a real fault, reported as `fail`: ruff declining to reach a fixed point
    means a syntax error it could not parse, which is the agent's to fix.
    """
    targets = [
        c[len("api/") :] for c in changed if c.startswith("api/") and c.endswith(".py")
    ]
    if not targets:
        return Check(
            name="ruff-format", status="skipped", detail={"reason": "no python in diff"}
        )
    proc = _run([uv, "run", "ruff", "format", "--check", *targets], api_dir)
    if proc.returncode == 0:
        return Check(
            name="ruff-format",
            status="pass",
            exit_code=0,
            detail={"scope": targets},
            output_tail=_tail(proc),
        )

    # Forbidden paths are excluded from the write set. `tampered` is repo-relative
    # like `changed`, so it is trimmed the same way before comparing.
    off_limits = {
        t[len("api/") :] for t in tampered if t.startswith("api/") and t.endswith(".py")
    }
    writable = [t for t in targets if t not in off_limits]
    if not writable:
        return Check(
            name="ruff-format",
            status="fail",
            exit_code=proc.returncode,
            detail={"scope": targets, "not_written": sorted(off_limits)},
            output_tail=(
                "Every misformatted file in the diff is one the packet forbids "
                "editing, so none was rewritten; see the `untouched` check.\n\n"
                + _tail(proc)
            ),
        )

    write = _run([uv, "run", "ruff", "format", *writable], api_dir)
    recheck = _run([uv, "run", "ruff", "format", "--check", *targets], api_dir)
    if recheck.returncode != 0:
        return Check(
            name="ruff-format",
            status="fail",
            exit_code=recheck.returncode,
            detail={"scope": targets, "write_attempted": writable},
            output_tail=(
                "`ruff format` was run in write mode and the files are still not "
                "formatted, which means ruff could not parse one of them.\n\n"
                + _tail(recheck)
            ),
        )
    return Check(
        name="ruff-format",
        status="pass",
        exit_code=0,
        detail={"scope": targets, "autoformatted": writable},
        output_tail=(
            "Was unformatted; the harness ran `ruff format` in write mode over "
            f"{len(writable)} file(s) and re-checked clean. Attempt not spent.\n\n"
            + _tail(write)
        ),
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

    # A *marked* run that collects nothing is two different states wearing one
    # exit code, and only one of them is the task's fault.
    #
    # `PYTEST_NO_TESTS` on the unmarked run means the suite vanished, which is
    # what `_pytest_verdict` calls a failure and rightly. On `-m <marker>` it
    # can instead mean the project registers no such marker — in which case
    # nothing was ever going to be selected, by this task or any other, and
    # failing the gate blames the work for a question the project cannot ask.
    # It fails identically on every attempt, which is the shape the harness
    # exists to *detect* rather than to produce.
    #
    # So: marker registered and empty is a failure (the task owed tests it did
    # not write). Marker not registered is `skipped`, loudly — the check is
    # reported, the gate stays green, and the detail names what would make it
    # mean something.
    if marker and proc.returncode == PYTEST_NO_TESTS:
        declared = declared_markers(api_dir)
        if declared is not None and marker not in declared:
            return Check(
                name=name,
                status="skipped",
                exit_code=proc.returncode,
                detail={
                    **counts,
                    "marker": marker,
                    "declared_markers": sorted(declared),
                },
                output_tail=(
                    f"`-m {marker}` selected no tests, and this project registers "
                    f"no `{marker}` marker: pyproject.toml's "
                    f"[tool.pytest.ini_options] markers are "
                    f"{sorted(declared) or 'empty'}. Nothing was verified by this "
                    f"check. Either register the marker and mark the tests, or "
                    f"stop setting the flag that asks for it."
                ),
            )

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
    extra_marker: str,
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
        # Format before check, and both before pytest. `_ruff_format` may rewrite
        # files, so anything that reads them has to run after it or it judges
        # content the merge will not contain.
        checks.append(_ruff_format(api_dir, uv, changed, tampered))
        checks.append(_ruff_check(api_dir, uv))
        checks.append(_pytest(api_dir, uv, marker=None, baseline=baseline))
        if extra_marker:
            checks.append(_pytest(api_dir, uv, marker=extra_marker, baseline=None))
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
