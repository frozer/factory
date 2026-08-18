"""Mechanical checks on a packet, run before any model reads it.

These exist because model assessment is the expensive call in the loop and most
of its verdicts come back `recut` — but the majority of what those verdicts
*found* was one of three defect classes, none of which needs a model to detect:

1. a count stated twice, once wrong (an invariant `id` saying six where its own
   `assert` enumerates seven);
2. a `path:line` citation pointing at a file that does not exist, or past the
   end of one that does;
3. a quoted sentence attributed to a file that does not contain it — which sends
   an implementer looking for text that is not there.

Each is invisible from inside the sentence containing it and trivial to find
from outside. `prompts/cut.md` asks the cutter to re-read for all three; asking
is worth doing and is not worth the price of an assessment call, and a check that
runs in milliseconds can run on every sync forever.

**These checks are deliberately conservative.** A lint that cries wolf gets
passed `--skip`, so every rule here is written to stay silent unless it is sure:
each fires only on the narrow, unambiguous form of its defect and says nothing
about the broad form. Silence is not a pass — it means *this particular check
found nothing*, which is why `cut.md` keeps the human-readable instruction and
why the model assessment is retained for the packets where being wrong is
expensive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Number words a cutter actually writes into an invariant id or a heading. Digits
# are handled alongside. Stopping at twelve is not laziness: past that, prose
# switches to digits, and "one"/"two" appear so often as articles ("one of each",
# "two states") that including them would produce noise rather than findings.
NUMBER_WORDS = {
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
WORD_RE = re.compile(r"\b(" + "|".join(NUMBER_WORDS) + r")\b", re.IGNORECASE)

#: `path:line` or `path:a-b`. The path must contain a `/` and a `.`, which keeps
#: `§7.6` and bare `:59` out, and requires the citation to name a real file
#: shape rather than a section.
CITATION_RE = re.compile(r"`([A-Za-z0-9_./\-]+\.[A-Za-z0-9]+):(\d+)(?:-(\d+))?`")

#: A quoted run of prose long enough that its presence in a file is a fact rather
#: than a coincidence. Five words is the floor: shorter quotes are idioms that
#: recur, and matching them produces false alarms.
QUOTE_RE = re.compile(r"[\"“]([^\"“”]{25,400})[\"”]")

MIN_QUOTE_WORDS = 5


@dataclass(frozen=True)
class Finding:
    check: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.check}: {self.detail}"


def _numbers_in(text: str) -> set[int]:
    """Every count *word* in *text*, as integers.

    Digits are deliberately excluded. An invariant id is written in words, and
    its assert mixes words with digits that are line numbers, versions, status
    codes and column widths — `:59`, `404`, `120`. Including digits here made
    every assert disagree with every id and produced nothing but noise.
    """
    return {NUMBER_WORDS[m.group(1).lower()] for m in WORD_RE.finditer(text)}


def check_restated_counts(pkt) -> list[Finding]:
    """Defect 1: an invariant whose `id` and `assert` state different counts.

    The shape it catches: an id saying `...the-six-attributes-provisioning-writes`
    over an assert that enumerates seven and says "seven attributes, no more". The
    assert is the right half far more often than not — it is the one written while
    looking at the file.

    Fires only when both halves contain count words and the two sets are
    disjoint. Overlap is treated as agreement, because an assert legitimately
    mentions several counts ("three grounds", "seven attributes") and only one
    of them is the id's subject.
    """
    out: list[Finding] = []
    for inv in pkt.invariants:
        in_id = _numbers_in(inv.id)
        in_assert = _numbers_in(inv.assertion)
        if in_id and in_assert and not (in_id & in_assert):
            out.append(
                Finding(
                    "restated-count",
                    f"invariant `{inv.id}` states {sorted(in_id)} in its id and "
                    f"{sorted(in_assert)} in its assert — they do not agree, and the "
                    f"assert is usually the correct half",
                )
            )
    return out


def resolve(rel: str, repo: Path) -> Path | None:
    """Find the file a citation means, or `None` if it names nothing.

    A packet cites the same file three ways and all three are legitimate. It
    gives the full path once (`api/tests/conftest.py:114-134`), then shortens to
    a basename for the rest of the section (`conftest.py:108`). And it cites
    third-party code by import path (`fastapi/routing.py:1193`), which lives
    under a virtualenv nobody checks in.

    So: try repo-relative, then the virtualenvs, then a unique suffix match
    anywhere in the tree. **A basename matching two files resolves to neither** —
    an ambiguous citation is not a dead one, and guessing which file was meant is
    how a checker invents a finding.
    """
    direct = repo / rel
    if direct.is_file():
        return direct
    for venv in repo.glob("*/.venv/Lib/site-packages"):
        cand = venv / rel
        if cand.is_file():
            return cand
    matches = [
        p
        for p in repo.glob(f"**/{rel}")
        if p.is_file() and ".venv" not in p.parts and ".git" not in p.parts
    ]
    return matches[0] if len(matches) == 1 else None


def check_citations_resolve(pkt, repo: Path) -> list[Finding]:
    """Defect 2: a `path:line` citation whose line is not in the file.

    Only the unambiguous half is checked — the line exists. A range whose
    *content* is wrong (a swapped pair of labels that makes a seven-attribute
    count point at a range holding six) is beyond a checker and stays with the
    reader.

    A citation that resolves to nothing is **not** reported. It is far more often
    a file the packet is about to create, an ambiguous basename, or a path in a
    dependency this checkout does not have than it is a real defect, and a rule
    whose findings are mostly noise is a rule that gets skipped.
    """
    out: list[Finding] = []
    seen: set[tuple[str, int, int | None]] = set()
    for m in CITATION_RE.finditer(pkt.body):
        rel, start = m.group(1), int(m.group(2))
        end = int(m.group(3)) if m.group(3) else None
        if (rel, start, end) in seen:
            continue
        seen.add((rel, start, end))

        if end is not None and end < start:
            out.append(
                Finding("dead-citation", f"`{rel}:{start}-{end}` is an inverted range")
            )
            continue

        target = resolve(rel, repo)
        if target is None:
            continue
        try:
            total = sum(1 for _ in target.open("r", encoding="utf-8", errors="replace"))
        except OSError:  # pragma: no cover - unreadable file
            continue
        hi = end or start
        if hi > total:
            shown = f"{start}-{end}" if end else str(start)
            out.append(
                Finding(
                    "dead-citation",
                    f"`{rel}:{shown}` runs past the end of "
                    f"{target.relative_to(repo).as_posix()}, which has {total} lines",
                )
            )
    return out


def check_quoted_strings(pkt, repo: Path) -> list[Finding]:
    """Defect 3: a quotation attributed to a file that does not contain it.

    The shape it catches: a packet telling its implementer to delete the sentence
    "Nothing in this module writes" from a file that has never contained it, and
    making a *Definition of done* line turn on the docstring "no longer" saying
    it — a line no diff could fail, satisfied by an untouched file.

    Conservative on five axes. **Fenced blocks are skipped entirely** — a packet
    dictates the code and docstrings an implementer is to write, and dictated text
    is not a quotation of anything; reading it as one was this check's first and
    loudest false positive. Files the packet creates are skipped, since a quote
    from a file that does not exist yet cannot be checked. Only quotes on a line
    carrying a `path:line` citation are considered, so the file is named rather
    than guessed. Only quotes of at least five words count. And a quote is
    accepted if it appears in *any* file cited on that line, since one sentence
    often quotes a spec and a module together.
    """
    out: list[Finding] = []
    cache: dict[str, str | None] = {}
    creating = {Path(p).name for p in pkt.create_paths}

    def norm(s: str) -> str:
        # Whitespace and the two dash conventions differ freely between a packet
        # and the file it quotes; neither difference makes the quote false.
        return (
            re.sub(r"\s+", " ", s.replace("--", "—").replace("``", '"')).strip().lower()
        )

    def body_of(rel: str) -> str | None:
        if rel not in cache:
            p = resolve(rel, repo)
            try:
                cache[rel] = (
                    norm(p.read_text(encoding="utf-8", errors="replace")) if p else None
                )
            except OSError:
                cache[rel] = None
        return cache[rel]

    fenced = False
    for line in pkt.body.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        cites = [m.group(1) for m in CITATION_RE.finditer(line)]
        readable = [
            r for r in cites if Path(r).name not in creating and body_of(r) is not None
        ]
        if not readable:
            continue
        for qm in QUOTE_RE.finditer(line):
            quote = qm.group(1).strip()
            if len(quote.split()) < MIN_QUOTE_WORDS:
                continue
            # An ellipsis means the cutter elided text on purpose; the two halves
            # are separately findable and the joined string never appears.
            if "…" in quote or "..." in quote:
                continue
            needle = norm(quote)
            if not any(needle in body_of(r) for r in readable):  # type: ignore[operator]
                out.append(
                    Finding(
                        "unattributed-quote",
                        f'"{quote[:70]}…" is quoted beside '
                        f"{', '.join(f'`{c}`' for c in readable)} and appears in none of them",
                    )
                )
    return out


#: "`Widget`, `widget_read` — both already imported ... by `B03`/`B04`". The
#: symbols are the backticked run before the phrase; the packets are the backticked
#: ids after it. Bounded so the two halves cannot span unrelated sentences.
INHERITED_IMPORT_RE = re.compile(
    r"((?:`[A-Za-z_][A-Za-z0-9_]*`[,/\s\w]{0,24}){1,8}?)"
    r"(?:is|are)?\s*(?:both\s+|all\s+)?already\s+(?:imported|bound)"
    r"(?![^.]{0,80}\bor\s+declared\b)"
    r"[^.]{0,120}?\bby\s+((?:`?[A-Z]\d{2}`?(?:'s)?[/,\s]{0,6}){1,4})",
    re.IGNORECASE,
)
SYMBOL_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")
#: Backticks optional: a packet can write "already imported ... by B01's contract"
#: bare, and requiring them is what made this rule miss the defect it was written for.
PACKET_ID_RE = re.compile(r"\b([A-Z]\d{2})\b")
TOKEN_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")


def check_inherited_imports(pkt, repo: Path) -> list[Finding]:
    """"`X` is already imported by `B04`" — when `B04` imports no such name.

    Two shapes it was written for. A packet makes `StoredValue` its own module's
    type on the grounds that a predecessor's contract had imported it, when that
    predecessor imported the module objects instead. Another makes `Widget` its
    route's `response_model` on the grounds that two predecessors had imported it,
    when between them they import other names entirely. In both the claim is not
    merely unverified but *impossible*: an import no later line uses is `F401`,
    and every packet in this family runs `ruff check`, so a predecessor could not
    have left the name bound even if it had wanted to.

    That makes it partly mechanical, and only partly — which is the whole design
    of this rule. **It fires only when the claimed symbol appears nowhere at all
    in the named packet**, as a token, in prose or code alike.

    Deliberately not an import parser. Three attempts at one all produced false
    positives: names like `widget_read` and `PublishConflict` arrive on
    continuation lines inside `from x import (...)`, which a line-oriented regex
    cannot see, and a name like `Annotated` is added by a *later step* of a
    predecessor described in prose rather than appearing in its dictated block at
    all. Each of those is a true inheritance that a stricter reading would have
    flagged, and three false alarms is how a lint gets switched off.

    So this takes the unambiguous half and leaves the rest to the model. It
    catches a `StoredValue` absent from the named predecessor entirely. It does
    *not* catch a `Widget` that appears in one predecessor's prose while being
    imported by neither — that one needs a reader, and `deps-complete` found it.
    Silence here is not a pass; it is this check finding nothing.

    Conservative on two further axes: the claim is skipped when phrased "imported
    **or declared**", since that form is true of a name a predecessor defined,
    and nothing is said when a named packet is absent from disk, since the claim
    may concern one already merged.
    """
    out: list[Finding] = []
    text = pkt.body
    if not text:
        return out
    contracts: dict[str, str] = {}
    for claim, ids in INHERITED_IMPORT_RE.findall(text):
        symbols = SYMBOL_RE.findall(claim)
        packet_ids = PACKET_ID_RE.findall(ids)
        if not symbols or not packet_ids:
            continue
        missing_from: list[str] = []
        for pid in packet_ids:
            if pid not in contracts:
                found = sorted(Path(repo, "tasks").glob(f"{pid}-*.md"))
                contracts[pid] = (
                    found[0].read_text(encoding="utf-8", errors="replace")
                    if found
                    else ""
                )
            if contracts[pid]:
                missing_from.append(pid)
        if not missing_from:
            continue  # every named packet is absent: it may already be merged
        present: set[str] = set()
        for pid in missing_from:
            present.update(TOKEN_RE.findall(contracts[pid]))
        for sym in symbols:
            if sym not in present:
                out.append(
                    Finding(
                        "inherited-import-absent",
                        f"`{sym}` is said to be already imported by "
                        f"{', '.join(f'`{p}`' for p in missing_from)}, which "
                        f"never mention the name at all — and could not have left "
                        f"it bound, since an unused import is F401 and those "
                        f"packets run `ruff check`",
                    )
                )
    return out


def lint(pkt, repo: Path) -> list[Finding]:
    """Every mechanical check, cheapest first."""
    return (
        check_restated_counts(pkt)
        + check_citations_resolve(pkt, repo)
        + check_quoted_strings(pkt, repo)
        + check_inherited_imports(pkt, repo)
    )
