Nine ways a packet fails, each one already paid for. The first five are **claims
that are not true**, the last four are **packets that cannot be satisfied**. A
model working inside the worktree can discover none of them, which is why they
fail identically on every attempt rather than degrading gracefully.

Three carry a worked example, because those turn on something a careful reader
would still get wrong. The rest state the defect, which is enough to check for it.

## False claims

**`phantom-artifact`** — the packet names a file, directory, filename, encoding,
delimiter, column or join key that does not exist as described.

> Three packets in one inventory described a directory layout, a JSON envelope, a
> filename and a "decimal comma" that existed nowhere. All three burned every
> attempt. One of them, once the real files were opened, held two files with **no
> usable join key at all** and two rows per logical record — neither knowable from
> the spec, both decisive for the loader that was supposed to read them.

**`unexpressible-sequence`** — a step sequence that reads perfectly and cannot be
expressed through the API that actually exists, almost always because of a side
effect nothing documents. The most expensive class here, because the model keeps
producing something that looks correct.

> A packet specified `build → validate → write`. The "build" call persisted
> internally before it returned, so build had already written. Three attempts
> validated *after* the write and logged the failure instead of preventing it —
> exactly the bug the invariant existed to stop.

**`type-disagreement`** — an artifact produced by an earlier packet has a
different type than this one assumes. Dependencies are not an ordering hint; they
are artifacts with types, and the migration, dataclass or config module that owns
one is the thing that decides it.

**`toolchain-claim`** — an assertion about what the linter or formatter wants
that is not what the tool does when run from where the gate runs it. Run it.

**`adjective-for-a-measurement`** — a quantity stated as a word when it could
have been counted. The packet reads as though it was checked and was not.

> Weak, and it failed: "`..` loads as NULL, never 0. It is the source's no-data
> marker and, at the left edge of a series, it means the municipality did not
> exist yet."
>
> Strong, and it passed first attempt: the same sentence, plus "in all five value
> columns … verified: exactly 5 municipalities, 36 rows, all at the series'
> start, 180 `..` cells total."
>
> The second is not more rigorous prose. It is a claim that was **checked**, and
> the checking is what found that two of the three files in that directory were
> unloadable.

## Packets that cannot be satisfied

**`unsatisfiable-invariant`** — the file whose contents would decide an invariant
is not in the packet's *Files you may CREATE* or *Files you may EDIT*. No diff the
implementer is permitted to produce can make it hold, so the attempts go on
producing the same rejected fix until they run out.

**`unwinnable-fixture`** — a planted test is three things at once: the
specification, immutable, and part of the diff the linter scans. So a lint
violation inside one is unwinnable — the model can neither fix it nor pass with
it — and so is any instruction that leads it to rewrite the file. Three packets
shipped the first (an unsorted import block, a bare `pytest.raises(Exception)`
tripping B017, a stray blank line); one omitted the instruction not to resave,
and three attempts then produced correct, green code that failed the gate
identically each time on a **whitespace-only resave**.

**`optimistic-tier`** — the tier was assigned from how much code the task looks
like, rather than from how much of the world the implementer has to resolve while
writing it. Of the packets re-cut after exhausting their attempts, three of four
were bottom-tier by a size judgement and correct one tier up — measured when
`basic` ran haiku and `standard` ran sonnet, so treat the shape as durable and
the ratio as dated.

**`relayed-ambiguity`** — the spec contradicts itself, or contradicts the code,
and the packet passes the contradiction through. The implementer has no budget to
adjudicate a design question and no access to the document that raised it, so an
ambiguity relayed is an ambiguity resolved at random. Name both readings, compute
the number that distinguishes them, pick one, and tell the implementer to leave a
trace in the docstring for the next reader.
