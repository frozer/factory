"""The structured responses, and the harness's authority over them.

Every agent *writes a file* rather than ending its turn with JSON — models
reliably write files and unreliably produce a clean final message. The file is
validated here; a validation error is fed back verbatim for exactly one retry,
because a model given a precise schema complaint usually fixes it, and a model
that fails twice is not going to succeed on a third.

Three shapes, one per stage: `result.json` from the implementer, `review.json`
from the reviewer, and `assess.json` from the assessor that rules on a packet
before either of them ever sees it.

Each has a paired override — `effective_verdict` and `effective_assessment` —
because in both cases the agent is the party with an incentive to be generous
and the harness is the party holding the evidence.

Validation is hand-rolled rather than `jsonschema`: the factory is stdlib-only,
the shapes are small, and a bespoke validator produces error messages that name
the offending field in the language of this design.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packet import Packet

IMPL_STATUS = ("complete", "blocked", "failed")
VERDICTS = ("accept", "revise", "reject")
INVARIANT_STATUS = ("held", "violated", "unverifiable")
SEVERITIES = ("blocker", "major", "minor")

# An assessment rules on the packet, not on any code, so its verdicts are about
# what to do with the packet: queue it, cut it again, or abandon the split.
ASSESS_VERDICTS = ("ready", "recut", "reject")

# Where the truth for a claim lives. The assessor classifies each claim so the
# kind can decide how it is weighed: only `invariant` claims block on their own,
# because an invariant that cannot be verified is one the reviewer will later be
# scored against and unable to answer.
CLAIM_KINDS = ("data", "code", "schema", "toolchain", "harness", "invariant")

# What a conventions document can assert about a project. Separate from
# CLAIM_KINDS because the object under test is different: those classify where
# the truth about a *packet* lives, these classify what a *document* is claiming.
CONVENTION_KINDS = ("structural", "toolchain", "behavioural", "prohibition")

# All five must be answered, every time. An omitted check is not a passed check
# — the whole point is that the assessor cannot decline the awkward question.
STRUCTURAL_CHECKS = (
    "satisfiable-in-boundary",
    "deps-complete",
    "fixtures-gate-clean",
    "harness-traps-present",
    "tier-justified",
)


class ContractError(ValueError):
    """A response the harness cannot act on. The message is shown to the model."""


@dataclass(frozen=True, slots=True)
class ImplResult:
    task_id: str
    status: str
    files_created: list[str]
    files_modified: list[str]
    files_deleted: list[str]
    tests_added: list[str]
    verification: dict[str, Any]
    deviations: list[dict[str, str]]
    blocked_reason: str | None
    notes: str
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ReviewResult:
    task_id: str
    verdict: str
    invariants: list[dict[str, str]]
    findings: list[dict[str, Any]]
    merge_ok: bool
    raw: dict[str, Any]

    def violated(self) -> list[str]:
        return [i["id"] for i in self.invariants if i["status"] == "violated"]

    def unverifiable(self) -> list[str]:
        return [i["id"] for i in self.invariants if i["status"] == "unverifiable"]

    def blockers(self) -> list[dict[str, Any]]:
        return [f for f in self.findings if f["severity"] == "blocker"]


@dataclass(frozen=True, slots=True)
class AssessResult:
    """A ruling on one packet, pinned to the bytes that were ruled on."""

    packet_id: str
    packet_sha256: str
    verdict: str
    claims: list[dict[str, Any]]
    structural: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    raw: dict[str, Any]

    def failed_checks(self) -> list[str]:
        return [c["check"] for c in self.structural if c["status"] == "fail"]

    def unsound_invariants(self) -> list[dict[str, Any]]:
        """Invariant-kind claims the assessor could not stand behind.

        `unverifiable` counts alongside `violated` on purpose. An invariant
        nobody can check is one the reviewer will be required to rule on later
        and equally unable to answer, and `effective_verdict` blocks there too —
        so letting it through here only moves the same halt three runs later.
        """
        return [
            c for c in self.claims if c["kind"] == "invariant" and c["status"] != "held"
        ]

    def blocking_findings(self) -> list[dict[str, Any]]:
        return [f for f in self.findings if f["severity"] in ("blocker", "major")]


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ContractError(
            f"you did not write {path.name}. It is required, at exactly that "
            "path, as the last thing you do."
        )
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ContractError(f"{path.name} is not valid JSON — {exc}") from exc
    if not isinstance(obj, dict):
        raise ContractError(
            f"{path.name} must be a JSON object, not a {type(obj).__name__}"
        )
    return obj


def _enum(obj: dict, key: str, allowed: tuple[str, ...], where: str) -> str:
    value = obj.get(key)
    if value not in allowed:
        raise ContractError(f"{where}: `{key}` must be one of {allowed}, got {value!r}")
    return str(value)


def _strlist(obj: dict, key: str, where: str) -> list[str]:
    value = obj.get(key, [])
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ContractError(f"{where}: `{key}` must be a list of strings")
    return value


def validate_impl(obj: dict[str, Any], pkt: Packet) -> ImplResult:
    where = "result.json"
    if obj.get("task_id") != pkt.id:
        raise ContractError(
            f"{where}: `task_id` must be {pkt.id!r}, got {obj.get('task_id')!r}"
        )

    status = _enum(obj, "status", IMPL_STATUS, where)

    verification = obj.get("verification")
    if not isinstance(verification, dict) or "command" not in verification:
        raise ContractError(
            f"{where}: `verification` must be an object with at least `command`, "
            "`passed` and `failed` — report what you actually ran and saw"
        )

    deviations = obj.get("deviations", [])
    if not isinstance(deviations, list) or any(
        not isinstance(d, dict) or "from_packet" not in d or "reason" not in d
        for d in deviations
    ):
        raise ContractError(
            f"{where}: `deviations` must be a list of objects with `from_packet` "
            "and `reason`. Use [] if you followed the packet exactly — this field "
            "is where you declare anything you did differently, and an omission "
            "here is worse than a deviation."
        )

    if status == "blocked" and not str(obj.get("blocked_reason") or "").strip():
        raise ContractError(
            f"{where}: status is 'blocked' so `blocked_reason` is required"
        )

    files_deleted = _strlist(obj, "files_deleted", where)
    not_authorized = sorted(set(files_deleted) - set(pkt.deletable_paths))
    if not_authorized:
        raise ContractError(
            f"{where}: `files_deleted` names {not_authorized}, which the packet "
            "does not list under its deletable paths. You have no way to delete "
            "a file yourself — declare it here and the harness removes it, but "
            "only paths the packet explicitly authorizes."
        )

    return ImplResult(
        task_id=pkt.id,
        status=status,
        files_created=_strlist(obj, "files_created", where),
        files_modified=_strlist(obj, "files_modified", where),
        files_deleted=files_deleted,
        tests_added=_strlist(obj, "tests_added", where),
        verification=verification,
        deviations=deviations,
        blocked_reason=obj.get("blocked_reason"),
        notes=str(obj.get("notes", "")),
        raw=obj,
    )


def validate_review(obj: dict[str, Any], pkt: Packet) -> ReviewResult:
    where = "review.json"
    if obj.get("task_id") != pkt.id:
        raise ContractError(f"{where}: `task_id` must be {pkt.id!r}")

    verdict = _enum(obj, "verdict", VERDICTS, where)

    rows = obj.get("invariants")
    if not isinstance(rows, list):
        raise ContractError(f"{where}: `invariants` must be a list")

    seen: dict[str, str] = {}
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ContractError(f"{where}: invariant #{i + 1} is not an object")
        inv_id = row.get("id")
        if not isinstance(inv_id, str):
            raise ContractError(f"{where}: invariant #{i + 1} has no string `id`")
        state = _enum(row, "status", INVARIANT_STATUS, f"{where} invariant {inv_id!r}")
        if state != "unverifiable" and not str(row.get("evidence") or "").strip():
            raise ContractError(
                f"{where}: invariant {inv_id!r} is {state!r} but carries no "
                "`evidence`. Cite the file:line you based that on — a verdict "
                "without a location is not a review."
            )
        if inv_id in seen:
            raise ContractError(f"{where}: invariant {inv_id!r} answered twice")
        seen[inv_id] = state

    declared = {i.id for i in pkt.invariants}
    missing = sorted(declared - set(seen))
    unknown = sorted(set(seen) - declared)
    if missing:
        raise ContractError(
            f"{where}: you must return a status for EVERY declared invariant. "
            f"Missing: {missing}. Omitting one is not the same as passing it."
        )
    if unknown:
        raise ContractError(f"{where}: unknown invariant id(s) {unknown}")

    findings = obj.get("findings", [])
    if not isinstance(findings, list):
        raise ContractError(f"{where}: `findings` must be a list (use [] for none)")
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            raise ContractError(f"{where}: finding #{i + 1} is not an object")
        _enum(f, "severity", SEVERITIES, f"{where} finding #{i + 1}")
        if not str(f.get("claim") or "").strip():
            raise ContractError(f"{where}: finding #{i + 1} has no `claim`")

    return ReviewResult(
        task_id=pkt.id,
        verdict=verdict,
        invariants=[
            {
                "id": r["id"],
                "status": r["status"],
                "evidence": str(r.get("evidence", "")),
            }
            for r in rows
        ],
        findings=findings,
        merge_ok=bool(obj.get("merge_ok", verdict == "accept")),
        raw=obj,
    )


def effective_verdict(
    review: ReviewResult, *, gate_status: str, pkt: Packet
) -> tuple[str, str | None]:
    """What the harness does, which is not always what the reviewer said.

    A reviewer can be talked into `accept` by work that looks right; it cannot
    talk the harness out of a red gate or a violated invariant it reported
    itself. Every downgrade is recorded with its reason, so a pattern of
    reviewers over-accepting is visible in the data rather than folklore.
    """
    if review.verdict == "reject":
        return "reject", None

    if gate_status != "green":
        return "revise", f"gate is {gate_status}; a non-green gate can never merge"

    critical = {i.id for i in pkt.critical_invariants}

    violated = [i for i in review.violated() if i in critical]
    if violated:
        return "revise", f"critical invariant(s) reported violated: {violated}"

    unverifiable = [i for i in review.unverifiable() if i in critical]
    if unverifiable:
        return "revise", (
            f"critical invariant(s) could not be verified: {unverifiable}. "
            "An unverifiable critical invariant is not a pass — either the "
            "evidence is missing or the packet needs a better assertion."
        )

    blockers = review.blockers()
    if blockers:
        return (
            "revise",
            f"{len(blockers)} blocker finding(s) despite verdict {review.verdict!r}",
        )

    if review.verdict == "accept" and not review.merge_ok:
        return "revise", "reviewer accepted but set merge_ok=false"

    return review.verdict, None


def validate_assess(obj: dict[str, Any], pkt: Packet) -> AssessResult:
    """The assessor's ruling on a packet, before any implementer sees it.

    The sha check is the load-bearing one. An assessment of a packet that has
    since been edited is not an assessment of the packet that will run, and the
    natural failure mode here is an operator fixing a finding and reusing the
    verdict that found it.
    """
    where = "assess.json"
    if obj.get("packet_id") != pkt.id:
        raise ContractError(
            f"{where}: `packet_id` must be {pkt.id!r}, got {obj.get('packet_id')!r}"
        )

    got_sha = str(obj.get("packet_sha256") or "")
    if got_sha != pkt.sha256:
        raise ContractError(
            f"{where}: `packet_sha256` must be the sha256 of the packet you were "
            f"given, {pkt.sha256}. You wrote {got_sha or '(nothing)'}. Copy it "
            "from the header of this prompt rather than computing it yourself."
        )

    verdict = _enum(obj, "verdict", ASSESS_VERDICTS, where)

    claims = obj.get("claims")
    if not isinstance(claims, list) or not claims:
        raise ContractError(
            f"{where}: `claims` must be a non-empty list. A packet with no "
            "checkable assertion in it is not a packet that passed assessment — "
            "it is one nobody inventoried."
        )
    for i, claim in enumerate(claims):
        at = f"{where} claim #{i + 1}"
        if not isinstance(claim, dict):
            raise ContractError(f"{at} is not an object")
        if not str(claim.get("quote") or "").strip():
            raise ContractError(
                f"{at}: no `quote` — cite the packet's own words, so the ruling "
                "can be traced back to the sentence it is about"
            )
        _enum(claim, "kind", CLAIM_KINDS, at)
        status = _enum(claim, "status", INVARIANT_STATUS, at)
        if status != "unverifiable" and not str(claim.get("evidence") or "").strip():
            raise ContractError(
                f"{at}: {status!r} with no `evidence`. Give the path:line you "
                "opened or the command you ran — a ruling without a location is "
                "the impression this assessment exists to replace."
            )

    structural = obj.get("structural")
    if not isinstance(structural, list):
        raise ContractError(f"{where}: `structural` must be a list")
    seen: dict[str, str] = {}
    for i, check in enumerate(structural):
        at = f"{where} structural #{i + 1}"
        if not isinstance(check, dict):
            raise ContractError(f"{at} is not an object")
        name = _enum(check, "check", STRUCTURAL_CHECKS, at)
        if name in seen:
            raise ContractError(f"{where}: check {name!r} answered twice")
        _enum(check, "status", ("pass", "fail"), f"{where} check {name!r}")
        if not str(check.get("detail") or "").strip():
            raise ContractError(
                f"{where}: check {name!r} has no `detail` — name what you found, "
                "since a bare 'pass' is indistinguishable from a skipped check"
            )
        seen[name] = str(check["status"])

    missing = [c for c in STRUCTURAL_CHECKS if c not in seen]
    if missing:
        raise ContractError(
            f"{where}: every structural check must be answered. Missing: "
            f"{missing}. An omitted check is not a passed check."
        )

    findings = obj.get("findings", [])
    if not isinstance(findings, list):
        raise ContractError(f"{where}: `findings` must be a list (use [] for none)")
    for i, finding in enumerate(findings):
        at = f"{where} finding #{i + 1}"
        if not isinstance(finding, dict):
            raise ContractError(f"{at} is not an object")
        severity = _enum(finding, "severity", SEVERITIES, at)
        if not str(finding.get("claim") or "").strip():
            raise ContractError(f"{at}: no `claim` — say what the packet asserts")
        # A blocker or a major is an instruction to re-cut, so it has to say what
        # to write instead. A minor is an observation the next edit absorbs.
        if (
            severity in ("blocker", "major")
            and not str(finding.get("fix") or "").strip()
        ):
            raise ContractError(
                f"{at}: severity {severity!r} needs a `fix`. It will be handed to "
                "whoever re-cuts this packet as the instruction for doing so; "
                "'this is wrong' without 'write this instead' is not actionable."
            )

    return AssessResult(
        packet_id=pkt.id,
        packet_sha256=got_sha,
        verdict=verdict,
        claims=[
            {
                "quote": str(c["quote"]),
                "kind": str(c["kind"]),
                "status": str(c["status"]),
                "evidence": str(c.get("evidence", "")),
            }
            for c in claims
        ],
        structural=[
            {
                "check": str(c["check"]),
                "status": str(c["status"]),
                "detail": str(c.get("detail", "")),
            }
            for c in structural
        ],
        findings=findings,
        raw=obj,
    )


def effective_assessment(assess: AssessResult) -> tuple[str, str | None]:
    """What the harness does with an assessment, which is not always what it says.

    The same shape as `effective_verdict`, for the same reason and against the
    same incentive: the assessor is the party that has just done the work of
    reading the packet, and the cheapest way to finish is to call it `ready`.
    The three conditions below are stated in `assess.md` as rules the assessor
    must apply itself — enforcing them here as well is what makes them rules
    rather than requests.
    """
    if assess.verdict == "reject":
        return "reject", None

    failed = assess.failed_checks()
    if failed:
        return "recut", f"structural check(s) failed: {failed}"

    unsound = assess.unsound_invariants()
    if unsound:
        return "recut", (
            "invariant(s) not established: "
            + "; ".join(f"{c['status']} — {c['quote'][:60]}" for c in unsound)
        )

    blocking = assess.blocking_findings()
    if blocking:
        counts = f"{len(blocking)} blocker/major finding(s)"
        return "recut", f"{counts} despite verdict {assess.verdict!r}"

    return assess.verdict, None


@dataclass(frozen=True, slots=True)
class ConventionClaim:
    source: str
    quote: str
    kind: str
    status: str
    evidence: str
    proposed_trap: str = ""


def validate_conventions(
    obj: dict[str, Any], sources: list[str]
) -> list[ConventionClaim]:
    """What the project says about itself, ruled on one sentence at a time.

    `source` is checked against the documents the run was actually given. A
    ruling attributed to a file nobody opened cannot be traced back to a
    sentence, and the point of quoting is that someone can go and re-read it.
    """
    where = "conventions.json"
    claims = obj.get("claims")
    if not isinstance(claims, list) or not claims:
        raise ContractError(
            f"{where}: `claims` must be a non-empty list. A run that found "
            "nothing checkable is indistinguishable from one that did not look — "
            "say so with an `unverifiable` entry instead."
        )

    allowed = set(sources)
    out = []
    for i, claim in enumerate(claims):
        at = f"{where} claim #{i + 1}"
        if not isinstance(claim, dict):
            raise ContractError(f"{at} is not an object")
        source = str(claim.get("source") or "").strip()
        if source not in allowed:
            raise ContractError(
                f"{at}: `source` {source or '(missing)'!r} is not one of the "
                f"documents you were given ({', '.join(sorted(allowed)) or 'none'})"
            )
        quote = str(claim.get("quote") or "").strip()
        if not quote:
            raise ContractError(
                f"{at}: no `quote` — cite the document's own sentence, so the "
                "ruling can be traced back to the words it is about"
            )
        kind = _enum(claim, "kind", CONVENTION_KINDS, at)
        status = _enum(claim, "status", INVARIANT_STATUS, at)
        evidence = str(claim.get("evidence") or "").strip()
        if status != "unverifiable" and not evidence:
            raise ContractError(
                f"{at}: {status!r} with no `evidence`. Give the path:line you "
                "opened or the command you ran — the document is the thing under "
                "test, so quoting it back is not evidence that it is true."
            )
        out.append(
            ConventionClaim(
                source=source,
                quote=quote,
                kind=kind,
                status=status,
                evidence=evidence,
                proposed_trap=str(claim.get("proposed_trap") or "").strip(),
            )
        )
    return out
