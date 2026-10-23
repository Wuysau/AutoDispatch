from __future__ import annotations

import ast
import fnmatch
import json
import re
import shlex
from pathlib import Path

from .model import Blocked, Task, digest, relative, within

SECRET = re.compile(r"(?i)(?:sk-[a-z0-9_-]{16,}|(?:gh[pousr]_[a-z0-9_]{16,})|"
                    r"(?:api[_-]?key|secret|password|authorization|access[_-]?token)\s*[\"']?\s*[:=]\s*[^\s,}\n]+)")
SECRET_LITERAL = re.compile(r"(?i)(?:sk-[a-z0-9_-]{16,}|gh[pousr]_[a-z0-9_]{16,}|"
                            r"(?:api[_-]?key|secret|password|authorization|access[_-]?token)[\"']?\s*[:=]\s*[\"'][^\"'\r\n]{8,}[\"'])")
SECRET_FIELD = re.compile(r"(?i)^(?:api[_-]?key|secret|password|authorization|access[_-]?token)$")


def has_secret_literal(text: str, path: str) -> bool:
    """Allow one public sentinel in test data, never arbitrary test credentials.

    Only a literal ``fictional`` value of a credential-named dict field or
    assignment inside a test_* function in tests/**/test_*.py is exempted.
    Production code, module globals, unknown values, tokens, and invalid Python
    retain the original strict scan. This changes recognition, not source bytes
    or log redaction.
    """
    if not SECRET_LITERAL.search(text):
        return False
    file = Path(path)
    if not file.parts or file.parts[0] != 'tests' or not file.name.startswith('test_') or file.suffix != '.py':
        return True
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return True
    raw = text.encode('utf-8')
    lines = raw.splitlines(keepends=True)
    offsets, total = [], 0
    for line in lines:
        offsets.append(total)
        total += len(line)
    masked = bytearray(raw)

    def mask_value(value):
        if not isinstance(value, ast.Constant) or value.value != 'fictional':
            return
        # AST columns count UTF-8 bytes, including on lines containing Chinese.
        start = offsets[value.lineno - 1] + value.col_offset
        end = offsets[value.end_lineno - 1] + value.end_col_offset
        masked[start:end] = b' ' * (end - start)

    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) or not function.name.startswith('test_'):
            continue
        for node in ast.walk(function):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and isinstance(key.value, str) and SECRET_FIELD.fullmatch(key.value):
                        mask_value(value)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(t, ast.Name) and SECRET_FIELD.fullmatch(t.id) for t in targets):
                    mask_value(node.value)
    return bool(SECRET_LITERAL.search(masked.decode('utf-8')))


def redact(text: str) -> str:
    text = SECRET.sub("[REDACTED]", text)
    return re.sub(r"https://[^\s/@]+:[^\s/@]+@", "https://[REDACTED]@", text)


def allowed(path: str, scopes: tuple[str, ...]) -> bool:
    relative(path)
    return any(fnmatch.fnmatchcase(path, s) for s in scopes)


def conflicts(a: Task, b: Task) -> bool:
    for left in a.scope_write:
        for right in b.scope_write:
            l, r = left.casefold(), right.casefold()
            lp, rp = re.split(r"[*?\[]", l)[0], re.split(r"[*?\[]", r)[0]
            if l == r or fnmatch.fnmatchcase(l, r) or fnmatch.fnmatchcase(r, l):
                return True
            if ("*" in l or "*" in r) and (lp.startswith(rp) or rp.startswith(lp)):
                return True
    return False


def check_scope(task: Task, changed: list[str], repo: Path, red: bool = False):
    if not changed:
        raise Blocked("No committed task changes")
    seen = set()
    for p in changed:
        relative(p)
        if p.casefold() in seen:
            raise Blocked("Case-colliding paths")
        seen.add(p.casefold())
        if p.startswith("docs/spec/") or not allowed(p, task.scope_write):
            raise Blocked(f"Scope expansion forbidden: {p}")
        if red and p not in {task.primary_test_file, task.test_support_file}:
            raise Blocked(f"RED may only change tests and fixture: {p}")
        if re.search(r"(?i)(^|/)(\.env(?:\..*)?|credentials?|id_rsa|node_modules|\.venv|target)(/|$)", p):
            raise Blocked(f"Credential/generated file forbidden: {p}")
        target = within(repo, p)
        if target.is_file():
            raw = target.read_bytes()
            if has_secret_literal(raw.decode("utf-8", errors="replace"), p):
                raise Blocked(f"Potential secret in changed file: {p}")


def verification_argv(command: str, python: str, npm: list[str]) -> list[str]:
    # Intentionally parse only the literal, known task command grammar. Never shell=True.
    if re.fullmatch(r"python -m pytest tests/[A-Za-z0-9_/.-]+\.py -v", command):
        p = command.split()[3]
        relative(p)
        return [python, "-m", "pytest", p, "-v"]
    if re.fullmatch(r"npm --prefix web run test -- [A-Za-z0-9_.-]+\.test\.tsx", command):
        return [*npm, "--prefix", "web", "run", "test", "--", command.split()[-1]]
    raise Blocked("Unsupported verify_command: explicit environment adapter required")


def assert_review(report: dict, task: Task, sha: str):
    if set(report) != {"task_id", "status", "findings", "commit_sha", "verified_commands"}:
        raise Blocked("Review schema mismatch")
    if report["task_id"] != task.id or report["commit_sha"] != sha:
        raise Blocked("Review is for another task/commit")
    if not isinstance(report["findings"], list) or not all(isinstance(x, str) for x in report["findings"]):
        raise Blocked("Malformed review findings")
    if report["status"] != "APPROVED":
        raise Blocked("Independent review status: " + str(report["status"]))
    if report["findings"]:
        raise Blocked("Review protocol violation: APPROVED must have an empty list of unresolved findings")
    if not isinstance(report["verified_commands"], list) or not all(isinstance(x, str) for x in report["verified_commands"]):
        raise Blocked("Malformed review commands")


def classify_red(code: int, report: dict) -> bool:
    # Parsed JUnit/Vitest results from the machine test, not Agent prose.
    return (code == 1 and report.get("tests", 0) > 0 and report.get("assertion_failures", 0) > 0
            and report.get("errors", 0) == 0 and report.get("skipped", 0) == 0
            and report.get("environment_errors", 0) == 0)


def approval_fingerprint(c: dict) -> str:
    keys = ["repo", "remote_url", "pins", "manifest_sha256", "worktree_root", "state_dir",
            "opencode_environment", "codex_command", "opencode_command", "npm_command",
            "task_timeout_seconds", "command_timeout_seconds", "max_agent_requests", "max_tasks",
            "max_workers", "regressions", "wsl", "required_checks", "publication_mode", "max_auto_retries",
            "codex_model", "opencode_model", "integration_target", "codex_windows_sandbox"]
    # Preserve existing approvals byte-for-byte; every new option, when present,
    # joins the fingerprint and requires matching execution authorization.
    keys += [key for key in ('worker_model_selection', 'codex_reasoning_effort', 'opencode_variant') if key in c]
    return digest(json.dumps({k: c.get(k) for k in keys}, sort_keys=True).encode())


def require_approval(c: dict, path: Path):
    obj = json.loads(path.read_text(encoding="utf-8"))
    if obj.get("configuration_sha256") != approval_fingerprint(c):
        raise Blocked("Approval does not match the current configuration")
    flags = ["implementation_plan_approved", "allow_product_tasks", "allow_agent_calls",
             "unknown_usage_fixed_request_cap_accepted"]
    if c.get("integration_target") == "local-dev":
        flags += ["allow_local_dev_merge"]
    if c["publication_mode"] == "draft-pr":
        flags += ["allow_task_branch_push", "allow_draft_pr"]
    for key in flags:
        if obj.get(key) is not True:
            raise Blocked(f"Human approval required: {key}")
    if not obj.get("approved_by") or not obj.get("approved_at"):
        raise Blocked("Named, dated human approval required")
    if set(obj.get("authorized_tasks", [])) - {f"IP-{i:02}" for i in range(39)}:
        raise Blocked("Invalid authorized task IDs")
    if not obj.get("authorized_tasks"):
        raise Blocked("No tasks authorized")
    if c["opencode_environment"] == "wsl" and obj.get("wsl_paths_approved") is not True:
        raise Blocked("WSL paths require explicit approval")
    return obj


def wsl_path(value: str) -> str:
    if not value.startswith("/home/") or "/../" in value or "\x00" in value or "\\" in value:
        raise Blocked("WSL needs explicit native /home/... path; no Windows mapping or shared deps")
    return value


def wsl_argv(distro: str, cwd: str, argv: list[str]) -> list[str]:
    wsl_path(cwd)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", distro):
        raise Blocked("Invalid WSL distribution")
    # --cd and --exec avoid bash/cmd parsing entirely.
    return ["wsl.exe", "--distribution", distro, "--cd", cwd, "--exec", *argv]
