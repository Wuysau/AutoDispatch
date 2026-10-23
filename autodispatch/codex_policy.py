"""Explicit per-invocation Codex permissions; never inherit a broader sandbox."""
from __future__ import annotations

import os
import json
from pathlib import Path

from .model import Blocked


def platform_overrides(c: dict) -> list[str]:
    if os.name != "nt":
        return []
    backend = c.get("codex_windows_sandbox")
    if backend not in {"unelevated", "elevated"}:
        raise Blocked("An explicit supported Codex Windows sandbox backend is required")
    return ["-c", f'windows.sandbox="{backend}"']


def exec_argv(c: dict, cwd: Path, mode: str, output: Path, schema: Path | None = None) -> list[str]:
    if mode not in {"read-only", "workspace-write"}:
        raise Blocked("Codex dispatch only permits read-only or workspace-write")
    argv = [*c["codex_command"], "exec", "--ignore-user-config", "--ephemeral",
            *platform_overrides(c), "-c", 'approval_policy="never"', "-C", str(cwd),
            "--sandbox", mode, "--json", "--output-last-message", str(output)]
    if schema:
        argv += ["--output-schema", str(schema)]
    if c.get("codex_model"):
        argv += ["--model", c["codex_model"]]
    if c.get('codex_reasoning_effort'):
        argv += ['-c', 'model_reasoning_effort=' + json.dumps(c['codex_reasoning_effort'])]
    return [*argv, "-"]
