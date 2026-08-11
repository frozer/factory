"""What the harness had to measure about the repository it landed in.

The planning tier reasons inside a worktree created by `git worktree add`, which
carries **tracked files only**. So the tree a cutter or an assessor sees is not
the tree the operator sees, and the gap between them is invisible from either
side: an agent told to "open the data and count the rows" finds nothing, and
nothing distinguishes "this project has no such file" from "this file is simply
not in your checkout". That is how an `unverifiable` claim becomes a `held` one.

Nothing here knows anything about any particular project. The one probe is
`git status --ignored`, which is true of every git repository, and the residue
that cannot be probed — *which* of those paths the planning tier actually needs
to read — is answered once by an operator and recorded in `tasks/GROUNDING.md`,
in git, beside the packets and the assessments and for the same reason.

Two axes, because either alone misses a real case:

* **Divergence** — present for the operator, absent for the planner. Measured.
* **Reachability** — for each declared input, whether it is anywhere at all.
  A project whose inputs have never been fetched has no divergence to find and
  is nonetheless ungroundable, which is the case this module was written from.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import gates
import gitops

# Generous enough for a cold ruff run, short enough that `ground` stays a
# command an operator will actually rerun.
_PROBE_TIMEOUT_S = 300

FRONTMATTER = re.compile(r"\A\+\+\+\s*\n(.*?)\n\+\+\+\s*\n?", re.DOTALL)

# Ignored paths that are build residue rather than input. Classification is
# presentation only — everything found is reported either way — so a wrong guess
# here costs a line of noise in a report, never a missed input. The operator's
# `planner_must_read` is what actually decides anything.
NOISE = (
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".ruff_cache",
    ".pytest_cache",
    ".mypy_cache",
    ".factory",
    ".next",
    "dist",
    "build",
    "htmlcov",
    ".coverage",
)

# Reachability verdicts. The two that block are the two where the planner cannot
# open the bytes the ground-truth ladder tells it to open.
IN_WORKTREE = "in_worktree"
EXTERNAL = "external"
REPO_ONLY = "repo_only"
ABSENT = "absent"
BLOCKING = (REPO_ONLY, ABSENT)


class GroundError(RuntimeError):
    """Grounding is malformed. Never raised for *ungrounded* — that is a state
    with an obvious remedy (`run.py ground`), not an error."""


@dataclass(frozen=True, slots=True)
class Divergence:
    path: str
    noise: bool

    @property
    def label(self) -> str:
        return "build residue" if self.noise else "unclassified"


@dataclass(frozen=True, slots=True)
class InputCheck:
    path: str
    status: str

    @property
    def blocks(self) -> bool:
        return self.status in BLOCKING

    @property
    def why(self) -> str:
        return {
            IN_WORKTREE: "tracked, so every worktree has it",
            EXTERNAL: "absolute path outside the repo, reachable from any cwd",
            REPO_ONLY: (
                "present in your checkout but untracked, so no planning "
                "worktree will contain it"
            ),
            ABSENT: "not on this machine at all",
        }[self.status]


@dataclass(frozen=True, slots=True)
class Probe:
    """One thing that was run, and what it said.

    `command` is carried so a reader can re-run it. A probe result nobody can
    reproduce is an assertion with a timestamp on it, which is the shape of
    claim this whole design exists to refuse.
    """

    name: str
    command: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class Grounding:
    grounded_at: str
    grounded_commit: str
    planner_must_read: list[str]
    spec_path: str = ""
    # Counts from the last `ground`. Snapshots, explicitly stamped with the
    # commit they were taken at — which is what makes them different from the
    # same numbers written into prose, where they read as standing facts and
    # rot in place. `ground --check` is what keeps them honest.
    measured: dict = field(default_factory=dict)
    surfaces: list[str] = field(default_factory=list)
    # Which declared inputs could not be reached when this was measured. Recorded
    # so `--check` can report what *changed* rather than what is merely true —
    # a check that fires forever on a known condition is one nobody reads.
    blocking: list[str] = field(default_factory=list)
    # Documents that state how this project does things. Discovered, but kept if
    # an operator narrows the list — the probe proposes and the operator decides,
    # the same shape as every other answer in this file.
    conventions: list[str] = field(default_factory=list)


def _is_rooted(path: str) -> bool:
    """Absolute on this platform, or POSIX-rooted on any.

    `Path("/mnt/data").is_absolute()` is **False** on Windows — an absolute path
    there needs a drive — and `repo / "/mnt/data"` then silently yields
    `C:/mnt/data`, so a config written on Linux would be judged against a path
    nobody named. Treat a leading separator as rooted everywhere and let the
    existence check decide.
    """
    return Path(path).is_absolute() or path.startswith(("/", "\\"))


def _is_noise(path: str) -> bool:
    parts = [p for p in path.strip("/").split("/") if p]
    return any(part in NOISE for part in parts)


def diverging_paths(repo: Path) -> list[Divergence]:
    """Ignored paths that exist for the operator and will not exist for a planner.

    `--ignored` without `--untracked-files=all` reports the *directory* rather
    than each file under it, which is the difference between fifteen lines and
    several thousand.
    """
    out = gitops.git("status", "--porcelain", "--ignored", cwd=repo, check=False)
    found = []
    for line in out.splitlines():
        if not line.startswith("!!"):
            continue
        path = line[2:].strip().strip('"').rstrip("/")
        if path:
            found.append(Divergence(path=path, noise=_is_noise(path)))
    return sorted(found, key=lambda d: d.path)


def check_inputs(repo: Path, declared: list[str]) -> list[InputCheck]:
    """Where each declared input actually is.

    Whether a planning worktree will contain a path is decided by whether it is
    *tracked*, which is knowable without building one — and worth knowing before
    building one, since a worktree costs a checkout and a dependency install
    before anything has been refused.
    """
    checks = []
    for raw in declared:
        path = raw.strip()
        if not path:
            continue
        if _is_rooted(path):
            # An absolute path resolves identically from any cwd, so a worktree
            # does not break it — but only if it is actually there. Without this
            # existence check `external` was a verdict that never blocked and
            # never looked, which is the one shape a fail-fast gate must not have.
            checks.append(InputCheck(path, EXTERNAL if Path(path).exists() else ABSENT))
            continue
        if gitops.git("ls-files", "--", path, cwd=repo, check=False).strip():
            checks.append(InputCheck(path, IN_WORKTREE))
        elif (repo / path).exists():
            checks.append(InputCheck(path, REPO_ONLY))
        else:
            checks.append(InputCheck(path, ABSENT))
    return checks


def probe_surfaces(repo: Path) -> tuple[list[str], list[Probe]]:
    """Which surfaces this repository actually has, and whether their runners
    are installable.

    Deliberately does **not** run `uv sync` or `npm ci`. Those are minutes on a
    cold landing, they are what `_planning_worktree` does anyway, and their
    failure already surfaces there with a clear message. What grounding is for
    is the cheap question a slow command would only answer by accident: is the
    directory there, is it the surface it claims to be, is the runner on PATH.
    """
    present, probes = [], []
    for name, spec in gitops.SURFACES.items():
        root = repo / spec["dir"]
        manifest = root / spec["manifest"]
        tool = shutil.which(spec["tool"])
        if not manifest.is_file():
            probes.append(
                Probe(
                    name=f"surface:{name}",
                    command=f"test -f {spec['dir']}/{spec['manifest']}",
                    ok=True,
                    detail="not present in this repository",
                )
            )
            continue
        present.append(name)
        probes.append(
            Probe(
                name=f"surface:{name}",
                command=f"which {spec['tool']}",
                ok=tool is not None,
                detail=(
                    f"`{spec['dir']}/` present; `{spec['tool']}` on PATH"
                    if tool
                    else f"`{spec['dir']}/` present but `{spec['tool']}` is NOT on "
                    "PATH — every gate on this surface will report an environment "
                    "fault"
                ),
            )
        )
    return present, probes


_FORMAT_COUNT = re.compile(r"(\d+)\s+files?\s+would be reformatted")


def probe_toolchain(repo: Path, surfaces: list[str]) -> tuple[dict, list[Probe]]:
    """Measure the two repo facts the gates are shaped around.

    Neither number is injected into a prompt and neither is what any gate reads:
    `gates.measure_baseline` re-measures on the task's own fork point, and the
    format gate is scoped to the diff. These are recorded for an operator and
    for `--check` to notice drift in, which is the only use of a count that does
    not rot.
    """
    measured: dict = {}
    probes: list[Probe] = []
    if "api" not in surfaces:
        return measured, probes
    api = repo / "api"
    uv = shutil.which("uv")
    if uv is None:
        return measured, probes

    cmd = "uv run --directory api ruff format --check ."
    try:
        proc = subprocess.run(
            [uv, "run", "ruff", "format", "--check", "."],
            cwd=api,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PROBE_TIMEOUT_S,
        )
        match = _FORMAT_COUNT.search(proc.stdout + proc.stderr)
        red = int(match.group(1)) if match else 0
        measured["format_red_files"] = red
        probes.append(
            Probe(
                name="toolchain:format",
                command=cmd,
                ok=True,
                detail=(
                    f"{red} file(s) already fail a whole-tree format check, which "
                    "is why the gate is scoped to the diff"
                    if red
                    else "the tree is clean, so a whole-tree format gate would "
                    "also pass today"
                ),
            )
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        probes.append(Probe("toolchain:format", cmd, False, f"could not run: {exc}"))

    counts = gates.measure_baseline(api)
    if counts:
        measured["offline_passed"] = counts.get("passed", 0)
        measured["offline_deselected"] = counts.get("deselected", 0)
        probes.append(
            Probe(
                name="toolchain:baseline",
                command="uv run --directory api pytest -q",
                ok=True,
                detail=f"{measured['offline_passed']} passed, "
                f"{measured['offline_deselected']} deselected",
            )
        )
    else:
        probes.append(
            Probe(
                name="toolchain:baseline",
                command="uv run --directory api pytest -q",
                ok=False,
                detail="could not measure — the regression check will be skipped "
                "rather than guessed at",
            )
        )
    return measured, probes


def probe_spec(repo: Path, spec_path: str) -> list[Probe]:
    """The spec of record exists and has history to pin a packet to.

    A packet pins to `git log -1 -- <spec>`, so a spec with no commits gives
    every packet an empty `spec_commit` and the drift check nothing to compare
    against — it would then be silent for exactly the reason it exists.
    """
    if not spec_path:
        return [
            Probe(
                name="spec",
                command="(none declared)",
                ok=True,
                detail="no spec of record declared; `cut --spec` must name one "
                "every time",
            )
        ]
    cmd = f"git log -1 --format=%h -- {spec_path}"
    if not (repo / spec_path).is_file():
        return [Probe("spec", cmd, False, f"`{spec_path}` is not a file")]
    sha = gitops.git(
        "log", "-1", "--format=%h", "--", spec_path, cwd=repo, check=False
    ).strip()
    if not sha:
        return [Probe("spec", cmd, False, f"`{spec_path}` has no commit history")]
    return [Probe("spec", cmd, True, f"`{spec_path}` at `{sha}`")]


# Files that conventionally hold conventions. Names first, because a project
# that has one of these has said where its rules live; READMEs after, because a
# project that has none has usually written them there anyway — which is this
# repository's case, and the common one.
CONVENTION_NAMES = (
    "CLAUDE.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "CONVENTIONS.md",
    "STYLE.md",
    ".cursorrules",
)


def convention_candidates(repo: Path, limit: int = 6) -> list[str]:
    """Tracked documents that plausibly state how this project does things.

    Conventions are prose about *intent*, which `cut.md` ranks below files on
    disk — so these are never handed to a planner as fact. They are where the
    `conventions` command goes looking for claims to check, and a stale one is
    worth more as a finding than as a rule.
    """
    tracked = [
        line.strip()
        for line in gitops.git("ls-files", cwd=repo, check=False).splitlines()
        if line.strip()
    ]
    named, readmes, adrs = [], [], []
    for rel in tracked:
        if rel.startswith(("tasks/", "factory/")):
            continue
        name = rel.rsplit("/", 1)[-1]
        if name in CONVENTION_NAMES:
            named.append(rel)
        elif "/adr/" in f"/{rel}" or rel.startswith("adr/"):
            adrs.append(rel)
        elif name == "README.md":
            readmes.append(rel)
    # `CONVENTION_NAMES` is ordered by how strongly the name commits to being
    # the rules — `CLAUDE.md` says it outright, `STYLE.md` covers a corner — so
    # rank by it rather than by whatever order git listed the tree in. READMEs
    # go last and shallowest-first: the root one is about the project, a deep
    # one is about a directory.
    named.sort(key=lambda r: CONVENTION_NAMES.index(r.rsplit("/", 1)[-1]))
    ordered = named + sorted(adrs) + sorted(readmes, key=lambda r: r.count("/"))
    return ordered[:limit]


@dataclass(frozen=True, slots=True)
class SpecCandidate:
    path: str
    last_commit: str
    lines: int


def spec_candidates(repo: Path, limit: int = 8) -> list[SpecCandidate]:
    """Tracked markdown that could plausibly be a spec of record.

    Offered so the question is a choice between things that exist rather than a
    memory test, and ranked by recency because the document being worked from is
    usually the one being amended. `tasks/` and `factory/` are excluded: those
    are the harness talking to itself.

    This ranks candidates; it does not pick one. Which document a project plans
    from is a fact about the project, and the one thing here no probe can settle.
    """
    out = gitops.git("ls-files", "*.md", cwd=repo, check=False)
    found = []
    for rel in out.splitlines():
        rel = rel.strip()
        if not rel or rel.startswith(("tasks/", "factory/")):
            continue
        sha = gitops.git(
            "log", "-1", "--format=%h %ad", "--date=short", "--", rel, cwd=repo,
            check=False,
        ).strip()
        if not sha:
            continue
        try:
            lines = len((repo / rel).read_text(encoding="utf-8").splitlines())
        except OSError:
            lines = 0
        found.append(SpecCandidate(path=rel, last_commit=sha, lines=lines))
    # Longest first: a spec of record is a design document, and the harness's
    # own README is not. A tie-break, not a rule — the operator still chooses.
    found.sort(key=lambda c: c.lines, reverse=True)
    return found[:limit]


def drift(
    before: Grounding, after: Grounding, checks: list[InputCheck]
) -> tuple[list[str], list[str]]:
    """`(regressions, improvements)` — what *changed* since grounding.

    Drift is change, not current state. An input that could not be reached when
    grounding was taken and still cannot has not drifted: it is a known
    condition that `require_reachable_inputs` already refuses on, and reporting
    it here as news would make `--check` fail permanently on a repository whose
    grounding is perfectly accurate. That is how a check gets ignored.

    Counts are excluded for the same reason from the other end — this suite went
    from 81 to 422 as the factory merged into it, so a changed count is the
    ordinary case and the caller prints it as information.
    """
    was_blocking = set(before.blocking)
    now_blocking = {c.path for c in checks if c.blocks}
    reasons = {c.path: c.why for c in checks}

    regressions = [
        f"input `{path}` became unreachable — {reasons.get(path, '')}"
        for path in sorted(now_blocking - was_blocking)
    ]
    improvements = [
        f"input `{path}` is reachable now"
        for path in sorted(was_blocking - now_blocking)
    ]

    for name in sorted(set(before.surfaces) - set(after.surfaces)):
        regressions.append(f"surface `{name}` was present at grounding and is not now")
    for name in sorted(set(after.surfaces) - set(before.surfaces)):
        improvements.append(f"surface `{name}` has appeared since grounding")
    if before.spec_path and not after.spec_path:
        regressions.append(f"spec `{before.spec_path}` no longer resolves")
    return regressions, improvements


def load(path: Path) -> Grounding | None:
    """Read `tasks/GROUNDING.md`. `None` means ungrounded, which is not an error."""
    if not path.is_file():
        return None
    raw = path.read_text(encoding="utf-8")
    match = FRONTMATTER.match(raw)
    if match is None:
        raise GroundError(f"{path.name}: no `+++` TOML frontmatter at the top")
    try:
        meta = tomllib.loads(match.group(1))
    except tomllib.TOMLDecodeError as exc:
        raise GroundError(
            f"{path.name}: frontmatter is not valid TOML — {exc}"
        ) from exc

    inputs = meta.get("inputs") or {}
    declared = inputs.get("planner_must_read", [])
    if not isinstance(declared, list) or any(not isinstance(p, str) for p in declared):
        raise GroundError(
            f"{path.name}: [inputs] planner_must_read must be a list of strings"
        )
    measured = meta.get("measured") or {}
    if not isinstance(measured, dict):
        raise GroundError(f"{path.name}: [measured] must be a table")
    return Grounding(
        grounded_at=str(meta.get("grounded_at", "")),
        grounded_commit=str(meta.get("grounded_commit", "")),
        planner_must_read=declared,
        spec_path=str((meta.get("spec") or {}).get("path", "")),
        measured=measured,
        surfaces=[str(s) for s in (meta.get("surfaces") or [])],
        blocking=[str(p) for p in (inputs.get("unreachable_when_measured") or [])],
        conventions=[
            str(c) for c in ((meta.get("conventions") or {}).get("sources") or [])
        ],
    )


def render(
    *,
    grounded_at: str,
    grounded_commit: str,
    declared: list[str],
    divergences: list[Divergence],
    checks: list[InputCheck],
    spec_path: str = "",
    conventions: list[str] | None = None,
    measured: dict | None = None,
    surfaces: list[str] | None = None,
    probes: list[Probe] | None = None,
) -> str:
    """The `tasks/GROUNDING.md` a `ground` run writes.

    `planner_must_read` is carried through rather than re-derived: it is the one
    thing here no probe can answer, so regenerating the report must never be
    able to silently discard it.
    """
    measured = measured or {}
    conventions = conventions or []
    surfaces = surfaces or []
    probes = probes or []
    declared_toml = ", ".join(f'"{p}"' for p in declared)
    lines = [
        "+++",
        f'grounded_at = "{grounded_at}"',
        f'grounded_commit = "{grounded_commit}"',
        "surfaces = [" + ", ".join(f'"{s}"' for s in surfaces) + "]",
        "",
        "[spec]",
        "# The spec of record. `cut --spec` defaults to this; nothing probes it,",
        "# and a packet pinned to the wrong file makes the drift check watch",
        "# something the work has nothing to do with.",
        f'path = "{spec_path}"',
        "",
        "[conventions]",
        "# Where this project states how it does things. Prose about intent, so",
        "# `run.py conventions` checks these rather than believing them — a rule",
        "# that has gone stale is worth more as a finding than as a rule.",
        "sources = [" + ", ".join(f'"{c}"' for c in conventions) + "]",
        "",
        "[measured]",
        "# Snapshots, not standing facts — stamped with the commit above and",
        "# re-measured by `ground --check`. No gate reads them: the baseline is",
        "# taken on each task's own fork point and the format gate is scoped to",
        "# the diff. They are here so drift is visible, not so anything trusts them.",
        *(f"{key} = {value}" for key, value in sorted(measured.items())),
        "",
        "[inputs]",
        "# Paths the planning tier must be able to read to check a claim against",
        "# the thing that decides it. Nothing probes this — it is the one answer",
        "# an operator has to supply. A path listed here that no worktree will",
        "# contain makes `cut` and `assess` refuse to start.",
        f"planner_must_read = [{declared_toml}]",
        "# What could not be reached last time this was measured. `ground --check`",
        "# compares against it, so it reports change rather than restating a",
        "# condition you already know about.",
        "unreachable_when_measured = ["
        + ", ".join(f'"{c.path}"' for c in checks if c.blocks)
        + "]",
        "+++",
        "",
        "# Grounding",
        "",
        "<!-- Regenerated by `factory/run.py ground`. The prose is measured and",
        "     will be overwritten; `planner_must_read` above is yours and is",
        "     carried through untouched. -->",
        "",
        f"Measured at `{grounded_commit}` on {grounded_at}.",
        "",
        "## Declared inputs",
        "",
    ]
    if not checks:
        lines += [
            "None declared. If this project has input files the planning tier is",
            "meant to open — data, fixtures, downloaded sources — add them to",
            "`planner_must_read` above. Until then the ground-truth ladder's first",
            "rung is unenforced, and a packet may describe files nobody checked.",
            "",
        ]
    else:
        lines += ["| Path | Status | |", "|---|---|---|"]
        for check in checks:
            mark = "⛔" if check.blocks else "✅"
            lines.append(f"| `{check.path}` | {check.status} | {mark} {check.why} |")
        lines.append("")

    lines += [
        "## Present for you, absent for a planner",
        "",
        "`git status --porcelain --ignored`, run in the operator's checkout. A",
        "worktree carries tracked files only, so everything below is invisible to",
        "a cutter or an assessor.",
        "",
    ]
    unclassified = [d for d in divergences if not d.noise]
    if not divergences:
        lines.append("Nothing ignored is present in this checkout.")
    else:
        for div in divergences:
            lines.append(f"- `{div.path}` — {div.label}")
    lines.append("")
    if unclassified:
        lines += [
            "The unclassified entries are the ones to look at: if any is an input",
            "the planning tier needs, add it to `planner_must_read`.",
            "",
        ]

    if probes:
        lines += [
            "## What was run",
            "",
            "Every line here is a command and what it said, so any of it can be",
            "re-run rather than believed.",
            "",
            "| | Probe | Command | Result |",
            "|---|---|---|---|",
        ]
        for probe in probes:
            mark = "✅" if probe.ok else "⚠️"
            lines.append(
                f"| {mark} | `{probe.name}` | `{probe.command}` | {probe.detail} |"
            )
        lines.append("")
    return "\n".join(lines)


def summary(grounding: Grounding | None, checks: list[InputCheck]) -> str:
    """`{{GROUNDING}}` — what a planning agent is told about its own worktree."""
    if grounding is None:
        return (
            "This repository has not been grounded, so nothing has been measured "
            "about what your worktree does and does not contain. Treat every "
            "claim about a file you cannot open as `unverifiable`."
        )
    lines = [
        "Your worktree was created by `git worktree add` and carries **tracked "
        "files only**. This was measured, not assumed:",
        "",
    ]
    if not checks:
        lines.append(
            "- No input paths are declared for this project, so nothing is known "
            "to be missing — and nothing is known to be present either. If the "
            "task you are working on parses a file, establish that the file is "
            "there before you describe it."
        )
    for check in checks:
        mark = "⛔" if check.blocks else "✅"
        lines.append(f"- {mark} `{check.path}` — {check.why}")
    lines += [
        "",
        "A path marked ⛔ cannot be opened from here. Do not describe its "
        "contents from the spec: say so, and let the claim block.",
    ]
    return "\n".join(lines)
