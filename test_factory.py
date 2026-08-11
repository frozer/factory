"""Tests for the harness itself.

The factory decides what merges. If its guards are wrong, every downstream gate
is decoration — so the cases here are the ones the design exists for: a reviewer
that omits an invariant, a reviewer that accepts over a red gate, a queue that
hands out a task whose dependency has not merged.

Stdlib `unittest`, no venv:

    python -m unittest discover -s factory -t factory
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import textwrap
import types
import unittest
import unittest.mock
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent  # noqa: E402
import contracts  # noqa: E402
import gates  # noqa: E402
import gitops  # noqa: E402
import ground  # noqa: E402
import packet as packet_module  # noqa: E402
import run  # noqa: E402
from db import Factory  # noqa: E402
from packet import (  # noqa: E402
    REVIEWER_LADDER,
    TIERS,
    PacketError,
    is_packet_filename,
    load_all,
    parse,
)

VALID = """\
+++
id = "B01"
slug = "example"
goal = "do the thing"
tier = "basic"
spec_commit = "abc1234"
spec_path = "docs/SPEC.md"

[[invariants]]
id = "one"
assert = "the thing is done"
+++

Body.
"""


def write(dirpath: Path, name: str, text: str) -> Path:
    target = dirpath / name
    target.write_text(textwrap.dedent(text), encoding="utf-8")
    return target


class PacketValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_a_valid_packet_parses(self) -> None:
        pkt = parse(write(self.dir, "B01-example.md", VALID))
        self.assertEqual(pkt.id, "B01")
        self.assertEqual(pkt.tier, "basic")
        # The ladder: a `basic` implementer is never reviewed at `basic`.
        self.assertEqual(pkt.reviewer, "standard")
        self.assertEqual([i.id for i in pkt.invariants], ["one"])
        self.assertTrue(pkt.invariants[0].critical, "invariants default to critical")

    def test_a_packet_without_invariants_is_refused(self) -> None:
        text = VALID.split("[[invariants]]")[0] + "+++\n\nBody.\n"
        with self.assertRaises(PacketError) as exc:
            parse(write(self.dir, "B01-example.md", text))
        self.assertIn("invariants", str(exc.exception))

    def test_a_reviewer_weaker_than_the_implementer_is_refused(self) -> None:
        text = VALID.replace('tier = "basic"', 'tier = "standard"\nreviewer = "basic"')
        with self.assertRaises(PacketError) as exc:
            parse(write(self.dir, "B01-example.md", text))
        self.assertIn("weaker", str(exc.exception))

    def test_a_packet_naming_a_model_is_refused(self) -> None:
        """`model = "haiku"` was the old spelling, and is rejected rather than
        translated — a packet does not get to choose which model runs it, and a
        silent alias would keep the old spelling alive in every packet cut from
        an older example."""
        text = VALID.replace('tier = "basic"', 'model = "haiku"')
        with self.assertRaises(PacketError) as exc:
            parse(write(self.dir, "B01-example.md", text))
        self.assertIn("tier", str(exc.exception))

    def test_a_filename_that_does_not_match_the_id_is_refused(self) -> None:
        with self.assertRaises(PacketError):
            parse(write(self.dir, "B02-example.md", VALID))

    def test_duplicate_invariant_ids_are_refused(self) -> None:
        text = VALID + '\n[[invariants]]\nid = "one"\nassert = "again"\n'
        # The second block has to sit inside the frontmatter to count.
        text = VALID.replace(
            "+++\n\nBody.",
            '[[invariants]]\nid = "one"\nassert = "again"\n+++\n\nBody.',
        )
        with self.assertRaises(PacketError) as exc:
            parse(write(self.dir, "B01-example.md", text))
        self.assertIn("duplicate", str(exc.exception))

    def test_a_dependency_on_a_packet_that_does_not_exist_is_refused(self) -> None:
        write(
            self.dir,
            "B01-example.md",
            VALID.replace(
                'spec_path = "docs/SPEC.md"',
                'spec_path = "docs/SPEC.md"\nrequires = ["Z99"]',
            ),
        )
        with self.assertRaises(PacketError) as exc:
            load_all(self.dir)
        self.assertIn("Z99", str(exc.exception))

    def test_missing_spec_commit_is_refused(self) -> None:
        with self.assertRaises(PacketError):
            parse(
                write(
                    self.dir,
                    "B01-example.md",
                    VALID.replace('spec_commit = "abc1234"\n', ""),
                )
            )

    def test_deletable_paths_default_to_empty(self) -> None:
        pkt = parse(write(self.dir, "B01-example.md", VALID))
        self.assertEqual(pkt.deletable_paths, [])

    def test_deletable_paths_parse(self) -> None:
        text = VALID.replace(
            'spec_path = "docs/SPEC.md"',
            'spec_path = "docs/SPEC.md"\ndeletable_paths = ["api/old_route.py"]',
        )
        pkt = parse(write(self.dir, "B01-example.md", text))
        self.assertEqual(pkt.deletable_paths, ["api/old_route.py"])

    def test_a_path_both_forbidden_and_deletable_is_refused(self) -> None:
        text = VALID.replace(
            'spec_path = "docs/SPEC.md"',
            'spec_path = "docs/SPEC.md"\n'
            'forbidden_paths = ["api/old_route.py"]\n'
            'deletable_paths = ["api/old_route.py"]',
        )
        with self.assertRaises(PacketError) as exc:
            parse(write(self.dir, "B01-example.md", text))
        self.assertIn("api/old_route.py", str(exc.exception))


class ImplContract(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.pkt = parse(
            write(
                self.dir,
                "B01-example.md",
                VALID.replace(
                    'spec_path = "docs/SPEC.md"',
                    'spec_path = "docs/SPEC.md"\n'
                    'deletable_paths = ["api/old_route.py"]',
                ),
            )
        )

    def _obj(self, **overrides):
        base = {
            "task_id": "B01",
            "status": "complete",
            "verification": {"command": "uv run pytest -q", "passed": 1, "failed": 0},
        }
        base.update(overrides)
        return base

    def test_files_deleted_defaults_to_empty(self) -> None:
        result = contracts.validate_impl(self._obj(), self.pkt)
        self.assertEqual(result.files_deleted, [])

    def test_an_authorized_deletion_is_accepted(self) -> None:
        result = contracts.validate_impl(
            self._obj(files_deleted=["api/old_route.py"]), self.pkt
        )
        self.assertEqual(result.files_deleted, ["api/old_route.py"])

    def test_an_undeclared_deletion_is_rejected(self) -> None:
        with self.assertRaises(contracts.ContractError) as exc:
            contracts.validate_impl(
                self._obj(files_deleted=["api/some_other_file.py"]), self.pkt
            )
        self.assertIn("api/some_other_file.py", str(exc.exception))


class ReviewContract(unittest.TestCase):
    """The review is the only gate on the failures tests cannot see."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        text = VALID.replace(
            '[[invariants]]\nid = "one"\nassert = "the thing is done"',
            '[[invariants]]\nid = "one"\nassert = "a"\n\n'
            '[[invariants]]\nid = "two"\ncritical = false\nassert = "b"',
        )
        self.pkt = parse(write(Path(self.tmp.name), "B01-example.md", text))

    def _review(self, **over):
        base = {
            "task_id": "B01",
            "verdict": "accept",
            "invariants": [
                {"id": "one", "status": "held", "evidence": "a.py:1"},
                {"id": "two", "status": "held", "evidence": "a.py:2"},
            ],
            "findings": [],
            "merge_ok": True,
        }
        base.update(over)
        return base

    def test_omitting_an_invariant_is_rejected(self) -> None:
        """The whole point: silence is not a pass."""
        obj = self._review(
            invariants=[{"id": "one", "status": "held", "evidence": "a.py:1"}]
        )
        with self.assertRaises(contracts.ContractError) as exc:
            contracts.validate_review(obj, self.pkt)
        self.assertIn("two", str(exc.exception))

    def test_a_verdict_without_evidence_is_rejected(self) -> None:
        obj = self._review(
            invariants=[
                {"id": "one", "status": "violated"},
                {"id": "two", "status": "held", "evidence": "a.py:2"},
            ]
        )
        with self.assertRaises(contracts.ContractError) as exc:
            contracts.validate_review(obj, self.pkt)
        self.assertIn("evidence", str(exc.exception))

    def test_unverifiable_needs_no_evidence(self) -> None:
        obj = self._review(
            invariants=[
                {"id": "one", "status": "unverifiable"},
                {"id": "two", "status": "held", "evidence": "a.py:2"},
            ]
        )
        contracts.validate_review(obj, self.pkt)  # does not raise

    def test_an_unknown_invariant_id_is_rejected(self) -> None:
        obj = self._review(
            invariants=[
                {"id": "one", "status": "held", "evidence": "a.py:1"},
                {"id": "two", "status": "held", "evidence": "a.py:2"},
                {"id": "three", "status": "held", "evidence": "a.py:3"},
            ]
        )
        with self.assertRaises(contracts.ContractError):
            contracts.validate_review(obj, self.pkt)


class HarnessOverridesTheReviewer(unittest.TestCase):
    """A reviewer can be talked into `accept`; the harness cannot."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        text = VALID.replace(
            '[[invariants]]\nid = "one"\nassert = "the thing is done"',
            '[[invariants]]\nid = "one"\nassert = "a"\n\n'
            '[[invariants]]\nid = "two"\ncritical = false\nassert = "b"',
        )
        self.pkt = parse(write(Path(self.tmp.name), "B01-example.md", text))

    def _accepted(self, invariants=None, findings=None):
        return contracts.validate_review(
            {
                "task_id": "B01",
                "verdict": "accept",
                "invariants": invariants
                or [
                    {"id": "one", "status": "held", "evidence": "a.py:1"},
                    {"id": "two", "status": "held", "evidence": "a.py:2"},
                ],
                "findings": findings or [],
                "merge_ok": True,
            },
            self.pkt,
        )

    def test_accept_over_a_red_gate_is_downgraded(self) -> None:
        verdict, why = contracts.effective_verdict(
            self._accepted(), gate_status="red", pkt=self.pkt
        )
        self.assertEqual(verdict, "revise")
        self.assertIn("red", why)

    def test_accept_with_a_violated_critical_invariant_is_downgraded(self) -> None:
        review = self._accepted(
            invariants=[
                {"id": "one", "status": "violated", "evidence": "a.py:1"},
                {"id": "two", "status": "held", "evidence": "a.py:2"},
            ]
        )
        verdict, why = contracts.effective_verdict(
            review, gate_status="green", pkt=self.pkt
        )
        self.assertEqual(verdict, "revise")
        self.assertIn("one", why)

    def test_a_violated_non_critical_invariant_still_merges(self) -> None:
        review = self._accepted(
            invariants=[
                {"id": "one", "status": "held", "evidence": "a.py:1"},
                {"id": "two", "status": "violated", "evidence": "a.py:2"},
            ]
        )
        verdict, _ = contracts.effective_verdict(
            review, gate_status="green", pkt=self.pkt
        )
        self.assertEqual(verdict, "accept")

    def test_an_unverifiable_critical_invariant_blocks(self) -> None:
        """Guessing `held` is the failure this prevents; `unverifiable` must
        therefore cost something, or it becomes the safe non-answer."""
        review = self._accepted(
            invariants=[
                {"id": "one", "status": "unverifiable"},
                {"id": "two", "status": "held", "evidence": "a.py:2"},
            ]
        )
        verdict, why = contracts.effective_verdict(
            review, gate_status="green", pkt=self.pkt
        )
        self.assertEqual(verdict, "revise")
        self.assertIn("one", why)

    def test_a_blocker_finding_beats_an_accept_verdict(self) -> None:
        review = self._accepted(
            findings=[
                {"severity": "blocker", "file": "a.py", "line": 3, "claim": "wrong"}
            ]
        )
        verdict, why = contracts.effective_verdict(
            review, gate_status="green", pkt=self.pkt
        )
        self.assertEqual(verdict, "revise")
        self.assertIn("blocker", why)

    def test_a_clean_accept_merges(self) -> None:
        verdict, why = contracts.effective_verdict(
            self._accepted(), gate_status="green", pkt=self.pkt
        )
        self.assertEqual(verdict, "accept")
        self.assertIsNone(why)


class Gates(unittest.TestCase):
    def test_pytest_summary_is_parsed(self) -> None:
        counts = gates.parse_pytest("81 passed, 19 deselected in 0.51s")
        self.assertEqual(counts["passed"], 81)
        self.assertEqual(counts["deselected"], 19)

    def test_failures_are_parsed(self) -> None:
        counts = gates.parse_pytest("2 failed, 79 passed, 19 deselected in 1.2s")
        self.assertEqual(counts["failed"], 2)
        self.assertEqual(counts["passed"], 79)

    def test_a_collection_error_is_the_work_s_fault_not_the_box_s(self) -> None:
        """The most common state of a task an agent got wrong.

        A planted test that cannot import the module the task was meant to
        create exits pytest with 2 — "interrupted", not "failed". Scoring that
        as an environment fault would halt the whole queue after two bad tasks.
        """
        output = (
            "ImportError: cannot import name 'ReferenceSource'\n"
            "!!!! Interrupted: 1 error during collection !!!!"
        )
        self.assertEqual(gates._pytest_verdict(2, output), "fail")

    def test_a_genuine_interrupt_is_an_environment_fault(self) -> None:
        self.assertEqual(gates._pytest_verdict(2, "KeyboardInterrupt"), "error")

    def test_usage_and_internal_errors_are_environment_faults(self) -> None:
        self.assertEqual(gates._pytest_verdict(4, "unrecognized arguments"), "error")
        self.assertEqual(gates._pytest_verdict(3, "INTERNALERROR"), "error")

    def test_an_empty_suite_is_a_failure_not_a_pass(self) -> None:
        self.assertEqual(gates._pytest_verdict(5, "no tests ran"), "fail")

    def test_editing_a_forbidden_path_fails_the_gate(self) -> None:
        check = gates.check_untouched(["api/tests/test_vintage.py"])
        self.assertEqual(check.status, "fail")
        self.assertIn("test_vintage.py", check.output_tail)

    def test_touching_nothing_forbidden_passes(self) -> None:
        self.assertEqual(gates.check_untouched([]).status, "pass")


class InstallDeps(unittest.TestCase):
    """A fresh worktree never inherits `.venv`/`node_modules` — both are
    gitignored — so this is what stands between the implementer and the
    `` `uv` is not on PATH `` failure that burned A05 and A06."""

    def test_api_surface_runs_uv_sync(self) -> None:
        with (
            unittest.mock.patch("gitops.shutil.which", return_value="/usr/bin/uv"),
            unittest.mock.patch("gitops.subprocess.run") as run_mock,
        ):
            run_mock.return_value = unittest.mock.Mock(
                returncode=0, stdout="", stderr=""
            )
            gitops.install_deps(Path("/wt"), "api")
            args, kwargs = run_mock.call_args
            self.assertEqual(args[0], ["/usr/bin/uv", "sync"])
            self.assertEqual(kwargs["cwd"], Path("/wt/api"))

    def test_webapp_surface_runs_npm_ci(self) -> None:
        with (
            unittest.mock.patch("gitops.shutil.which", return_value="/usr/bin/npm"),
            unittest.mock.patch("gitops.subprocess.run") as run_mock,
        ):
            run_mock.return_value = unittest.mock.Mock(
                returncode=0, stdout="", stderr=""
            )
            gitops.install_deps(Path("/wt"), "webapp")
            args, kwargs = run_mock.call_args
            self.assertEqual(args[0], ["/usr/bin/npm", "ci"])
            self.assertEqual(kwargs["cwd"], Path("/wt/webapp"))

    def test_missing_tool_raises_deps_error(self) -> None:
        with unittest.mock.patch("gitops.shutil.which", return_value=None):
            with self.assertRaises(gitops.DepsError):
                gitops.install_deps(Path("/wt"), "api")

    def test_nonzero_exit_raises_deps_error(self) -> None:
        with (
            unittest.mock.patch("gitops.shutil.which", return_value="/usr/bin/npm"),
            unittest.mock.patch("gitops.subprocess.run") as run_mock,
        ):
            run_mock.return_value = unittest.mock.Mock(
                returncode=1, stdout="", stderr="EACCES"
            )
            with self.assertRaises(gitops.DepsError):
                gitops.install_deps(Path("/wt"), "webapp")


class WorkerEnvironment(unittest.TestCase):
    """How the worker authenticates has to be a decision, not an inheritance."""

    def test_api_billing_keeps_the_key(self) -> None:
        with unittest.mock.patch.dict(
            os.environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=False
        ):
            self.assertEqual(agent.child_env("api").get("ANTHROPIC_API_KEY"), "sk-test")

    def test_subscription_billing_scrubs_the_key(self) -> None:
        with unittest.mock.patch.dict(
            os.environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=False
        ):
            self.assertNotIn("ANTHROPIC_API_KEY", agent.child_env("subscription"))

    def test_the_parent_session_is_never_inherited(self) -> None:
        """A worker in a disposable worktree is not a continuation of the
        session that launched it."""
        with unittest.mock.patch.dict(
            os.environ, {"CLAUDE_CODE_SESSION_ID": "parent"}, clear=False
        ):
            for billing in ("api", "subscription"):
                self.assertNotIn("CLAUDE_CODE_SESSION_ID", agent.child_env(billing))


# The literal fault B07 hit three times running: every leg of every attempt
# reported this same shape, and it was silently treated as a bad model
# result instead of an environment fault. See .factory/runs/B07/attempt-1/
# implement.stdout.json for the raw fixture this was taken from.
RATE_LIMIT_PAYLOAD = {
    "is_error": True,
    "api_error_status": 429,
    "terminal_reason": "api_error",
    "result": "You've hit your session limit · resets 7pm (America/New_York)",
    "duration_ms": 500,
}


def _success_payload(**overrides):
    base = {
        "is_error": False,
        "session_id": "sess-1",
        "total_cost_usd": 0.01,
        "duration_ms": 1000,
        "num_turns": 3,
        "result": "done",
    }
    base.update(overrides)
    return base


class RateLimitClassification(unittest.TestCase):
    """A 429/API-level fault is an environment fault, not a bad model
    result — this is what B07 needed and did not get."""

    def test_the_b07_shape_is_a_rate_limit(self) -> None:
        self.assertTrue(agent._is_rate_limit(429, "api_error"))

    def test_a_bare_5xx_is_a_rate_limit(self) -> None:
        self.assertTrue(agent._is_rate_limit(503, None))

    def test_neither_signal_set_is_not_a_rate_limit(self) -> None:
        """Covers both a normal success and a validation-style `is_error`
        with no infra signal — `_is_rate_limit` only sees status/reason, so
        either case must not be swept into the infra-fault bucket."""
        self.assertFalse(agent._is_rate_limit(None, None))


class ResetHintParsing(unittest.TestCase):
    """Free text from the CLI, not a contract — best-effort only, and every
    case that doesn't match cleanly must fall back to `None`."""

    def test_same_day_not_yet_passed(self) -> None:
        tz = ZoneInfo("America/New_York")
        now_local = datetime(2026, 8, 9, 14, 0, tzinfo=tz)
        got = agent._parse_reset_hint(
            "You've hit your session limit · resets 7pm (America/New_York)",
            now=now_local.astimezone(UTC),
        )
        expected = now_local.replace(hour=19, minute=0, second=0, microsecond=0)
        self.assertEqual(got, expected.astimezone(UTC))

    def test_already_passed_today_rolls_to_tomorrow(self) -> None:
        tz = ZoneInfo("America/New_York")
        now_local = datetime(2026, 8, 9, 20, 0, tzinfo=tz)
        got = agent._parse_reset_hint(
            "resets 7pm (America/New_York)", now=now_local.astimezone(UTC)
        )
        expected = now_local.replace(
            hour=19, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
        self.assertEqual(got, expected.astimezone(UTC))

    def test_midnight_and_noon(self) -> None:
        tz = ZoneInfo("America/New_York")
        now_local = datetime(2026, 8, 9, 20, 0, tzinfo=tz)
        got_midnight = agent._parse_reset_hint(
            "resets 12am (America/New_York)", now=now_local.astimezone(UTC)
        )
        expected_midnight = now_local.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
        self.assertEqual(got_midnight, expected_midnight.astimezone(UTC))

        now_local = datetime(2026, 8, 9, 8, 0, tzinfo=tz)
        got_noon = agent._parse_reset_hint(
            "resets 12pm (America/New_York)", now=now_local.astimezone(UTC)
        )
        expected_noon = now_local.replace(hour=12, minute=0, second=0, microsecond=0)
        self.assertEqual(got_noon, expected_noon.astimezone(UTC))

    def test_minutes_are_parsed(self) -> None:
        tz = ZoneInfo("America/New_York")
        now_local = datetime(2026, 8, 9, 14, 0, tzinfo=tz)
        got = agent._parse_reset_hint(
            "resets 7:30pm (America/New_York)", now=now_local.astimezone(UTC)
        )
        expected = now_local.replace(hour=19, minute=30, second=0, microsecond=0)
        self.assertEqual(got, expected.astimezone(UTC))

    def test_unrecognized_text_falls_back_to_none(self) -> None:
        self.assertIsNone(
            agent._parse_reset_hint(
                "the server is having a bad day", now=datetime.now(UTC)
            )
        )

    def test_unknown_timezone_falls_back_to_none(self) -> None:
        self.assertIsNone(
            agent._parse_reset_hint("resets 7pm (Mars/Standard)", now=datetime.now(UTC))
        )


class _FakeClock:
    """An injectable `now_fn`/`sleep_fn` pair where sleeping advances the
    clock instead of the wall — so backoff scheduling is testable in
    milliseconds regardless of how many simulated hours it spans."""

    def __init__(self, start: datetime) -> None:
        self.now = start
        self.slept: list[float] = []

    def now_fn(self) -> datetime:
        return self.now

    def sleep_fn(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


class RateLimitBackoff(unittest.TestCase):
    """`run_resilient` waits out a rate limit instead of failing the
    attempt — bounded, so a persistent fault still surfaces."""

    def _mock_run(self, payload: dict) -> unittest.mock.Mock:
        return unittest.mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")

    def test_no_parseable_hint_backs_off_with_growing_capped_delays(self) -> None:
        clock = _FakeClock(datetime(2026, 8, 9, 12, 0, tzinfo=UTC))
        with (
            tempfile.TemporaryDirectory() as tmp,
            unittest.mock.patch(
                "agent.subprocess.run",
                return_value=self._mock_run(
                    {**RATE_LIMIT_PAYLOAD, "result": "no reset hint here"}
                ),
            ),
        ):
            with self.assertRaises(agent.RateLimited):
                agent.run_resilient(
                    worktree=Path(tmp),
                    prompt="p",
                    model="haiku",
                    allowed_tools="",
                    run_dir=Path(tmp),
                    label="implement",
                    max_wait_s=3600,
                    sleep_fn=clock.sleep_fn,
                    now_fn=clock.now_fn,
                )
        self.assertGreaterEqual(len(clock.slept), 2)
        self.assertEqual(clock.slept[0], agent._BACKOFF_START_S)
        self.assertEqual(clock.slept[1], agent._BACKOFF_START_S * agent._BACKOFF_FACTOR)
        self.assertTrue(all(s <= agent._BACKOFF_MAX_S for s in clock.slept))
        self.assertEqual(clock.slept, sorted(clock.slept))

    def test_exceeding_the_wait_cap_raises_a_rate_limited_agent_error(self) -> None:
        clock = _FakeClock(datetime(2026, 8, 9, 12, 0, tzinfo=UTC))
        with (
            tempfile.TemporaryDirectory() as tmp,
            unittest.mock.patch(
                "agent.subprocess.run",
                return_value=self._mock_run(
                    {**RATE_LIMIT_PAYLOAD, "result": "no reset hint here"}
                ),
            ),
        ):
            with self.assertRaises(agent.RateLimited) as exc:
                agent.run_resilient(
                    worktree=Path(tmp),
                    prompt="p",
                    model="haiku",
                    allowed_tools="",
                    run_dir=Path(tmp),
                    label="implement",
                    max_wait_s=120,
                    sleep_fn=clock.sleep_fn,
                    now_fn=clock.now_fn,
                )
        self.assertIsInstance(exc.exception, agent.AgentError)
        self.assertGreater(exc.exception.waited_s, 0)

    def test_a_parsed_hint_beyond_the_backoff_cap_still_waits(self) -> None:
        """C07's shape: a *weekly* limit reset ~13h out. `max_wait_s` (the
        backoff-only budget) is smaller than that gap, but a parsed reset
        hint is a fact from the CLI, not a guess — it must be judged against
        `max_parsed_wait_s` instead, and wait rather than give up."""
        clock = _FakeClock(datetime(2026, 8, 9, 12, 0, tzinfo=UTC))
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if len(calls) == 1:
                return self._mock_run(
                    {
                        **RATE_LIMIT_PAYLOAD,
                        "result": "You've hit your weekly limit · resets 1am (UTC)",
                    }
                )
            return self._mock_run(_success_payload())

        with (
            tempfile.TemporaryDirectory() as tmp,
            unittest.mock.patch("agent.subprocess.run", side_effect=fake_run),
        ):
            result = agent.run_resilient(
                worktree=Path(tmp),
                prompt="p",
                model="haiku",
                allowed_tools="",
                run_dir=Path(tmp),
                label="implement",
                max_wait_s=3600,  # 1h — far short of the ~13h gap to 1am UTC
                max_parsed_wait_s=8 * 24 * 3600,
                sleep_fn=clock.sleep_fn,
                now_fn=clock.now_fn,
            )
        self.assertTrue(result.ok)
        self.assertEqual(len(calls), 2)
        self.assertGreater(clock.slept[0], 3600)

    def test_a_parsed_hint_beyond_its_own_cap_still_raises(self) -> None:
        """The longer leash on parsed hints is not unlimited — a reset
        further out than `max_parsed_wait_s` must still raise, or a
        genuinely broken box with a garbage far-future hint would hang the
        loop forever."""
        clock = _FakeClock(datetime(2026, 8, 9, 12, 0, tzinfo=UTC))
        with (
            tempfile.TemporaryDirectory() as tmp,
            unittest.mock.patch(
                "agent.subprocess.run",
                return_value=self._mock_run(
                    {
                        **RATE_LIMIT_PAYLOAD,
                        "result": "You've hit your weekly limit · resets 1am (UTC)",
                    }
                ),
            ),
        ):
            with self.assertRaises(agent.RateLimited):
                agent.run_resilient(
                    worktree=Path(tmp),
                    prompt="p",
                    model="haiku",
                    allowed_tools="",
                    run_dir=Path(tmp),
                    label="implement",
                    max_wait_s=3600,
                    max_parsed_wait_s=3600,  # too short even for the parsed hint
                    sleep_fn=clock.sleep_fn,
                    now_fn=clock.now_fn,
                )

    def test_a_successful_retry_after_a_wait_returns_cleanly(self) -> None:
        clock = _FakeClock(datetime(2026, 8, 9, 12, 0, tzinfo=UTC))
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if len(calls) == 1:
                # No parseable reset hint here, so this exercises the plain
                # backoff path (30s) rather than the "resets 7pm" hint path,
                # which at a noon-UTC fake clock is an ~11h gap that would
                # itself exceed this test's max_wait_s — see the dedicated
                # cap-exceeded test below for that case.
                return self._mock_run({**RATE_LIMIT_PAYLOAD, "result": "retry later"})
            return self._mock_run(_success_payload())

        with (
            tempfile.TemporaryDirectory() as tmp,
            unittest.mock.patch("agent.subprocess.run", side_effect=fake_run),
        ):
            result = agent.run_resilient(
                worktree=Path(tmp),
                prompt="p",
                model="haiku",
                allowed_tools="",
                run_dir=Path(tmp),
                label="implement",
                max_wait_s=3600,
                sleep_fn=clock.sleep_fn,
                now_fn=clock.now_fn,
            )
        self.assertTrue(result.ok)
        self.assertEqual(len(calls), 2)


class RateLimitContract(unittest.TestCase):
    """The property `run_task` relies on: a rate limit that eventually
    resolves must never reach the contract-violation retry-prompt leg —
    that leg exists for a bad model response, not a probe that never got to
    try. Reaching it on an infra fault is exactly what let B07 burn an
    attempt on a 429."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.pkt = parse(write(self.dir, "B01-example.md", VALID))
        self.artifact = self.dir / "result.json"

    def test_a_resolved_rate_limit_never_reaches_the_retry_prompt_leg(self) -> None:
        clock = _FakeClock(datetime(2026, 8, 9, 12, 0, tzinfo=UTC))
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if len(calls) == 1:
                return unittest.mock.Mock(
                    returncode=0,
                    stdout=json.dumps({**RATE_LIMIT_PAYLOAD, "result": "retry later"}),
                    stderr="",
                )
            self.artifact.write_text(
                json.dumps(
                    {
                        "task_id": "B01",
                        "status": "complete",
                        "verification": {
                            "command": "uv run pytest -q",
                            "passed": 1,
                            "failed": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )
            return unittest.mock.Mock(
                returncode=0, stdout=json.dumps(_success_payload()), stderr=""
            )

        with unittest.mock.patch("agent.subprocess.run", side_effect=fake_run):
            impl, telemetry, err = agent.run_with_contract(
                worktree=self.dir,
                prompt="p",
                model="haiku",
                allowed_tools="",
                run_dir=self.dir,
                label="implement",
                artifact=self.artifact,
                validate=lambda obj: contracts.validate_impl(obj, self.pkt),
                max_wait_s=3600,
                sleep_fn=clock.sleep_fn,
                now_fn=clock.now_fn,
            )

        self.assertIsNone(err)
        self.assertIsNotNone(impl)
        # Exactly the 429 probe plus the one real attempt — never the third
        # call that a contract-violation retry-prompt leg would have made.
        self.assertEqual(len(calls), 2)


class Tiers(unittest.TestCase):
    """`packet.TIERS` and `agent.TIER_MODELS` are two tables that have to agree.

    Nothing structural forces them to: a tier added to one and not the other
    parses fine and fails at the moment a task is claimed, halfway through a
    run, on a task that was never wrong.
    """

    def test_every_tier_resolves_to_a_model(self) -> None:
        for tier in TIERS:
            self.assertIsInstance(agent.resolve(tier), str)

    def test_the_tier_table_declares_nothing_extra(self) -> None:
        self.assertEqual(set(agent.TIER_MODELS), set(TIERS))

    def test_an_unknown_tier_raises_rather_than_defaulting(self) -> None:
        """A typo that silently ran the cheapest model would surface as a task
        that failed for no visible reason."""
        with self.assertRaises(agent.AgentError):
            agent.resolve("cheap")

    def test_the_reviewer_ladder_covers_every_tier(self) -> None:
        for tier in TIERS:
            reviewer = REVIEWER_LADDER[tier]
            self.assertGreaterEqual(
                TIERS.index(reviewer), TIERS.index(tier), f"{tier} reviewed weaker"
            )


class Allowlist(unittest.TestCase):
    def test_the_verification_command_needs_no_cd(self) -> None:
        """The first run's most expensive denial.

        Compound commands are checked per segment, so `cd api && uv run pytest`
        was refused on its first segment — and the worker burned three turns
        inventing shell workarounds for a command its own packet told it to run.
        Every segment must now start with something the allowlist permits.
        """
        pkt = parse(
            write(
                Path(tempfile.mkdtemp()),
                "B01-example.md",
                VALID.replace(
                    'spec_path = "docs/SPEC.md"', 'spec_path = "d"\nsurface = "api"'
                ),
            )
        )
        cmd = run.verify_command(pkt)
        self.assertNotIn("cd ", cmd)
        for segment in cmd.split("&&"):
            self.assertTrue(
                segment.strip().startswith("uv run"),
                f"segment {segment.strip()!r} is not covered by Bash(uv run *)",
            )

    def test_the_implementer_gets_inspection_shell_plus_its_runner(self) -> None:
        for surface, runner in (("api", "uv run"), ("webapp", "npm run")):
            tools = agent.TOOLS_IMPL[surface]
            self.assertIn(f"Bash({runner} *)", tools)
            self.assertIn("Bash(ls *)", tools)
            for forbidden in ("rm", "mv", "chmod", "curl", "pip", "sed -i"):
                self.assertNotIn(f"Bash({forbidden}", tools)

    def test_awk_is_documented_as_the_exception(self) -> None:
        """`awk` can write files and shell out, unlike everything else on the
        list. It is allowed on purpose, but the code must not describe the set
        as read-only while it is there."""
        source = Path(agent.__file__).read_text(encoding="utf-8")
        self.assertIn("Bash(awk *)", agent.TOOLS_IMPL["api"])
        head = source.split("_BASE =")[0]
        self.assertNotIn("READ_ONLY_SHELL", head)
        self.assertIn("not strictly read-only", head)

    def test_the_reviewer_gets_no_shell(self) -> None:
        """Zero denials means no evidence it needs one, and its Write is
        already the sharp edge."""
        self.assertNotIn("Bash", agent.TOOLS_REVIEW)


class AssessContract(unittest.TestCase):
    """The ruling on a packet, before any implementer is paid to run it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pkt = parse(write(Path(self.tmp.name), "B01-example.md", VALID))

    def _assess(self, **over):
        base = {
            "packet_id": "B01",
            "packet_sha256": self.pkt.sha256,
            "verdict": "ready",
            "claims": [
                {
                    "quote": "the file is utf-8",
                    "kind": "data",
                    "status": "held",
                    "evidence": "api/data/x.csv:1",
                }
            ],
            "structural": [
                {"check": c, "status": "pass", "detail": "checked"}
                for c in contracts.STRUCTURAL_CHECKS
            ],
            "findings": [],
        }
        base.update(over)
        return base

    def test_a_valid_assessment_parses(self) -> None:
        result = contracts.validate_assess(self._assess(), self.pkt)
        self.assertEqual(result.verdict, "ready")
        self.assertEqual(result.failed_checks(), [])

    def test_an_assessment_of_a_different_revision_is_rejected(self) -> None:
        """The pin. An assessment of a packet that has since been edited is not
        an assessment of the packet that will run."""
        obj = self._assess(packet_sha256="0" * 64)
        with self.assertRaises(contracts.ContractError) as exc:
            contracts.validate_assess(obj, self.pkt)
        self.assertIn("packet_sha256", str(exc.exception))

    def test_omitting_a_structural_check_is_rejected(self) -> None:
        """An omitted check is not a passed check — the same rule the reviewer
        contract applies to invariants, for the same reason."""
        obj = self._assess(
            structural=[
                {"check": c, "status": "pass", "detail": "d"}
                for c in contracts.STRUCTURAL_CHECKS[:-1]
            ]
        )
        with self.assertRaises(contracts.ContractError) as exc:
            contracts.validate_assess(obj, self.pkt)
        self.assertIn(contracts.STRUCTURAL_CHECKS[-1], str(exc.exception))

    def test_a_check_without_detail_is_rejected(self) -> None:
        """A bare 'pass' is indistinguishable from a check nobody ran."""
        structural = [
            {"check": c, "status": "pass", "detail": "d"}
            for c in contracts.STRUCTURAL_CHECKS
        ]
        structural[0] = {"check": structural[0]["check"], "status": "pass"}
        with self.assertRaises(contracts.ContractError) as exc:
            contracts.validate_assess(self._assess(structural=structural), self.pkt)
        self.assertIn("detail", str(exc.exception))

    def test_a_claim_without_evidence_is_rejected(self) -> None:
        obj = self._assess(claims=[{"quote": "q", "kind": "data", "status": "held"}])
        with self.assertRaises(contracts.ContractError) as exc:
            contracts.validate_assess(obj, self.pkt)
        self.assertIn("evidence", str(exc.exception))

    def test_no_claims_at_all_is_rejected(self) -> None:
        """A packet nobody inventoried is not a packet that passed assessment."""
        with self.assertRaises(contracts.ContractError):
            contracts.validate_assess(self._assess(claims=[]), self.pkt)

    def test_a_blocking_finding_must_say_what_to_write_instead(self) -> None:
        obj = self._assess(
            verdict="recut",
            findings=[{"severity": "blocker", "claim": "names a file that is absent"}],
        )
        with self.assertRaises(contracts.ContractError) as exc:
            contracts.validate_assess(obj, self.pkt)
        self.assertIn("fix", str(exc.exception))

    def test_a_minor_finding_needs_no_fix(self) -> None:
        obj = self._assess(findings=[{"severity": "minor", "claim": "loose wording"}])
        contracts.validate_assess(obj, self.pkt)  # does not raise


class HarnessOverridesTheAssessor(unittest.TestCase):
    """`ready` is a claim the harness re-derives, not one it accepts."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pkt = parse(write(Path(self.tmp.name), "B01-example.md", VALID))

    def _ready(self, *, claims=None, structural=None, findings=None):
        return contracts.validate_assess(
            {
                "packet_id": "B01",
                "packet_sha256": self.pkt.sha256,
                "verdict": "ready",
                "claims": claims
                or [
                    {
                        "quote": "q",
                        "kind": "data",
                        "status": "held",
                        "evidence": "a:1",
                    }
                ],
                "structural": structural
                or [
                    {"check": c, "status": "pass", "detail": "d"}
                    for c in contracts.STRUCTURAL_CHECKS
                ],
                "findings": findings or [],
            },
            self.pkt,
        )

    def test_a_clean_assessment_stays_ready(self) -> None:
        effective, reason = contracts.effective_assessment(self._ready())
        self.assertEqual(effective, "ready")
        self.assertIsNone(reason)

    def test_ready_over_a_failed_structural_check_is_downgraded(self) -> None:
        structural = [
            {"check": c, "status": "pass", "detail": "d"}
            for c in contracts.STRUCTURAL_CHECKS
        ]
        structural[0]["status"] = "fail"
        effective, reason = contracts.effective_assessment(
            self._ready(structural=structural)
        )
        self.assertEqual(effective, "recut")
        self.assertIn(contracts.STRUCTURAL_CHECKS[0], reason)

    def test_ready_over_an_unverifiable_invariant_is_downgraded(self) -> None:
        """`unverifiable` blocks here for the same reason it blocks in review:
        it is the safe non-answer, and an invariant nobody can check is one the
        reviewer will be equally unable to rule on three runs later."""
        effective, reason = contracts.effective_assessment(
            self._ready(
                claims=[
                    {"quote": "q", "kind": "invariant", "status": "unverifiable"},
                ]
            )
        )
        self.assertEqual(effective, "recut")
        self.assertIn("invariant", reason)

    def test_a_violated_non_invariant_claim_does_not_block_on_its_own(self) -> None:
        """Severity is the judgement, not the kind: an imprecise word about a
        file that changes nothing gets reported, not blocked."""
        effective, _ = contracts.effective_assessment(
            self._ready(
                claims=[
                    {
                        "quote": "q",
                        "kind": "data",
                        "status": "violated",
                        "evidence": "a:1",
                    }
                ],
                findings=[{"severity": "minor", "claim": "loose count"}],
            )
        )
        self.assertEqual(effective, "ready")

    def test_ready_over_a_major_finding_is_downgraded(self) -> None:
        effective, reason = contracts.effective_assessment(
            self._ready(
                findings=[
                    {"severity": "major", "claim": "wrong type", "fix": "use int"}
                ]
            )
        )
        self.assertEqual(effective, "recut")
        self.assertIn("major", reason)


class SyncRefusesUnassessedPackets(unittest.TestCase):
    """The gate that makes assessment mandatory rather than advisory."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.tasks = root / "tasks"
        self.assessments = self.tasks / "assessments"
        self.tasks.mkdir()
        self.assessments.mkdir()
        self.fac = Factory(root / "f.db")
        self.addCleanup(self.fac.close)

        for attr, value in (
            ("REPO", root),
            ("TASKS_DIR", self.tasks),
            ("ASSESSMENTS_DIR", self.assessments),
        ):
            patch = unittest.mock.patch.object(run, attr, value)
            patch.start()
            self.addCleanup(patch.stop)

        # board.write would otherwise render a board into the fake repo.
        patch = unittest.mock.patch.object(run.board, "write")
        patch.start()
        self.addCleanup(patch.stop)

        self.pkt = parse(write(self.tasks, "B01-example.md", VALID))

    def _args(self, **over):
        ns = unittest.mock.Mock(allow_unassessed=False)
        for key, value in over.items():
            setattr(ns, key, value)
        return ns

    def _write_assessment(self, *, sha: str, verdict: str = "ready", findings=None):
        (self.assessments / "B01.json").write_text(
            json.dumps(
                {
                    "packet_id": "B01",
                    "packet_sha256": sha,
                    "verdict": verdict,
                    "claims": [
                        {
                            "quote": "q",
                            "kind": "data",
                            "status": "held",
                            "evidence": "a:1",
                        }
                    ],
                    "structural": [
                        {"check": c, "status": "pass", "detail": "d"}
                        for c in contracts.STRUCTURAL_CHECKS
                    ],
                    "findings": findings or [],
                }
            ),
            encoding="utf-8",
        )

    def test_an_unassessed_packet_is_not_queued(self) -> None:
        self.assertEqual(run.cmd_sync(self.fac, self._args()), 2)
        self.assertIsNone(self.fac.get("B01"))

    def test_an_assessed_packet_is_queued(self) -> None:
        self._write_assessment(sha=self.pkt.sha256)
        self.assertEqual(run.cmd_sync(self.fac, self._args()), 0)
        self.assertEqual(self.fac.get("B01").status, "ready")

    def test_an_assessment_of_an_older_revision_does_not_count(self) -> None:
        """The case this is really for: an operator fixes the finding the
        assessment raised, and the assessment that raised it is now describing
        a packet that no longer exists."""
        self._write_assessment(sha=self.pkt.sha256)
        write(self.tasks, "B01-example.md", VALID.replace("Body.", "Edited body."))
        self.assertEqual(run.cmd_sync(self.fac, self._args()), 2)
        self.assertIsNone(self.fac.get("B01"))

    def test_a_recut_verdict_does_not_queue(self) -> None:
        self._write_assessment(sha=self.pkt.sha256, verdict="recut")
        self.assertEqual(run.cmd_sync(self.fac, self._args()), 2)
        self.assertIsNone(self.fac.get("B01"))

    def test_the_harness_downgrade_is_applied_at_the_gate(self) -> None:
        """An assessor that says `ready` while reporting a blocker does not get
        to queue the packet — `sync` re-derives the verdict rather than reading
        the one it was handed."""
        self._write_assessment(
            sha=self.pkt.sha256,
            verdict="ready",
            findings=[{"severity": "blocker", "claim": "no such file", "fix": "recut"}],
        )
        self.assertEqual(run.cmd_sync(self.fac, self._args()), 2)
        self.assertIsNone(self.fac.get("B01"))

    def test_allow_unassessed_is_the_escape_hatch(self) -> None:
        self.assertEqual(run.cmd_sync(self.fac, self._args(allow_unassessed=True)), 0)
        self.assertEqual(self.fac.get("B01").status, "ready")

    def test_an_already_merged_task_is_grandfathered(self) -> None:
        """The 37 packets that merged before assessment existed must not be
        refused now: the work is on the branch and cannot be re-run anyway."""
        run.cmd_sync(self.fac, self._args(allow_unassessed=True))
        self.fac.mark_done("B01", "deadbee")
        self.assertEqual(run.cmd_sync(self.fac, self._args()), 0)
        self.assertEqual(self.fac.get("B01").status, "done")


class PlanningTierTools(unittest.TestCase):
    def test_the_assessor_cannot_edit(self) -> None:
        """Its entire output is a verdict. A planning run that can edit the
        thing it is judging will eventually fix a defect instead of report it."""
        self.assertNotIn("Edit", agent.TOOLS_ASSESS.split(","))
        self.assertIn("Write", agent.TOOLS_ASSESS.split(","))

    def test_both_planners_can_check_claims_against_the_repo(self) -> None:
        """The ground-truth ladder is unusable without a shell — a planner that
        can only read the spec can only restate it."""
        for tools in (agent.TOOLS_CUT, agent.TOOLS_ASSESS):
            self.assertIn("Bash(uv run *)", tools)
            self.assertIn("Bash(grep *)", tools)

    def test_a_cut_report_is_not_mistaken_for_a_packet(self) -> None:
        self.assertTrue(run._is_packet("tasks/B01-example.md"))
        self.assertTrue(run._is_packet("tasks/A10b-example.md"))
        self.assertFalse(run._is_packet("tasks/CUT-REPORT.md"))
        self.assertFalse(run._is_packet("tasks/README.md"))
        self.assertFalse(run._is_packet("tasks/supplied/B01/api/tests/test_x.py"))


class PromptPlaceholders(unittest.TestCase):
    """Every `{{FIELD}}` in a prompt is filled by a call site.

    An unfilled placeholder fails silently and expensively: the model is handed
    a literal `{{PACKET}}` and works from whatever it can infer, so the run
    completes, costs full price, and produces something plausible against no
    packet at all. Renaming a field on one side only is the way that happens.
    """

    def test_every_placeholder_has_a_call_site(self) -> None:
        source = Path(run.__file__).read_text(encoding="utf-8")
        for prompt in sorted(run.PROMPTS.glob("*.md")):
            placeholders = set(
                re.findall(r"\{\{([A-Z_]+)\}\}", prompt.read_text(encoding="utf-8"))
            )
            self.assertTrue(placeholders, f"{prompt.name} has no placeholders")
            for field in sorted(placeholders):
                self.assertIn(
                    f"{field}=",
                    source,
                    f"{prompt.name} wants {{{{{field}}}}}; run.py never supplies it",
                )

    def test_rendering_leaves_nothing_behind(self) -> None:
        rendered = run.render(
            run.PROMPTS / "assess.md",
            REPO_NAME="example-app",
            FAILURE_CLASSES=run.failure_classes(),
            PROJECT_TRAPS=run.project_traps(),
            GROUNDING=run.grounding_summary(),
            PACKET_ID="B01",
            PACKET_PATH="tasks/B01-x.md",
            SPEC_PATH="docs/SPEC.md",
            PACKET_SHA256="abc",
            CUT_REPORT="",
            ASSESS_PATH=".factory/assess.json",
        )
        self.assertNotIn("{{", rendered)

    def test_fragments_are_not_templates(self) -> None:
        """A fragment is composed into a prompt, never rendered itself — so a
        placeholder in one would reach the model literally."""
        for fragment in sorted(run.FRAGMENTS.glob("*.md")):
            with self.subTest(fragment=fragment.name):
                self.assertNotIn("{{", fragment.read_text(encoding="utf-8"))


class SharedCatalogue(unittest.TestCase):
    """`cut.md` and `assess.md` are built around one list of failure classes.

    Two copies would be the same list seen from opposite sides, and would drift
    — leaving the assessor hunting for something the cutter was never warned
    about. So both compose the same fragment, and the slugs they cite by name
    have to exist in it.
    """

    def test_both_planning_prompts_compose_the_catalogue(self) -> None:
        for name in ("cut.md", "assess.md"):
            with self.subTest(prompt=name):
                text = (run.PROMPTS / name).read_text(encoding="utf-8")
                self.assertIn("{{FAILURE_CLASSES}}", text)
                self.assertIn("{{PROJECT_TRAPS}}", text)

    def test_every_class_cited_by_name_is_defined(self) -> None:
        defined = set(re.findall(r"\*\*`([a-z-]+)`\*\*", run.failure_classes()))
        self.assertEqual(defined, _CLASS_SHAPED)

        sources = [run.PROMPTS / "cut.md", run.PROMPTS / "assess.md", run.TRAPS_PATH]
        for path in sources:
            if not path.is_file():
                continue
            with self.subTest(source=path.name):
                text = path.read_text(encoding="utf-8")
                cited = set(re.findall(r"`([a-z]+(?:-[a-z]+){1,3})`", text))
                for slug in cited & _CLASS_SHAPED:
                    self.assertIn(
                        slug,
                        defined,
                        f"{path.name} cites `{slug}`; the catalogue does not "
                        "define it",
                    )


# Slugs that look like a failure class and are cited in prose. Listed rather
# than inferred, so a new hyphenated backtick in an unrelated sentence does not
# quietly become a thing the catalogue is required to define.
_CLASS_SHAPED = {
    "phantom-artifact",
    "unexpressible-sequence",
    "type-disagreement",
    "toolchain-claim",
    "adjective-for-a-measurement",
    "unsatisfiable-invariant",
    "unwinnable-fixture",
    "optimistic-tier",
    "relayed-ambiguity",
}


class RealTasksDirectory(unittest.TestCase):
    """`load_all` against the repository's own `tasks/`, not a temp fixture.

    This is the test that was missing. Every other packet test builds its own
    directory containing only packets, so none of them could see that `tasks/`
    had become a directory where nothing but a packet could live: `load_all`
    excluded non-packets by a denylist of exactly one filename, so `TRAPS.md`
    broke it, and `CUT-REPORT.md` — written by every cut — would have broken it
    on the first real one.

    The first one only means anything where the harness is vendored into a
    project that has cut packets. In the factory's own repository there is no
    `tasks/` to load, and a failure there would report the absence of a project
    as a defect in the loader. It skips instead — the other two are pure and
    run everywhere.
    """

    def test_the_real_tasks_directory_loads(self) -> None:
        if not run.TASKS_DIR.is_dir():
            self.skipTest(
                f"no {run.TASKS_DIR} — the harness is not vendored into a project"
            )
        packets = load_all(run.TASKS_DIR)
        self.assertTrue(packets, "no packets found in the repository's tasks/")

    def test_the_factory_own_documents_are_not_packets(self) -> None:
        for name in ("README.md", "CUT-REPORT.md", "TRAPS.md", "GROUNDING.md"):
            with self.subTest(name=name):
                self.assertFalse(is_packet_filename(name))

    def test_a_packet_filename_is_recognised(self) -> None:
        for name in ("B01-adrh-loader.md", "S08a-snapshot-id.md", "A10b-x.md"):
            with self.subTest(name=name):
                self.assertTrue(is_packet_filename(name))


class Grounding(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)

    def test_noise_is_classified_but_still_reported(self) -> None:
        status = "!! .venv/\n!! api/app/__pycache__/\n!! api/data/\n"
        with unittest.mock.patch.object(ground.gitops, "git", return_value=status):
            found = ground.diverging_paths(self.repo)
        self.assertEqual(
            [d.path for d in found],
            [".venv", "api/app/__pycache__", "api/data"],
        )
        self.assertEqual([d.noise for d in found], [True, True, False])

    def test_reachability_of_each_kind(self) -> None:
        (self.repo / "present").mkdir()

        def fake_git(*args, **kwargs):
            # `ls-files` answers "is it tracked"; only `tracked` is.
            return "tracked\n" if "tracked" in args else ""

        external = self.repo / "outside"
        external.mkdir()

        with unittest.mock.patch.object(ground.gitops, "git", side_effect=fake_git):
            checks = ground.check_inputs(
                self.repo, ["tracked", "present", "gone", str(external), "/mnt/nope"]
            )
        self.assertEqual(
            [(c.path, c.status) for c in checks],
            [
                ("tracked", ground.IN_WORKTREE),
                ("present", ground.REPO_ONLY),
                ("gone", ground.ABSENT),
                (str(external), ground.EXTERNAL),
                # Rooted but not there. `external` must not be a verdict that
                # never blocks and never looks.
                ("/mnt/nope", ground.ABSENT),
            ],
        )
        self.assertEqual([c.blocks for c in checks], [False, True, True, False, True])

    def test_absent_grounding_is_not_an_error(self) -> None:
        self.assertIsNone(ground.load(self.repo / "nope.md"))

    def test_declared_inputs_survive_a_regenerate(self) -> None:
        """The one answer no probe can supply must not be lost by re-running."""
        path = self.repo / "GROUNDING.md"
        path.write_text(
            ground.render(
                grounded_at="2026-08-11",
                grounded_commit="abc1234",
                declared=["api/data"],
                divergences=[],
                checks=[ground.InputCheck("api/data", ground.ABSENT)],
            ),
            encoding="utf-8",
        )
        self.assertEqual(ground.load(path).planner_must_read, ["api/data"])

    def test_malformed_frontmatter_is_an_error(self) -> None:
        path = self.repo / "GROUNDING.md"
        path.write_text("# no frontmatter here", encoding="utf-8")
        with self.assertRaises(ground.GroundError):
            ground.load(path)

    def test_summary_marks_what_cannot_be_opened(self) -> None:
        text = ground.summary(
            ground.Grounding("2026-08-11", "abc", ["api/data"]),
            [ground.InputCheck("api/data", ground.ABSENT)],
        )
        self.assertIn("⛔", text)
        self.assertIn("tracked files only", text)


class Probes(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)

    def test_a_surface_is_present_only_if_its_manifest_is(self) -> None:
        (self.repo / "api").mkdir()
        (self.repo / "api" / "pyproject.toml").write_text("", encoding="utf-8")
        with unittest.mock.patch.object(ground.shutil, "which", return_value="/bin/uv"):
            surfaces, probes = ground.probe_surfaces(self.repo)
        self.assertEqual(surfaces, ["api"])
        self.assertIn("webapp", {p.name.split(":")[1] for p in probes})

    def test_a_missing_runner_is_flagged_not_fatal(self) -> None:
        """`uv` off PATH turns every gate on that surface into an environment
        fault, which is worth knowing before a cut rather than during one."""
        (self.repo / "api").mkdir()
        (self.repo / "api" / "pyproject.toml").write_text("", encoding="utf-8")
        with unittest.mock.patch.object(ground.shutil, "which", return_value=None):
            surfaces, probes = ground.probe_surfaces(self.repo)
        self.assertEqual(surfaces, ["api"])
        api = next(p for p in probes if p.name == "surface:api")
        self.assertFalse(api.ok)
        self.assertIn("NOT on", api.detail)

    def test_toolchain_is_skipped_without_the_api_surface(self) -> None:
        measured, probes = ground.probe_toolchain(self.repo, ["webapp"])
        self.assertEqual(measured, {})
        self.assertEqual(probes, [])

    def test_a_spec_without_history_cannot_be_pinned(self) -> None:
        (self.repo / "SPEC.md").write_text("x", encoding="utf-8")
        with unittest.mock.patch.object(ground.gitops, "git", return_value=""):
            probe = ground.probe_spec(self.repo, "SPEC.md")[0]
        self.assertFalse(probe.ok)
        self.assertIn("no commit history", probe.detail)

    def test_no_declared_spec_is_not_a_failure(self) -> None:
        self.assertTrue(ground.probe_spec(self.repo, "")[0].ok)


class Drift(unittest.TestCase):
    """`--check` reports what changed, never what is merely still true."""

    def _g(self, **kw):
        base = dict(
            grounded_at="2026-08-11",
            grounded_commit="abc",
            planner_must_read=["api/data"],
        )
        return ground.Grounding(**{**base, **kw})

    def test_a_known_blocker_is_not_drift(self) -> None:
        before = self._g(blocking=["api/data"])
        checks = [ground.InputCheck("api/data", ground.ABSENT)]
        regressions, improvements = ground.drift(before, self._g(), checks)
        self.assertEqual(regressions, [])
        self.assertEqual(improvements, [])

    def test_a_new_blocker_is_drift(self) -> None:
        before = self._g(blocking=[])
        checks = [ground.InputCheck("api/data", ground.ABSENT)]
        regressions, _ = ground.drift(before, self._g(), checks)
        self.assertEqual(len(regressions), 1)
        self.assertIn("became unreachable", regressions[0])

    def test_a_resolved_blocker_is_good_news(self) -> None:
        before = self._g(blocking=["api/data"])
        checks = [ground.InputCheck("api/data", ground.IN_WORKTREE)]
        regressions, improvements = ground.drift(before, self._g(), checks)
        self.assertEqual(regressions, [])
        self.assertIn("reachable now", improvements[0])

    def test_a_vanished_surface_is_drift(self) -> None:
        before = self._g(surfaces=["api", "webapp"])
        after = self._g(surfaces=["api"])
        regressions, _ = ground.drift(before, after, [])
        self.assertIn("webapp", regressions[0])

    def test_a_moved_count_is_not_drift(self) -> None:
        """The suite went 81 → 422 as the factory merged into it. A check that
        fired on that is one an operator stops running."""
        before = self._g(measured={"offline_passed": 81})
        after = self._g(measured={"offline_passed": 422})
        regressions, improvements = ground.drift(before, after, [])
        self.assertEqual((regressions, improvements), ([], []))


class Interview(unittest.TestCase):
    """The two questions no probe can answer, and nothing else.

    An earlier draft wanted to ask about the target branch, billing, tiers and
    the `human` gate policy. All four turned out to be answerable elsewhere or
    not project facts at all, and every question that collects an answer it did
    not need adds an asserted fact to a design built to refuse them.
    """

    def _run(self, answers, **kw):
        it = iter(answers)
        opts = dict(
            divergences=[
                ground.Divergence("api/scratch", noise=False),
                ground.Divergence(".venv", noise=True),
            ],
            candidates=[ground.SpecCandidate("docs/A.md", "abc 2026-08-01", 900)],
            declared=[],
            spec_path="",
        )
        opts.update(kw)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            result = run._interview(ask=lambda _: next(it), **opts)
        self.output = buffer.getvalue()
        return result

    def test_numbers_select_from_what_the_probe_found(self) -> None:
        declared, _ = self._run(["1", ""])
        self.assertEqual(declared, ["api/scratch"])

    def test_free_text_declares_a_path_no_probe_can_see(self) -> None:
        """The case this whole feature came from: an input nobody has fetched is
        invisible to the divergence probe *and* to the planner. Offering only
        what was found would make it undeclarable."""
        declared, _ = self._run(["api/data", ""])
        self.assertEqual(declared, ["api/data"])

    def test_numbers_and_paths_mix(self) -> None:
        declared, _ = self._run(["1, api/data", ""])
        self.assertEqual(declared, ["api/data", "api/scratch"])

    def test_noise_is_never_offered(self) -> None:
        self._run(["", ""])
        self.assertIn("api/scratch", self.output)
        self.assertNotIn(".venv", self.output)

    def test_blank_keeps_what_was_already_declared(self) -> None:
        declared, spec = self._run(
            ["", ""], declared=["api/data"], spec_path="docs/S.md"
        )
        self.assertEqual(declared, ["api/data"])
        self.assertEqual(spec, "docs/S.md")

    def test_the_spec_can_be_chosen_by_number_or_typed(self) -> None:
        self.assertEqual(self._run(["", "1"])[1], "docs/A.md")
        self.assertEqual(self._run(["", "docs/Other.md"])[1], "docs/Other.md")

    def test_an_out_of_range_number_is_taken_as_a_path(self) -> None:
        """Better than silently ignoring it: a path that does not exist is
        caught by the reachability check and refused, loudly."""
        self.assertEqual(self._run(["", "9"])[1], "9")


class InterviewGuards(unittest.TestCase):
    def _args(self, **kw):
        return types.SimpleNamespace(
            **{"yes": False, "interview": False, "check": False, **kw}
        )

    def test_yes_never_asks(self) -> None:
        self.assertFalse(run._should_interview(self._args(yes=True), None))

    def test_a_first_grounding_asks(self) -> None:
        with unittest.mock.patch.object(run.sys.stdin, "isatty", return_value=True):
            self.assertTrue(run._should_interview(self._args(), None))

    def test_an_existing_grounding_is_left_alone_unless_asked(self) -> None:
        grounded = ground.Grounding("2026-08-11", "abc", [])
        with unittest.mock.patch.object(run.sys.stdin, "isatty", return_value=True):
            self.assertFalse(run._should_interview(self._args(), grounded))
            self.assertTrue(
                run._should_interview(self._args(interview=True), grounded)
            )


class Conventions(unittest.TestCase):
    """A convention is a claim, not a fact.

    Conventions documents are prose about intent, which is the status `cut.md`
    ranks *below* files on disk. A true one is worth grounding a planner on; a
    stale one is a confident sentence that will be believed, which is the defect
    class the whole factory exists to prevent.
    """

    SOURCES = ["api/README.md", "CONTRIBUTING.md"]

    def _claim(self, **kw):
        base = {
            "source": "api/README.md",
            "quote": "loaders live in ingest/",
            "kind": "structural",
            "status": "held",
            "evidence": "api/ingest/adrh_load.py:1",
        }
        return {**base, **kw}

    def _validate(self, claims):
        return contracts.validate_conventions({"claims": claims}, self.SOURCES)

    def test_a_ruling_is_returned(self) -> None:
        out = self._validate([self._claim()])
        self.assertEqual(out[0].kind, "structural")
        self.assertEqual(out[0].status, "held")

    def test_an_empty_result_is_rejected(self) -> None:
        """A run that found nothing checkable is indistinguishable from one that
        did not look."""
        with self.assertRaises(contracts.ContractError):
            self._validate([])

    def test_a_ruling_on_a_document_nobody_opened_is_rejected(self) -> None:
        with self.assertRaises(contracts.ContractError) as caught:
            self._validate([self._claim(source="docs/INVENTED.md")])
        self.assertIn("not one of the documents", str(caught.exception))

    def test_held_without_evidence_is_rejected(self) -> None:
        """Quoting the document back is not evidence — the document is the thing
        under test."""
        with self.assertRaises(contracts.ContractError):
            self._validate([self._claim(evidence="")])

    def test_unverifiable_needs_no_evidence(self) -> None:
        out = self._validate([self._claim(status="unverifiable", evidence="")])
        self.assertEqual(out[0].status, "unverifiable")

    def test_the_kinds_are_about_documents_not_packets(self) -> None:
        with self.assertRaises(contracts.ContractError):
            self._validate([self._claim(kind="data")])

    def test_a_stale_convention_reaches_the_draft(self) -> None:
        claims = self._validate(
            [
                self._claim(
                    source="CONTRIBUTING.md",
                    quote="run `make test` before pushing",
                    kind="toolchain",
                    status="violated",
                    evidence="no Makefile in the repository root",
                )
            ]
        )
        draft = run._conventions_draft(self.SOURCES, claims)
        self.assertIn("documents are wrong about these", draft)
        self.assertIn("make test", draft)

    def test_the_draft_is_never_project_state(self) -> None:
        """A proposal grounds nothing until an operator moves it. `TRAPS.md` is
        read on every cut, so writing there directly would be the cutter's
        forbidden edit by another route."""
        draft = run._conventions_draft(
            self.SOURCES, self._validate([self._claim(proposed_trap="Do the thing.")])
        )
        self.assertIn("Nothing here is in effect", draft)
        self.assertNotIn("{{", draft)


class ConventionDiscovery(unittest.TestCase):
    def test_named_documents_outrank_readmes(self) -> None:
        listing = "CONTRIBUTING.md\nREADME.md\napi/README.md\nCLAUDE.md\n"
        with unittest.mock.patch.object(ground.gitops, "git", return_value=listing):
            found = ground.convention_candidates(Path("/repo"))
        self.assertEqual(found[:2], ["CLAUDE.md", "CONTRIBUTING.md"])

    def test_readmes_are_offered_when_there_is_nothing_else(self) -> None:
        """This repository's real case: no CLAUDE.md, no CONTRIBUTING.md, and
        the conventions written into `api/README.md` anyway."""
        listing = "README.md\napi/README.md\napi/ingest/README.md\n"
        with unittest.mock.patch.object(ground.gitops, "git", return_value=listing):
            found = ground.convention_candidates(Path("/repo"))
        self.assertEqual(found[0], "README.md")
        self.assertIn("api/README.md", found)

    def test_the_harness_talking_to_itself_is_excluded(self) -> None:
        listing = "factory/README.md\ntasks/README.md\nREADME.md\n"
        with unittest.mock.patch.object(ground.gitops, "git", return_value=listing):
            found = ground.convention_candidates(Path("/repo"))
        self.assertEqual(found, ["README.md"])


class SurfaceTable(unittest.TestCase):
    def test_the_installer_and_the_probe_read_one_table(self) -> None:
        """`load_all` and `_is_packet` diverged because the rule had two copies.
        The surface definition does not get a second one."""
        self.assertEqual(set(gitops.SURFACES), set(packet_module.SURFACES))
        for name, spec in gitops.SURFACES.items():
            with self.subTest(surface=name):
                self.assertEqual(spec["dir"], name)
                self.assertIn("manifest", spec)
                self.assertIn("tool", spec)


class FailFast(unittest.TestCase):
    """Planning refuses before a worktree or a model costs anything."""

    def _halt(self, grounding, checks):
        with (
            unittest.mock.patch.object(run.ground, "load", return_value=grounding),
            unittest.mock.patch.object(run.ground, "check_inputs", return_value=checks),
        ):
            return run.require_reachable_inputs("cut", allow=False)

    def test_ungrounded_halts_and_names_the_remedy(self) -> None:
        with self.assertRaises(run.Halt) as caught:
            self._halt(None, [])
        self.assertIn("run.py ground", str(caught.exception))

    def test_an_unreachable_declared_input_halts(self) -> None:
        grounding = ground.Grounding("2026-08-11", "abc", ["api/data"])
        with self.assertRaises(run.Halt) as caught:
            self._halt(grounding, [ground.InputCheck("api/data", ground.ABSENT)])
        self.assertIn("api/data", str(caught.exception))

    def test_reachable_inputs_pass(self) -> None:
        grounding = ground.Grounding("2026-08-11", "abc", ["api/data"])
        self._halt(grounding, [ground.InputCheck("api/data", ground.IN_WORKTREE)])

    def test_the_escape_hatch_proceeds(self) -> None:
        grounding = ground.Grounding("2026-08-11", "abc", ["api/data"])
        checks = [ground.InputCheck("api/data", ground.ABSENT)]
        with (
            unittest.mock.patch.object(run.ground, "load", return_value=grounding),
            unittest.mock.patch.object(run.ground, "check_inputs", return_value=checks),
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            run.require_reachable_inputs("cut", allow=True)
        self.assertIn("api/data", err.getvalue())


class ProjectTraps(unittest.TestCase):
    def test_absent_traps_file_says_so(self) -> None:
        """A project that has not run the harness renders an explicit note.

        An empty section would read as "nothing to watch for here", which is a
        claim, and the wrong one.
        """
        with unittest.mock.patch.object(run, "TRAPS_PATH", Path("/nope/TRAPS.md")):
            text = run.project_traps()
        self.assertTrue(text.strip())
        self.assertIn("Nothing recorded yet", text)

    def test_the_cutter_may_not_edit_the_traps_file(self) -> None:
        """It grounds every future cut, so it is refused rather than copied
        back — the same reason the assessor gets no `Edit` at all."""
        status = " M tasks/TRAPS.md\n?? tasks/B01-new.md\n"
        with unittest.mock.patch.object(run.gitops, "git", return_value=status):
            produced, refused = run._cut_output(Path("/wt"))
        self.assertEqual(produced, ["tasks/B01-new.md"])
        self.assertEqual(refused, ["tasks/TRAPS.md"])


class RepoName(unittest.TestCase):
    """`{{REPO_NAME}}` comes from the remote, not from the checkout directory.

    A working copy checked out as `proj-wt` while the project is `example-app`
    is the case the directory name gets wrong.
    """

    def _name(self, remote: str, *, directory: str = "some-checkout") -> str:
        with unittest.mock.patch("gitops.git", return_value=remote):
            return gitops.repo_name(Path("/tmp") / directory)

    def test_scp_style_remote(self) -> None:
        self.assertEqual(
            self._name("git@github.com:acme/example-app.git\n"), "example-app"
        )

    def test_https_remote(self) -> None:
        self.assertEqual(
            self._name("https://github.com/acme/example-app.git\n"), "example-app"
        )

    def test_remote_without_git_suffix_or_trailing_slash(self) -> None:
        self.assertEqual(self._name("https://example.com/team/thing/\n"), "thing")

    def test_no_remote_falls_back_to_the_directory(self) -> None:
        """A repo that has never been pushed still needs a name."""
        self.assertEqual(self._name("", directory="local-only"), "local-only")


class ProjectDefaults(unittest.TestCase):
    """The two CLI defaults that cannot be a constant.

    Both were literals naming this project. A wrong branch is caught on the
    first run; a wrong spec is not caught at all — the packets pin to it and the
    drift check then watches a file the work has nothing to do with.
    """

    def test_branch_is_left_unset_for_main_to_resolve(self) -> None:
        for argv in (["run"], ["approve", "B01"], ["resume", "B01"]):
            with self.subTest(command=argv[0]):
                self.assertIsNone(run.build_parser().parse_args(argv).branch)

    def test_cut_takes_no_spec_from_the_source(self) -> None:
        """Layer 1 removed the baked-in default; Stage 2 moved the answer into
        `tasks/GROUNDING.md`, which is the operator's, in git, and probed. So
        argparse supplies nothing and `cmd_cut` resolves it."""
        self.assertIsNone(run.build_parser().parse_args(["cut", "--scope", "x"]).spec)

    def test_cut_refuses_when_no_spec_is_declared_anywhere(self) -> None:
        args = run.build_parser().parse_args(["cut", "--scope", "x"])
        grounded = ground.Grounding("2026-08-11", "abc", [], spec_path="")
        with (
            unittest.mock.patch.object(run, "require_reachable_inputs"),
            unittest.mock.patch.object(run.ground, "load", return_value=grounded),
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            self.assertEqual(run.cmd_cut(None, args), 2)
        self.assertIn("no spec of record", err.getvalue())

    def test_grounding_supplies_the_spec_when_the_flag_does_not(self) -> None:
        args = run.build_parser().parse_args(["cut", "--scope", "x"])
        grounded = ground.Grounding("2026-08-11", "abc", [], spec_path="docs/NOPE.md")
        with (
            unittest.mock.patch.object(run, "require_reachable_inputs"),
            unittest.mock.patch.object(run.ground, "load", return_value=grounded),
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            run.cmd_cut(None, args)
        # It got past "no spec declared" and on to resolving the declared one.
        self.assertIn("no spec at docs/NOPE.md", err.getvalue())


class SuppliedFiles(unittest.TestCase):
    def test_bytecode_is_not_a_supplied_file(self) -> None:
        """Caught on the first real run: a `.pyc` left by running the planted
        test locally got planted too, and was then declared off-limits — so
        pytest regenerating it would have failed the tamper gate."""
        self.assertTrue(run._is_build_residue(Path("api/tests/__pycache__/t.pyc")))
        self.assertTrue(run._is_build_residue(Path("api/tests/__pycache__/t.py")))
        self.assertFalse(run._is_build_residue(Path("api/tests/test_vintage.py")))


class Queue(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fac = Factory(Path(self.tmp.name) / "f.db")
        self.addCleanup(self.fac.close)

    def _add(self, task_id: str, requires: list[str], ordinal: int) -> None:
        self.fac.upsert_packet(
            {
                "id": task_id,
                "slug": task_id.lower(),
                "goal": "g",
                "packet_path": f"tasks/{task_id}.md",
                "packet_sha256": "x",
                "spec_commit": "abc",
                "spec_path": "docs/SPEC.md",
                # `tasks.model`/`tasks.reviewer` hold tiers, not models.
                "model": "basic",
                "reviewer": "standard",
                "gate": "auto",
                "needs_db": False,
                "surface": "api",
                "max_attempts": 3,
                "requires": requires,
            },
            ordinal=ordinal,
        )

    def test_a_task_is_not_handed_out_until_its_dependency_merges(self) -> None:
        self._add("S01", [], 0)
        self._add("B01", ["S01"], 1)

        first = self.fac.claim_next()
        self.assertEqual(first.id, "S01")
        # B01 is not runnable while S01 is merely in flight.
        self.assertIsNone(self.fac.claim_next())

        self.fac.mark_done("S01", "deadbee")
        self.assertEqual(self.fac.claim_next().id, "B01")

    def test_a_blocked_dependency_leaves_its_dependents_unreachable(self) -> None:
        self._add("S01", [], 0)
        self._add("B01", ["S01"], 1)
        self.fac.set_status("S01", "blocked", blocked_reason="nope")

        self.assertEqual([t[0] for t in self.fac.unreachable()], ["B01"])

    def test_the_planned_order_simulates_the_unlock(self) -> None:
        self._add("B01", ["S01"], 1)
        self._add("S01", [], 0)
        self.assertEqual([t.id for t in self.fac.runnable_order()], ["S01", "B01"])

    def test_billed_and_notional_spend_are_never_added_together(self) -> None:
        """A subscription run still reports a cost; nothing was charged.

        Summing the two produces a number describing neither, and the cost cap
        would then halt a queue that cost nothing.
        """
        self._add("S01", [], 0)
        self._add("S02", [], 1)
        paid = self.fac.start_attempt(
            "S01", model="haiku", branch="b", base_sha="a", run_dir="d", billing="api"
        )
        self.fac.finish_attempt(paid, cost_usd=0.10)
        free = self.fac.start_attempt(
            "S02",
            model="sonnet",
            branch="b",
            base_sha="a",
            run_dir="d",
            billing="subscription",
        )
        self.fac.finish_attempt(free, cost_usd=0.50)
        self.fac.record_review(
            free,
            reviewer_model="sonnet",
            verdict="accept",
            effective="accept",
            override_reason=None,
            invariants=[],
            findings=[],
            cost_usd=0.25,
            billing="subscription",
        )

        split = self.fac.cost_split()
        self.assertAlmostEqual(split["billed"], 0.10)
        self.assertAlmostEqual(split["notional"], 0.75)
        self.assertAlmostEqual(self.fac.total_cost(billed_only=True), 0.10)

    def test_findings_survive_for_the_next_attempt(self) -> None:
        self._add("S01", [], 0)
        attempt = self.fac.start_attempt(
            "S01", model="haiku", branch="task/S01", base_sha="a", run_dir="d"
        )
        self.fac.record_review(
            attempt,
            reviewer_model="sonnet",
            verdict="revise",
            effective="revise",
            override_reason=None,
            invariants=[{"id": "one", "status": "violated", "evidence": "a.py:1"}],
            findings=[{"severity": "blocker", "file": "a.py", "claim": "wrong"}],
            cost_usd=0.01,
        )
        prior = self.fac.prior_findings("S01")
        self.assertEqual(len(prior), 1)
        self.assertEqual(prior[0]["findings"][0]["claim"], "wrong")

    def test_revert_attempt_rolls_back_the_budget_but_keeps_the_row(self) -> None:
        """C07's shape: a leg that never got a real try at the task, because
        the CLI was rate-limited before the model did anything. It must not
        consume part of `max_attempts`'s real retry budget — but the row
        should stay, finished, for the audit trail `.factory/runs/` promises."""
        self._add("S01", [], 0)
        attempt = self.fac.start_attempt(
            "S01", model="sonnet", branch="task/S01", base_sha="a", run_dir="d"
        )
        self.assertEqual(self.fac.get("S01").attempts, 1)

        self.fac.revert_attempt("S01", attempt, "rate limited; wall-clock cap exceeded")

        self.assertEqual(self.fac.get("S01").attempts, 0)
        row = self.fac.conn.execute(
            "SELECT agent_status, ended_at FROM attempts WHERE id=?", (attempt,)
        ).fetchone()
        self.assertEqual(row["agent_status"], "rate_limited")
        self.assertIsNotNone(row["ended_at"])

    def test_reverted_legs_do_not_collide_on_run_dir_numbering(self) -> None:
        """`attempt_count` — what callers use to name `attempt-<n>/` — must
        keep climbing even after a revert, or a retry's transcript would
        silently overwrite the reverted leg's own logs."""
        self._add("S01", [], 0)
        first = self.fac.start_attempt(
            "S01", model="sonnet", branch="task/S01", base_sha="a", run_dir="attempt-1"
        )
        self.assertEqual(self.fac.attempt_count("S01"), 1)
        self.fac.revert_attempt("S01", first, "rate limited")
        self.assertEqual(self.fac.get("S01").attempts, 0)

        # A real fix for the run_dir bug: numbering must not reuse "attempt-1"
        # even though the budget counter dropped back to 0.
        self.assertEqual(self.fac.attempt_count("S01") + 1, 2)

    def test_a_never_reverted_attempt_still_counts_normally(self) -> None:
        self._add("S01", [], 0)
        attempt = self.fac.start_attempt(
            "S01", model="sonnet", branch="task/S01", base_sha="a", run_dir="d"
        )
        self.fac.finish_attempt(attempt, agent_status="complete", gate_status="green")
        self.assertEqual(self.fac.get("S01").attempts, 1)
        self.assertEqual(self.fac.attempt_count("S01"), 1)


class Reset(unittest.TestCase):
    """`reset` is not a retry: a retry keeps prior findings so the next
    attempt is told what it got wrong; a reset is for when that history
    should not carry forward, so it must wipe it and rebuild clean."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.fac = Factory(root / "f.db")
        self.addCleanup(self.fac.close)
        self.enterContext(unittest.mock.patch.object(run, "REPO", root))
        self.enterContext(unittest.mock.patch.object(run, "STATE", root / ".factory"))
        self.fac.upsert_packet(
            {
                "id": "B01",
                "slug": "b01",
                "goal": "g",
                "packet_path": "tasks/B01.md",
                "packet_sha256": "x",
                "spec_commit": "abc",
                "spec_path": "docs/SPEC.md",
                # `tasks.model`/`tasks.reviewer` hold tiers, not models.
                "model": "basic",
                "reviewer": "standard",
                "gate": "human",
                "needs_db": False,
                "surface": "api",
                "max_attempts": 3,
                "requires": [],
            },
            ordinal=0,
        )

    def test_db_reset_clears_attempts_and_reopens_the_task(self) -> None:
        attempt = self.fac.start_attempt(
            "B01", model="haiku", branch="task/B01", base_sha="a", run_dir="d"
        )
        self.fac.record_review(
            attempt,
            reviewer_model="sonnet",
            verdict="revise",
            effective="revise",
            override_reason=None,
            invariants=[{"id": "one", "status": "violated", "evidence": "a.py:1"}],
            findings=[{"severity": "blocker", "file": "a.py", "claim": "wrong"}],
            cost_usd=0.01,
        )
        self.fac.set_status("B01", "awaiting_human", blocked_reason="held")

        self.fac.reset("B01")

        task = self.fac.get("B01")
        self.assertEqual(task.status, "ready")
        self.assertEqual(task.attempts, 0)
        self.assertIsNone(task.blocked_reason)
        self.assertIsNone(task.merged_sha)
        self.assertEqual(self.fac.prior_findings("B01"), [])

    def test_reset_on_an_unknown_task_raises(self) -> None:
        with self.assertRaises(KeyError):
            self.fac.reset("NOPE")

    def test_cmd_reset_refuses_a_done_task_without_force(self) -> None:
        self.fac.mark_done("B01", "deadbee")
        args = unittest.mock.Mock(task_id="B01", force=False)
        with (
            unittest.mock.patch("run.gitops.worktree_remove") as remove_mock,
            unittest.mock.patch("run.gitops.branch_delete") as delete_mock,
        ):
            rc = run.cmd_reset(self.fac, args)
        self.assertEqual(rc, 2)
        remove_mock.assert_not_called()
        delete_mock.assert_not_called()
        self.assertEqual(self.fac.get("B01").status, "done")

    def test_cmd_reset_refuses_an_in_flight_task_without_force(self) -> None:
        self.fac.set_status("B01", "reviewing")
        args = unittest.mock.Mock(task_id="B01", force=False)
        rc = run.cmd_reset(self.fac, args)
        self.assertEqual(rc, 2)
        self.assertEqual(self.fac.get("B01").status, "reviewing")

    def test_cmd_reset_an_unknown_task(self) -> None:
        args = unittest.mock.Mock(task_id="NOPE", force=False)
        rc = run.cmd_reset(self.fac, args)
        self.assertEqual(rc, 2)

    def test_cmd_reset_force_wipes_a_done_task_and_removes_the_worktree(self) -> None:
        self.fac.mark_done("B01", "deadbee")
        args = unittest.mock.Mock(task_id="B01", force=True)
        with (
            unittest.mock.patch("run.gitops.worktree_remove") as remove_mock,
            unittest.mock.patch("run.gitops.branch_delete") as delete_mock,
            unittest.mock.patch("run.board.write"),
        ):
            rc = run.cmd_reset(self.fac, args)
        self.assertEqual(rc, 0)
        remove_mock.assert_called_once_with(run.REPO, run.STATE / "worktrees" / "B01")
        delete_mock.assert_called_once_with(run.REPO, "task/B01")
        task = self.fac.get("B01")
        self.assertEqual(task.status, "ready")
        self.assertIsNone(task.merged_sha)

    def test_cmd_reset_awaiting_human_needs_no_force(self) -> None:
        self.fac.set_status("B01", "awaiting_human", blocked_reason="held")
        args = unittest.mock.Mock(task_id="B01", force=False)
        with (
            unittest.mock.patch("run.gitops.worktree_remove"),
            unittest.mock.patch("run.gitops.branch_delete"),
            unittest.mock.patch("run.board.write"),
        ):
            rc = run.cmd_reset(self.fac, args)
        self.assertEqual(rc, 0)
        self.assertEqual(self.fac.get("B01").status, "ready")


if __name__ == "__main__":
    unittest.main(verbosity=2)
