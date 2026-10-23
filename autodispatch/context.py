from pathlib import Path

from .model import CATALOG, Plan, Task, within


def context(plan: Plan, task: Task, phase: str, sha="", evidence=None, repo=None) -> str:
    repo = repo or plan.repo
    prompt_path = f"prompts/tasks/{task.id}-{task.agent}.md"
    if phase in {"red-check", "green-check", "review"} and task.agent == "OpenCode":
        prompt_path = f"prompts/verify/{task.id}-Codex.md"
    parts = [
        f"Task {task.id}; phase={phase}; reviewed commit={sha}.",
        "Use the default sandbox for command execution. Do not provide sandbox_permissions or request escalation. "
        "If a command is rejected, report BLOCKED; do not bypass the policy.",
        "The HUMAN has explicitly authorized external OS-script scheduling under the approval file. "
        "Historical instructions about manual prompt distribution remain historical; you only perform this ONE stage.",
        "NO LIVE, orders, trading credentials, deployments, risk bypass, SPEC/schema edits, scope expansion, "
        "pushes, PRs, merges, git resets, or external communications. Do not launch another Agent. "
        "Do not read home credentials. Do not modify approved files or T01-T66/Q qualification status. "
        "No stage may claim unregistered/unrun acceptance passed.",
        "Ownership: " + ", ".join(task.scope_write),
        "Root AGENTS.md:\n" + within(repo, "AGENTS.md").read_text(encoding="utf-8"),
        "Current task card:\n" + within(repo, task.task_card).read_text(encoding="utf-8"),
    ]
    if within(repo, prompt_path).is_file():
        parts.append("Existing execution/verification protocol:\n" + within(repo, prompt_path).read_text(encoding="utf-8"))
    parts.append("Direct dependency interfaces (only):\n" + "\n".join(
        f"{dep}: " + "\n".join(plan.tasks[dep].interfaces) for dep in task.dependencies))
    # Relevant catalog rows only, not all 39 cards or all Skills.
    catalog = within(repo, CATALOG).read_text(encoding="utf-8")
    ids = {task.id, *task.dependencies}
    parts.append("Related interface catalog rows:\n" + "\n".join(
        line for line in catalog.splitlines() if any(tid in line for tid in ids)))
    parts.append("Approved SPEC is under docs/spec/approved/trading-architecture/. "
                 "Read only sections required by this card; approval is docs/spec/APPROVAL.md. "
                 "Read relevant entries in ACCEPTANCE_TRACEABILITY.md; no full-plan bulk ingestion.")
    if phase == "red":
        parts.append(f"Write ONLY {task.primary_test_file} and {task.test_support_file}. "
                     "Create failing behavior assertions. Do not run tests/builds. Do not commit; runner records exact paths and SHA. Return RED_READY.")
    elif phase in {"batch-implement", "batch-repair"}:
        parts.append("CURRENT HUMAN WORKFLOW OVERRIDE: write this task's production code AND its complete "
                     "tests/fixtures in this stage. Existing RED provenance is NOT a prerequisite for this "
                     "batch. Do not run tests/builds, create environments, or claim RED/GREEN/review passed. "
                     "The authorized runner integrates local dev without task-level verification, then runs "
                     "the full batch checks at its final exact SHA. Earlier worklogs describe earlier stages; "
                     "use the supplied dependency integration receipts for current integration status. "
                     "Dependencies integrated without verification are code inputs, not proof of safety. "
                     "Do not block ordinary implementation solely on absent RED, deferred tests/review, "
                     "or an earlier worklog saying verification pending. Preserve funds-safety/qualification "
                     "gates: native/real-platform evidence that has not run MUST remain NOT_RUN. Implement "
                     "the offline code/probes with honest boundaries; never substitute mocks as qualification. "
                     "Do not commit, merge, push, deploy, launch agents or modify files outside ownership. "
                     "Update worklog to CODE_READY_VERIFY_PENDING, tests/review DEFERRED_TO_BATCH. "
                     "Your final message MUST start CODE_READY_VERIFY_PENDING if implementation is ready "
                     "for unverified integration. Otherwise start BLOCKED and explain the concrete blocker.")
        if phase == "batch-repair":
            parts.append("RECOVERY STAGE: the runner restored a scoped source snapshot in a fresh worktree. "
                         "Expected uncommitted files are retained partial work, not a reason to refuse. "
                         "Read the supplied failure log and code, identify the concrete cause, repair it, "
                         "and complete remaining owned files. Preserve useful progress; do not restart blindly. "
                         "For a timeout, make bounded progress using the extended but finite attempt budget. "
                         "Do not run tests/builds; final batch validation remains deferred. "
                         "Record the root cause and what changed in this task's worklog.")
    elif phase == "implement":
        parts.append("Implement only this task. Do not commit; the runner validates scope then commits. "
                     "Do not create .venv/node_modules. OpenCode must NOT run tests/builds. "
                     "If shared dependency/contract changes are needed, STOP and report BLOCKED. "
                     "Keep RED test files unchanged. Return CODE_READY_VERIFY_PENDING.")
    else:
        parts.append("Independent read-only verification/review. Inspect the actual committed diff and machine evidence. "
                     "Check that tests assert real behavior, not precomputed success, missing safety cases or mocks for native proof. "
                     "Do not write any tracked file or claim this structured report replaces machine test evidence. "
                     "Return the exact required JSON schema. APPROVED only if evidence matches this commit and phase; otherwise BLOCKED/CHANGES_REQUESTED. "
                     "findings contains ONLY unresolved actionable defects. For APPROVED, findings MUST be []. "
                     "Do not put successful checks, approval explanations, historical limitations or NOT_RUN scope notes in findings. "
                     "Use verified_commands for actual command names; preserve required NOT_RUN states without calling them findings unless they block this specific task.")
    if evidence:
        import json
        parts.append("Machine evidence:\n" + json.dumps(evidence, ensure_ascii=False))
    return "\n\n".join(parts)
