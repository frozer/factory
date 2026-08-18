"""Task packets — the unit of work handed to a model.

A packet is one markdown file under `tasks/`, opening with TOML frontmatter
delimited by `+++`. TOML rather than YAML because `tomllib` is in the stdlib
since 3.11 and parses the nested `[[invariants]]` table array properly; the
alternative was hand-rolling a YAML subset, which is where this kind of harness
usually acquires its first silent bug.

The body below the frontmatter is handed to the implementer verbatim. It must be
self-contained: a packet that says "see the spec" sends a small model into a
design document hundreds of lines long with no budget left for the task.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FRONTMATTER = re.compile(r"\A\+\+\+\s*\n(.*?)\n\+\+\+\s*\n?", re.DOTALL)

ID_RE = re.compile(r"\A[A-Z]\d{2}[a-z]?\Z")

# `tasks/B01-adrh-loader.md` is a packet; `README.md`, `CUT-REPORT.md`,
# `TRAPS.md` and `GROUNDING.md` are not. The id prefix is what tells them apart.
#
# This used to be a denylist of one filename, which made `tasks/` a directory
# where only packets could live — every other document the factory writes there
# broke `load_all` on sight, and `CUT-REPORT.md` is written by every cut. A
# denylist cannot be right here: the set of non-packet files grows, and the set
# of packet filenames is the one that is actually specified.
PACKET_FILE = re.compile(r"\A[A-Z]\d{2}[a-z]?-")


def is_packet_filename(name: str) -> bool:
    return name.endswith(".md") and PACKET_FILE.match(name) is not None

# Capability tiers, weakest first. The order is load-bearing: the reviewer rule
# below is an index comparison, so this tuple *is* the definition of "stronger".
#
# Tiers rather than model names because a packet is not entitled to an opinion
# about which model runs it. `basic` says the task needs no capability beyond
# following a fully-specified packet; which model clears that bar is a fact
# about the runtime, and lives in `agent.TIER_MODELS`.
TIERS = ("basic", "standard", "advanced")

# Who reviews whom. A reviewer is never weaker than the implementer — same-tier
# self-review is theatre, and on this spec most failures are green.
REVIEWER_LADDER = {"basic": "standard", "standard": "standard", "advanced": "advanced"}

SURFACES = ("api", "webapp")

# The repo this module ships in, for the one rule that has to look outside the
# packet. `parse` takes it as an argument so a test can point the rule at a
# fixture; this is only the default.
REPO = Path(__file__).resolve().parent.parent

SPECS_PREFIX = "docs/specs/"

FEATURES_DIR = "features"

# One row of a manifest's `## Depends on` table: `| `SPEC_x.md` | §5 | `abc123` |`.
# Only the document column is read here — which sections a manifest watches is the
# business of whatever generates the manifest, and this rule just needs to know
# that some manifest already speaks for the document. The pattern is the document
# half of that generator's dependency-row pattern, and has to keep agreeing with
# it: a row one reads as a dependency and the other does not is a packet waved
# through against a document something is already watching.
MANIFEST_DEP_ROW = re.compile(r"^\|\s*`?([A-Za-z0-9_.-]+\.md)`?\s*\|")

# `## Files you may CREATE`, and the backticked bullets under it. Backticks are
# required, which is what keeps `- none. Everything this task needs already
# exists` out of the list without special-casing the word.
CREATE_HEADING = re.compile(r"^##\s+Files you may CREATE\s*$", re.IGNORECASE)
NEXT_HEADING = re.compile(r"^##\s+")
CREATE_BULLET = re.compile(r"^[-*]\s+`([^`]+)`")


class PacketError(ValueError):
    """A packet that cannot be trusted to drive a run."""


@dataclass(frozen=True, slots=True)
class Invariant:
    """One checkable claim about the finished work.

    These are what the reviewer is scored against. A reviewer asked "does this
    look right?" says yes; a reviewer required to return held/violated for a
    named id, with a `file:line`, has to actually look.
    """

    id: str
    assertion: str
    critical: bool = True


@dataclass(frozen=True, slots=True)
class Packet:
    id: str
    slug: str
    goal: str
    tier: str
    reviewer: str
    gate: str
    surface: str
    spec_commit: str
    spec_path: str
    #: A pytest marker to run as a *second*, separate pytest invocation, or
    #: `""` for none. Not "this task uses a database" — that is what the name
    #: `needs_db` said, and that name is what led packets to set it for a marker
    #: their project never registered. It selects a held-out subset of the suite;
    #: where the project holds none out, there is nothing to ask for.
    extra_pytest_marker: str
    max_attempts: int
    requires: list[str]
    invariants: list[Invariant]
    body: str
    path: Path
    sha256: str
    ordinal: int = 0
    forbidden_paths: list[str] = field(default_factory=list)
    deletable_paths: list[str] = field(default_factory=list)
    create_paths: list[str] = field(default_factory=list)

    @property
    def critical_invariants(self) -> list[Invariant]:
        return [i for i in self.invariants if i.critical]

    def to_meta(self, repo: Path) -> dict[str, Any]:
        """The subset `db.upsert_packet` stores."""
        return {
            "id": self.id,
            "slug": self.slug,
            "goal": self.goal,
            "packet_path": self.path.relative_to(repo).as_posix(),
            "packet_sha256": self.sha256,
            "spec_commit": self.spec_commit,
            "spec_path": self.spec_path,
            # The `tasks.model`/`tasks.reviewer` columns hold *tiers*. The
            # column names predate the tier vocabulary and are left alone
            # because `db._migrate` can only add columns, never rename one.
            "model": self.tier,
            "reviewer": self.reviewer,
            "gate": self.gate,
            "extra_pytest_marker": self.extra_pytest_marker,
            "surface": self.surface,
            "max_attempts": self.max_attempts,
            "requires": self.requires,
        }


def _require(meta: dict[str, Any], key: str, path: Path) -> Any:
    if key not in meta:
        raise PacketError(f"{path.name}: frontmatter is missing `{key}`")
    return meta[key]


def _extra_marker(meta: dict) -> str:
    """`extra_pytest_marker`, accepting the retired `needs_db` spelling.

    `needs_db = true` meant "also run `pytest -m db`", and its name did not say
    so. A cutter reading the name sets it wherever the task's tests touch a
    datastore, which is not what it asks for. It asks for a *held-out subset of
    the suite*, and a project that holds none out has nothing to give it.

    The old key is still honoured rather than rejected, because packets already
    written carry it and `sync` re-reads the merged ones: refusing it would break
    the board for work that has long since finished. It is translated, not
    deprecated in place, so a packet keeps meaning what it meant.
    """
    marker = meta.get("extra_pytest_marker")
    if marker is not None:
        return str(marker).strip()
    return "db" if bool(meta.get("needs_db", False)) else ""


def _str_list(value: Any, key: str, path: Path) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise PacketError(f"{path.name}: `{key}` must be a list of strings")
    return list(value)


def _manifest_covering(spec_path: str, repo: Path) -> Path | None:
    """The `features/` manifest that already lists this document, if there is one.

    Reads defensively and answers `None` for everything it is not sure about: a
    `features/` that is absent, or a file in it that cannot be read, is not
    evidence about the packet, and turning either into a parse failure would
    halt a sync over the state of a directory the packet never mentions.
    """
    normalized = spec_path.strip().replace("\\", "/")
    if not normalized.startswith(SPECS_PREFIX):
        return None
    document = normalized.rsplit("/", 1)[-1]

    directory = repo / FEATURES_DIR
    if not directory.is_dir():
        return None
    for manifest in sorted(directory.glob("*.md")):
        try:
            text = manifest.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            row = MANIFEST_DEP_ROW.match(line)
            if row is not None and row.group(1) == document:
                return manifest
    return None


def _create_paths(body: str) -> list[str]:
    """The paths under `## Files you may CREATE`, in the order the packet lists them.

    Read from the body rather than the frontmatter because that is where a cutter
    writes them and where an implementer reads them; a second copy in `+++` would
    be a second thing to keep true.
    """
    paths: list[str] = []
    inside = False
    for line in body.splitlines():
        if CREATE_HEADING.match(line):
            inside = True
            continue
        if inside and NEXT_HEADING.match(line):
            break
        if inside:
            bullet = CREATE_BULLET.match(line.strip())
            if bullet is not None:
                paths.append(bullet.group(1).strip().replace("\\", "/"))
    return paths


def already_landed(pkt: Packet, repo: Path) -> list[str]:
    """Paths the packet is to CREATE that are already files in the tree.

    A packet cannot create a file that exists, so any hit here is a packet with
    no remaining work — or with work it can only do by editing outside its own
    boundary, which the gates refuse anyway. Cheap, mechanical, and deliberately
    so: packets sit in the queue as `ready` while the branch that cut them
    implements them by hand, and a re-assessment asked to notice this catches only
    some of them. A verdict is a judgement and this is a stat call.

    Says nothing about a packet that only EDITs. Those are the ones this cannot
    see, and a packet whose whole boundary is an edit list has to be judged.
    """
    landed = []
    for rel in pkt.create_paths:
        candidate = repo / rel
        try:
            if candidate.is_file():
                landed.append(rel)
        except OSError:
            continue
    return landed


def parse(path: Path, *, ordinal: int = 0, repo: Path = REPO) -> Packet:
    """Read and validate one packet. Raises `PacketError` with the file named.

    Validation is strict and total: an invalid packet halts the sync rather than
    being skipped, because a queue that silently drops work is worse than one
    that refuses to start.

    `repo` is only where `features/` is looked for — see `_manifest_covering`.
    """
    raw = path.read_text(encoding="utf-8")
    match = FRONTMATTER.match(raw)
    if match is None:
        raise PacketError(f"{path.name}: no `+++` TOML frontmatter at the top")

    try:
        meta = tomllib.loads(match.group(1))
    except tomllib.TOMLDecodeError as exc:
        raise PacketError(
            f"{path.name}: frontmatter is not valid TOML — {exc}"
        ) from exc

    body = raw[match.end() :].strip()
    if not body:
        raise PacketError(f"{path.name}: the packet body is empty")

    task_id = str(_require(meta, "id", path))
    if not ID_RE.match(task_id):
        raise PacketError(
            f"{path.name}: id {task_id!r} must look like 'B01' or 'B09a' — "
            "the prefix groups the phase and the order is the queue order"
        )
    if path.stem.split("-")[0] != task_id:
        raise PacketError(
            f"{path.name}: filename must start with the id {task_id!r} "
            "so a packet is findable from a task row"
        )

    # `model = "haiku"` was the old spelling. Rejected loudly rather than
    # translated: a packet naming a model is asserting something it no longer
    # gets to decide, and a silent alias would keep that spelling alive in
    # every packet cut from an older example.
    if "model" in meta:
        raise PacketError(
            f"{path.name}: `model` is no longer a packet field — use "
            f"`tier`, one of {TIERS}. Which model runs a tier is decided by "
            "`agent.TIER_MODELS`, not by the packet"
        )

    tier = str(_require(meta, "tier", path))
    if tier not in TIERS:
        raise PacketError(f"{path.name}: tier must be one of {TIERS}, got {tier!r}")

    reviewer = str(meta.get("reviewer", REVIEWER_LADDER[tier]))
    if reviewer not in TIERS:
        raise PacketError(f"{path.name}: reviewer must be one of {TIERS}")
    if TIERS.index(reviewer) < TIERS.index(tier):
        raise PacketError(
            f"{path.name}: reviewer {reviewer!r} is weaker than implementer "
            f"{tier!r} — a review is only worth running if it can catch the "
            "implementer's mistakes"
        )

    gate = str(meta.get("gate", "auto"))
    if gate not in ("auto", "human"):
        raise PacketError(f"{path.name}: gate must be 'auto' or 'human'")

    surface = str(meta.get("surface", "api"))
    if surface not in SURFACES:
        raise PacketError(f"{path.name}: surface must be one of {SURFACES}")

    invariants = _parse_invariants(meta, path)

    spec_commit = str(_require(meta, "spec_commit", path)).strip()
    if not spec_commit:
        raise PacketError(
            f"{path.name}: `spec_commit` is required — it is what stops a stale "
            "packet from being run after the spec moved underneath it"
        )

    # A packet that pins a raw document a manifest already covers is watching
    # one file while its work draws on every section that manifest lists — the
    # drift check then stays silent through a change the packet was wrong
    # about. The manifest exists precisely so that cannot happen, so the packet
    # is refused rather than allowed to re-open the hole. A document no
    # manifest names is left alone: a one-document scope is a real scope.
    spec_path = str(_require(meta, "spec_path", path))
    covering = _manifest_covering(spec_path, repo)
    if covering is not None:
        raise PacketError(
            f"{path.name}: `spec_path` {spec_path!r} is covered by "
            f"{covering.relative_to(repo).as_posix()} — pin that manifest "
            "instead. Pinning the document watches one file, while the "
            "manifest watches every section the scope actually draws on"
        )

    forbidden_paths = _str_list(
        meta.get("forbidden_paths", []), "forbidden_paths", path
    )
    deletable_paths = _str_list(
        meta.get("deletable_paths", []), "deletable_paths", path
    )
    overlap = sorted(set(forbidden_paths) & set(deletable_paths))
    if overlap:
        raise PacketError(
            f"{path.name}: {overlap} listed in both `forbidden_paths` and "
            "`deletable_paths` — a path cannot be both untouchable and "
            "removable"
        )

    return Packet(
        id=task_id,
        slug=str(meta.get("slug", path.stem.split("-", 1)[-1])),
        goal=str(_require(meta, "goal", path)).strip(),
        tier=tier,
        reviewer=reviewer,
        gate=gate,
        surface=surface,
        spec_commit=spec_commit,
        spec_path=spec_path,
        extra_pytest_marker=_extra_marker(meta),
        max_attempts=int(meta.get("max_attempts", 3)),
        requires=_str_list(meta.get("requires", []), "requires", path),
        invariants=invariants,
        forbidden_paths=forbidden_paths,
        deletable_paths=deletable_paths,
        create_paths=_create_paths(body),
        body=body,
        path=path,
        sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        ordinal=ordinal,
    )


def _parse_invariants(meta: dict[str, Any], path: Path) -> list[Invariant]:
    rows = meta.get("invariants", [])
    if not isinstance(rows, list) or not rows:
        raise PacketError(
            f"{path.name}: at least one `[[invariants]]` block is required. "
            "The invariants are the review checklist; without them the reviewer "
            "has nothing to be scored against and will pass anything green"
        )
    out: list[Invariant] = []
    seen: set[str] = set()
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise PacketError(f"{path.name}: invariant #{i + 1} is not a table")
        inv_id = str(row.get("id", "")).strip()
        assertion = str(row.get("assert", "")).strip()
        if not inv_id or not assertion:
            raise PacketError(
                f"{path.name}: invariant #{i + 1} needs both `id` and `assert`"
            )
        if inv_id in seen:
            raise PacketError(f"{path.name}: duplicate invariant id {inv_id!r}")
        seen.add(inv_id)
        out.append(
            Invariant(
                id=inv_id, assertion=assertion, critical=bool(row.get("critical", True))
            )
        )
    return out


def load_all(tasks_dir: Path) -> list[Packet]:
    """Every packet in `tasks/`, in filename order.

    Filename order is the queue's tie-break, so `S01` runs before `B01` without
    anyone having to state it. Dependencies still gate the actual order.
    """
    if not tasks_dir.is_dir():
        return []
    files = sorted(p for p in tasks_dir.glob("*.md") if is_packet_filename(p.name))
    packets = [parse(p, ordinal=i) for i, p in enumerate(files)]

    seen: dict[str, Path] = {}
    for pkt in packets:
        if pkt.id in seen:
            raise PacketError(
                f"duplicate task id {pkt.id!r} in {pkt.path.name} "
                f"and {seen[pkt.id].name}"
            )
        seen[pkt.id] = pkt.path

    known = set(seen)
    for pkt in packets:
        missing = [d for d in pkt.requires if d not in known]
        if missing:
            raise PacketError(
                f"{pkt.path.name}: requires unknown task(s) {missing} — "
                "a dependency on a packet that does not exist can never unlock"
            )
    return packets
