from __future__ import annotations

import json
import re
from pathlib import Path

from .model import Blocked
from .process import Runner, checked


class Git:
    def __init__(self, runner: Runner, timeout=30):
        self.runner, self.timeout = runner, timeout

    def run(self, repo: Path, *args: str, tid="git"):
        return self.runner.run(["git", "-c", "core.autocrlf=false", "-c", "core.quotePath=false",
                               "-c", "core.whitespace=blank-at-eol,blank-at-eof,space-before-tab,cr-at-eol", *args],
                               repo, self.timeout, tid=tid, stage="git")

    def text(self, repo, *args, tid="git"):
        return checked(self.run(repo, *args, tid=tid))

    def sha(self, repo, ref="HEAD"):
        value = self.text(repo, "rev-parse", "--verify", ref)
        if not re.fullmatch(r"[a-f0-9]{40}", value):
            raise Blocked("Invalid Git SHA")
        return value

    def clean(self, repo):
        if self.text(repo, "status", "--porcelain", "--untracked-files=all"):
            raise Blocked(f"Unexpected dirty worktree: {repo}")

    def ancestor(self, repo, older, newer):
        if not all(re.fullmatch(r"[a-f0-9]{40}", v) for v in (older, newer)):
            return False
        return self.run(repo, "merge-base", "--is-ancestor", older, newer).code == 0

    def remote_head(self, repo):
        text = self.text(repo, "ls-remote", "--exit-code", "origin", "refs/heads/dev")
        sha, ref = text.split()
        if ref != "refs/heads/dev" or not re.fullmatch(r"[a-f0-9]{40}", sha):
            raise Blocked("Cannot establish live origin/dev")
        return sha

    def refresh(self, repo):
        self.text(repo, "fetch", "--no-tags", "origin", "dev")
        base = self.sha(repo, "origin/dev")
        if base != self.remote_head(repo):
            raise Blocked("origin/dev moved while fetching")
        return base

    def create(self, repo, path, sha, branch=None):
        if path.exists():
            raise Blocked(f"Worktree path already exists: {path}")
        args = ["worktree", "add"]
        args += ["-b", branch] if branch else ["--detach"]
        self.text(repo, *args, str(path), sha)
        self.clean(path)

    def changed(self, repo, base):
        return self.text(repo, "diff", "--name-only", "--no-renames", f"{base}...HEAD").splitlines()


class GitHub:
    def __init__(self, runner: Runner, repo: Path, timeout=30, configured_checks=()):
        self.runner, self.repo, self.timeout = runner, repo, timeout
        self.configured_checks = set(configured_checks)

    def call(self, *args):
        return json.loads(checked(self.runner.run(["gh", *args], self.repo, self.timeout, stage="github")))

    def pr(self, branch):
        fields = "number,url,state,isDraft,headRefOid,baseRefName,mergeCommit,reviewDecision,statusCheckRollup"
        prs = self.call("pr", "list", "--repo", "Wuysau/AetherTrade", "--head", branch,
                        "--base", "dev", "--state", "all", "--json", fields)
        if len(prs) > 1:
            raise Blocked("Multiple PRs for task branch; reconcile manually")
        return prs[0] if prs else None

    def required(self):
        # A failed/forbidden API call is UNKNOWN, never evidence of absent protection.
        branch = self.call("api", "repos/Wuysau/AetherTrade/branches/dev")
        rules = self.call("api", "repos/Wuysau/AetherTrade/rules/branches/dev")
        if type(branch.get("protected")) is not bool or not isinstance(rules, list):
            raise Blocked("Cannot establish branch protection state")
        names = set(self.configured_checks)
        for rule in rules:
            if rule.get("type") == "required_status_checks":
                names |= {x["context"] for x in rule["parameters"]["required_status_checks"]}
        if branch["protected"]:
            # Includes classic protection. Missing API permission blocks even if rulesets were readable.
            result = self.call("api", "repos/Wuysau/AetherTrade/branches/dev/protection")
            checks = result.get("required_status_checks") or {}
            names |= set(checks.get("contexts", [])) | {x["context"] for x in checks.get("checks", [])}
        return sorted(names)

    @staticmethod
    def ci_passed(pr, required):
        if not required or not pr:
            return False
        checks = {x.get("name", x.get("context")): x for x in pr.get("statusCheckRollup", [])}
        return all(name in checks and
                   (checks[name].get("conclusion") == "SUCCESS" or checks[name].get("state") == "SUCCESS")
                   for name in required)

    def draft(self, branch, title, body_file):
        # Query first. Recovery also queries; never blindly create a second PR.
        existing = self.pr(branch)
        if existing:
            return existing
        result = self.runner.run(["gh", "pr", "create", "--repo", "Wuysau/AetherTrade", "--draft",
                                  "--base", "dev", "--head", branch, "--title", title,
                                  "--body-file", str(body_file)], self.repo, self.timeout, stage="draft-pr")
        checked(result)
        return self.pr(branch)
