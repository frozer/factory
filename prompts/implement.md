You are implementing one task in the `{{REPO_NAME}}` repository. You are working
in a git worktree that has been branched for you. Nobody else is editing it.

# Ground rules

1. **The packet below is complete.** Do not go looking for the design spec in
   `docs/`. Everything you need has been copied into the packet on purpose. If
   something genuinely is not there, stop and report `blocked` — do not
   improvise a substitute.
2. **Backend commands go through `uv`, never `pip` and never a bare `pytest`.**
   Your shell starts at the repository root, so target the sub-project with
   `--directory` rather than changing into it:
   `uv run --directory api pytest -q`, `uv run --directory api ruff check .`.
3. **Prefer `Read`, `Glob` and `Grep` over shell.** They are cheaper and always
   permitted. Shell is available for anything read-only plus `uv run`, but a
   command the permission layer refuses costs you a whole turn for nothing.
4. **Stay inside your files.** The packet lists what you may create and edit.
   Touching anything else fails the task mechanically, whatever the diff looks
   like — another task owns those files.
5. **Do not commit.** Leave your changes in the working tree. The harness
   commits, reviews and merges.

{{PLANTED_BLOCK}}

# Forbidden paths

These are checked by the harness after you finish. Modifying any of them fails
the attempt outright:

{{FORBIDDEN}}

# Deletable paths

You have no shell command that can delete a file — `rm`, `git rm` and every
other removal path are deliberately absent from your tools, worktree included.
If the packet calls for removing a file, do not fight the sandbox for a
workaround: leave it in place and list its path in `files_deleted` in
`{{RESULT_PATH}}`. The harness deletes it for you, after your response is
validated, from exactly this list — nothing else:

{{DELETABLE}}

Naming a path here that is not on that list fails the contract.

# The packet

{{PACKET}}

{{PRIOR_FEEDBACK}}

# Verification

Run this yourself before you finish, and fix what it reports:

```
{{VERIFY_CMD}}
```
{{FORMAT_NOTE}}
The harness will run it again independently. Your report of the result is
recorded but is not what decides the outcome — so there is no advantage in
describing it as better than it was, and a real cost if you do.

# Finish by writing your result

The last thing you do is write **`{{RESULT_PATH}}`**, exactly at that path, as
valid JSON:

```json
{
  "task_id": "{{TASK_ID}}",
  "status": "complete",
  "files_created": ["api/ingest/example_load.py"],
  "files_modified": ["api/sources.yaml"],
  "files_deleted": [],
  "tests_added": ["api/tests/test_example.py"],
  "verification": {"command": "uv run pytest -q", "passed": 83, "failed": 0},
  "deviations": [],
  "blocked_reason": null,
  "notes": ""
}
```

`status` is `complete`, `blocked` (you could not proceed — say why in
`blocked_reason`) or `failed` (you tried and could not get it green).

**`deviations` is the field that matters most.** Every place you did something
other than what the packet said goes here, as
`{"from_packet": "...", "reason": "..."}`. Deviating is sometimes correct. Not
declaring it is not. The reviewer reads this first, and an undeclared deviation
found in the diff is treated as a worse failure than the deviation itself.
