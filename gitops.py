"""Git, wrapped just enough.

Every task runs in its own worktree so a wedged attempt can never dirty the
branch the factory merges into, and so killing the loop mid-run leaves nothing
to clean up by hand. Merges are squashes: one task becomes one commit, which
makes a bad task exactly one `git revert` to undo.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


class DepsError(RuntimeError):
    pass


def git(*args: str, cwd: Path, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed ({proc.returncode}) in {cwd}:\n"
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout


def rev_parse(repo: Path, ref: str) -> str:
    return git("rev-parse", ref, cwd=repo).strip()


def current_branch(repo: Path) -> str:
    return git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo).strip()


def repo_name(repo: Path) -> str:
    """What to call this repository in a prompt.

    The remote's basename rather than the checkout directory's: a working copy
    is routinely cloned, or worktree'd, under a different name, and the name in
    a prompt should be the one the operator would recognise. Falls back to the
    directory name when there is no remote, which is the case for a repo that
    has never been pushed.
    """
    url = git("remote", "get-url", "origin", cwd=repo, check=False).strip()
    if url:
        base = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        if base.endswith(".git"):
            base = base[: -len(".git")]
        if base:
            return base
    return repo.name


def is_dirty(path: Path) -> list[str]:
    """Tracked changes only — gitignored factory state is expected and fine."""
    out = git("status", "--porcelain", "--untracked-files=no", cwd=path)
    return [line.strip() for line in out.splitlines() if line.strip()]


def spec_moved(repo: Path, spec_commit: str, spec_path: str) -> list[str]:
    """Commits touching the spec since the packet was cut.

    A non-empty result means the packet may describe work that no longer matches
    the design. That doc was amended five times in two days and two amendments
    invalidated decisions a packet would already have carried, so this check is
    not paranoia — it is the reason packets record a commit at all.
    """
    out = git(
        "log",
        "--oneline",
        f"{spec_commit}..HEAD",
        "--",
        spec_path,
        cwd=repo,
        check=False,
    )
    return [line.strip() for line in out.splitlines() if line.strip()]


def worktree_add(repo: Path, path: Path, branch: str, base: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = git("branch", "--list", branch, cwd=repo).strip()
    if existing:
        git("worktree", "add", str(path), branch, cwd=repo)
    else:
        git("worktree", "add", "-b", branch, str(path), base, cwd=repo)


# What each surface is, in one place: its directory, the manifest that proves
# the directory is that surface, and how its dependencies get installed.
# `ground` reads this to check a surface is present without installing anything,
# which is the whole reason it is a table rather than an `if` inside the
# installer — a second copy is how `load_all` and `_is_packet` came to disagree.
SURFACES = {
    "api": {"dir": "api", "manifest": "pyproject.toml", "tool": "uv", "args": ["sync"]},
    "webapp": {
        "dir": "webapp",
        "manifest": "package.json",
        "tool": "npm",
        "args": ["ci"],
    },
}


def install_deps(worktree: Path, surface: str) -> None:
    """`.venv` and `node_modules` are both gitignored, so a fresh worktree
    starts with neither — without this, the first thing an implementer or a
    gate hits is a missing toolchain it cannot fix from inside its sandbox.
    """
    spec = SURFACES.get(surface, SURFACES["api"])
    tool, args, cwd = spec["tool"], spec["args"], worktree / spec["dir"]
    exe = shutil.which(tool)
    if exe is None:
        raise DepsError(f"`{tool}` is not on PATH")
    proc = subprocess.run(
        [exe, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise DepsError(
            f"{tool} {' '.join(args)} failed ({proc.returncode}) in {cwd}:\n"
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )


def worktree_remove(repo: Path, path: Path) -> None:
    git("worktree", "remove", "--force", str(path), cwd=repo, check=False)
    git("worktree", "prune", cwd=repo, check=False)


def branch_delete(repo: Path, branch: str) -> None:
    git("branch", "-D", branch, cwd=repo, check=False)


def ensure_excluded(repo: Path, pattern: str = ".factory/") -> None:
    """Put `pattern` in `.git/info/exclude`, which every worktree shares.

    Belt and braces for `.gitignore`: a task branch forks from whatever the
    feature branch contains, and if that commit predates the ignore rule, the
    agent's `result.json` and the review diff would be swept into the task's
    commit and carried through the merge. `info/exclude` is not versioned, so it
    holds regardless of what the branch knows.
    """
    common = git("rev-parse", "--git-common-dir", cwd=repo).strip()
    exclude = repo / common if not Path(common).is_absolute() else Path(common)
    exclude = exclude / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    current = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    if pattern not in current.split():
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write(f"\n# factory state, never part of a task's diff\n{pattern}\n")


def commit_all(worktree: Path, message: str) -> str | None:
    """Stage and commit the agent's work. Returns the sha, or None if there was
    nothing to commit.

    Plain `git add -A`, with no pathspec: git skips ignored files silently, but
    an *explicit* pathspec that matches one is a hard error. Naming `.` here to
    be careful about `.factory/` is what made the first real run fail — the
    ignore rule was already doing the job.
    """
    git("add", "-A", cwd=worktree)
    staged = git("diff", "--cached", "--name-only", cwd=worktree).strip()
    if not staged:
        return None
    git("-c", "core.hooksPath=/dev/null", "commit", "-m", message, cwd=worktree)
    return rev_parse(worktree, "HEAD")


def changed_files(worktree: Path, base: str, head: str = "HEAD") -> list[str]:
    out = git("diff", "--name-only", f"{base}..{head}", cwd=worktree)
    return [line.strip() for line in out.splitlines() if line.strip()]


def diff_text(worktree: Path, base: str, head: str = "HEAD") -> str:
    return git("diff", f"{base}..{head}", cwd=worktree)


def touched(
    worktree: Path, base: str, paths: list[str], head: str = "HEAD"
) -> list[str]:
    """Which of `paths` the diff modified. Used for the forbidden-path gate and
    for catching a supplied test that was edited into submission."""
    if not paths:
        return []
    out = git(
        "diff",
        "--name-only",
        f"{base}..{head}",
        "--",
        *paths,
        cwd=worktree,
        check=False,
    )
    return [line.strip() for line in out.splitlines() if line.strip()]


def squash_merge(repo: Path, branch: str, message: str) -> str:
    """Collapse a task branch onto whatever the repo currently has checked out.

    Squash rather than merge: the attempt-by-attempt history lives in
    `.factory/runs/`, and what the branch wants is one revertible commit per
    task rather than a thicket of "fix review finding" commits.
    """
    git("merge", "--squash", branch, cwd=repo)
    git("-c", "core.hooksPath=/dev/null", "commit", "-m", message, cwd=repo)
    return rev_parse(repo, "HEAD")


def amend_with(repo: Path, paths: list[str]) -> str:
    """Fold `paths` into the commit just made, keeping the message.

    Used for the regenerated board. A separate "update the board" commit after
    every task is exactly the bookkeeping noise that keeping state out of git
    was meant to avoid — and leaving it uncommitted is worse, because the loop
    checks for a clean tree and would refuse to start the next run.
    """
    git("add", "--", *paths, cwd=repo)
    git("-c", "core.hooksPath=/dev/null", "commit", "--amend", "--no-edit", cwd=repo)
    return rev_parse(repo, "HEAD")


def abort_merge(repo: Path) -> None:
    git("merge", "--abort", cwd=repo, check=False)
    git("reset", "--hard", cwd=repo, check=False)
