from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

GRAPH = "docs/superpowers/plans/ai-trading/task-graph.json"
MANIFEST = "docs/spec/spec-manifest.json"
CATALOG = "docs/superpowers/plans/ai-trading/INTERFACE_CATALOG.md"
STATES = {"PENDING", "READY", "RUNNING", "RED_READY", "RED_VERIFIED",
          "CODE_READY_VERIFY_PENDING", "REVIEWING", "WAITING_CI", "WAITING_PR",
          "WAITING_HUMAN_APPROVAL", "MERGED_VERIFIED", "INTEGRATED_UNVERIFIED", "BLOCKED", "FAILED",
          "RECOVERY_QUEUED", "RECOVERY_RUNNING", "RECOVERY_EXHAUSTED"}
OPEN_CODE = {"IP-05", "IP-20", "IP-28", "IP-29", "IP-31"}


class Blocked(RuntimeError):
    """A fail-closed policy or evidence boundary."""


class PublicationPending(Blocked):
    """A completed local task whose single publication attempt failed."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def relative(value: str, glob: bool = False) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise Blocked(f"Unsafe relative path: {value!r}")
    p = PurePosixPath(value)
    if p.is_absolute() or any(x in {"..", "."} for x in value.split("/")):
        raise Blocked(f"Unsafe relative path: {value}")
    if re.search(r"[\x00-\x1f;|&`$<>]", value) or (not glob and re.search(r"[*?\[]", value)):
        raise Blocked(f"Unsafe path characters: {value}")
    return value


def within(root: Path, value: str) -> Path:
    relative(value)
    root = root.resolve()
    out = root / value
    if not out.resolve().is_relative_to(root):
        raise Blocked(f"Path escapes root: {value}")
    for part in [out, *out.parents]:
        if part == root:
            break
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise Blocked(f"Linked path forbidden: {value}")
    return out


@dataclass(frozen=True)
class Task:
    id: str
    agent: str
    dependencies: tuple[str, ...]
    scope_write: tuple[str, ...]
    task_card: str
    verify_command: str
    primary_test_file: str
    test_support_file: str | None
    interfaces: tuple[str, ...]
    title: str


class Plan:
    def __init__(self, repo: Path, pins: dict | None = None):
        self.repo = repo.resolve()
        raw = within(self.repo, GRAPH).read_bytes()
        self.sha = digest(raw)
        obj = json.loads(raw)
        self.plan_status = obj["plan_status"]
        self.tasks = {}
        for row in obj["tasks"]:
            tid = row["id"]
            if tid in self.tasks or not re.fullmatch(r"IP-\d{2}", tid):
                raise Blocked("Duplicate or invalid task ID")
            expected_agent = "OpenCode" if tid in OPEN_CODE else "Codex"
            if row["agent"] != expected_agent:
                raise Blocked(f"Agent assignment drift: {tid}")
            scope = tuple(relative(p, glob=True) for p in row["scope_write"])
            if not scope:
                raise Blocked("Empty ownership scope")
            self.tasks[tid] = Task(tid, row["agent"], tuple(row["dependencies"]), scope,
                                   relative(row["task_card"]), row["verify_command"],
                                   relative(row.get("primary_test_file", "tests/harness/test_repository_bootstrap.py")),
                                   relative(row["test_support_file"]) if row.get("test_support_file") else None,
                                   tuple(row.get("interfaces", [])), row["title"])
        if set(self.tasks) != {f"IP-{i:02}" for i in range(39)}:
            raise Blocked("Expected exactly IP-00 through IP-38")
        seen, stack = set(), set()

        def visit(tid):
            if tid in stack or tid not in self.tasks:
                raise Blocked("DAG cycle or unknown dependency")
            if tid in seen:
                return
            stack.add(tid)
            for dep in self.tasks[tid].dependencies:
                visit(dep)
            stack.remove(tid)
            seen.add(tid)

        for tid in self.tasks:
            visit(tid)
            if not within(self.repo, self.tasks[tid].task_card).is_file():
                raise Blocked(f"Missing task card: {tid}")
        if pins:
            for path, expected in pins.items():
                if digest(within(self.repo, path).read_bytes()) != expected:
                    raise Blocked(f"Pinned input changed: {path}")


def validate_spec(repo: Path, manifest_sha: str) -> dict:
    raw = within(repo, MANIFEST).read_bytes()
    if digest(raw) != manifest_sha:
        raise Blocked("Approved manifest changed; never refresh hashes to hide drift")
    files = json.loads(raw)["files"]
    if len(files) != 23:
        raise Blocked("Expected exactly 23 approved originals")
    for path, expected in files.items():
        if digest(within(repo, path).read_bytes()) != expected:
            raise Blocked(f"Approved bytes changed: {path}")
    return files


def load_config(path: Path) -> dict:
    c = read_json(path)
    c["config_dir"] = str(path.resolve().parent)
    for key in ("repo", "state_dir", "worktree_root"):
        c[key] = str(Path(c[key]).resolve())
    repo, state, wt = (Path(c[x]) for x in ("repo", "state_dir", "worktree_root"))
    if state.is_relative_to(repo) or wt.is_relative_to(repo) or repo.is_relative_to(state):
        raise Blocked("State and worktrees must be outside the product checkout")
    if state == wt or state.is_relative_to(wt) or wt.is_relative_to(state):
        raise Blocked("State and worktree roots must be disjoint")
    for key in ("task_timeout_seconds", "command_timeout_seconds", "max_agent_requests", "max_tasks"):
        if type(c.get(key)) is not int or not 1 <= c[key] <= 86400:
            raise Blocked(f"Finite positive {key} required")
    if c.get('max_workers') != 'auto' and (type(c.get('max_workers')) is not int or not 1 <= c['max_workers'] <= 39):
        raise Blocked('max_workers must be auto or an integer between 1 and 39')
    if c["max_tasks"] > 39:
        raise Blocked("Worker/task limit too large")
    if c.get("max_auto_retries", 0) not in {0, 1}:
        raise Blocked("At most one automatic retry is permitted")
    if c.get("opencode_environment") not in {"windows", "wsl"}:
        raise Blocked("Select windows or wsl explicitly")
    if c.get("publication_mode") not in {"deferred", "draft-pr"}:
        raise Blocked("Publication mode must be explicitly deferred or draft-pr")
    if c.get("integration_target", "origin-dev") not in {"origin-dev", "local-dev"}:
        raise Blocked("Integration target must be origin-dev or local-dev")
    if c.get("integration_target") == "local-dev" and c["publication_mode"] != "deferred":
        raise Blocked("Local development mode cannot publish remotely")
    if c.get("codex_windows_sandbox") not in {"unelevated", "elevated"}:
        raise Blocked("Codex Windows sandbox backend must be explicitly selected")
    if c.get("remote_url") != "https://github.com/Wuysau/AetherTrade.git":
        raise Blocked("Unexpected origin; preserve existing remote")
    if c.get("environment") != "OFFLINE_DEVELOPMENT" or c.get("live_authorized") is not False:
        raise Blocked("LIVE and credential-bearing runtime forbidden")
    if c.get("pins", {}).get(GRAPH) != "9a15031d6d8fafa754fad58a698328dbcc2b59be5aad508b4d49d000ed053d8c":
        raise Blocked("The approved 39-task graph pin cannot be silently replaced")
    if c.get("manifest_sha256") != "ce3f17a96135761eb1c33a6f6d104840c213beb0711502c7a49f86ceb4cede28":
        raise Blocked("The original approved manifest pin cannot be replaced")
    for name in ("codex_command", "opencode_command", "npm_command"):
        prefix = c.get(name)
        if not isinstance(prefix, list) or not 1 <= len(prefix) <= 2 or not all(isinstance(x, str) and x and not x.startswith("-") for x in prefix):
            raise Blocked(f"{name} must be a native executable, optionally followed by a JS entrypoint")
        if len(prefix) == 2 and not prefix[1].endswith(".js"):
            raise Blocked("Command prefix arguments may not override sandbox flags")
    from .workers import validate_worker_config
    validate_worker_config(c)
    return c
