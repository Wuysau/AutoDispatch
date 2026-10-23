from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .model import Blocked


def junit(path: Path):
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    failures = [f for c in cases for f in c.findall("failure")]
    errors = sum(len(c.findall("error")) for c in cases)
    text = " ".join((f.get("message", "") + " " + (f.text or "")) for f in failures)
    environment = len(re.findall(r"ModuleNotFoundError|ImportError|SyntaxError|PermissionError|ConnectionError|fixture .* not found", text))
    assertions = sum("assert" in (f.get("message", "") + (f.text or "")).lower() for f in failures)
    return {"tests": len(cases), "failures": len(failures), "assertion_failures": assertions,
            "errors": errors, "environment_errors": environment,
            "skipped": sum(len(c.findall("skipped")) for c in cases)}


def vitest(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    tests = [a for result in data.get("testResults", []) for a in result.get("assertionResults", [])]
    failed = [a for a in tests if a.get("status") == "failed"]
    text = " ".join(" ".join(a.get("failureMessages", [])) for a in failed)
    env = bool(re.search(r"Cannot find|SyntaxError|ReferenceError|ECONN|ENOENT|Failed to load", text))
    return {"tests": len(tests), "failures": len(failed),
            "assertion_failures": sum(bool(re.search(r"AssertionError|expect\(|expected", " ".join(a.get("failureMessages", [])))) for a in failed),
            "errors": int(data.get("numRuntimeErrorTestSuites", 0)), "environment_errors": int(env),
            "skipped": sum(a.get("status") in {"pending", "skipped", "todo"} for a in tests)}


def green(result):
    stats = result["stats"]
    if (result["exit_code"] != 0 or stats["tests"] < 1 or stats["failures"] or stats["errors"]
            or stats["skipped"] or stats["environment_errors"]):
        raise Blocked("GREEN requires executed passing target tests, no skips/errors")


def receipt_valid(receipt: dict) -> bool:
    from .model import digest
    try:
        return (Path(receipt["log"]).is_file()
                and digest(Path(receipt["log"]).read_bytes()) == receipt["log_sha256"])
    except (KeyError, OSError):
        return False
