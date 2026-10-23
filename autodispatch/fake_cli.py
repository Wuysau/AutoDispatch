"""Offline disposable Agent/test executable. Never import a real adapter here."""
import argparse
import json
import runpy
import sys
import traceback
import xml.etree.ElementTree as ET
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("stage")
    p.add_argument("payload")
    args = p.parse_args()
    data = json.loads(args.payload)
    root = Path.cwd()
    fault = data.get("fault")
    if fault == "quota":
        print("quota exhausted")
        return 29
    if args.stage == "red":
        (root / "test_behavior.py").write_text(
            "from pathlib import Path\nnamespace = {}\np = Path('implementation.py')\n"
            "if p.exists():\n    exec(p.read_text(), namespace)\n"
            "actual = namespace.get('answer', lambda: None)()\nassert actual == 42, 'expected real behavior 42'\n",
            encoding="utf-8")
        print(json.dumps({"status": "RED_READY", "simulated": True}))
    elif args.stage == "implement":
        if not (root / "test_behavior.py").exists():
            (root / "test_behavior.py").write_text("from implementation import answer\nassert answer() == 42\n", encoding="utf-8")
        (root / "implementation.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
        print(json.dumps({"status": "CODE_READY_VERIFY_PENDING", "simulated": True}))
    elif args.stage == "test":
        suite = ET.Element("testsuite")
        case = ET.SubElement(suite, "testcase", name="behavior")
        code = 0
        try:
            sys.path.insert(0, str(root))
            if fault == "environment":
                raise ModuleNotFoundError("fake unavailable dependency")
            if fault == "test-failed":
                raise AssertionError("simulated regression failure")
            runpy.run_path(str(root / "test_behavior.py"))
        except AssertionError as exc:
            code = 1
            ET.SubElement(case, "failure", message="AssertionError").text = str(exc)
            traceback.print_exc()
        except Exception as exc:
            code = 2
            ET.SubElement(case, "error", message=type(exc).__name__).text = str(exc)
        ET.ElementTree(suite).write(data["report"], encoding="utf-8")
        print(json.dumps({"simulated": True, "machine_test_exit": code}))
        return code
    else:
        status = "CHANGES_REQUESTED" if fault == "review-failed" else "APPROVED"
        print(json.dumps({"task_id": data["task_id"], "status": status,
                          "findings": ["simulated finding"] if status != "APPROVED" else [],
                          "commit_sha": data["sha"], "verified_commands": ["fake offline behavior test"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
