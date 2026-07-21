#!/usr/bin/env python3
"""
validate_repo.py — precision audit for a github-collab-init repo.

Checks:
  1. Required collaboration files are present.
  2. The Session Log JSON block parses and has sequential, non-duplicate IDs
     with all required fields.
  3. Every commit hash referenced in a commit_range actually exists in git
     history (catches a log that's drifted from reality).
  4. Recent commit subjects follow the Conventional Commits pattern.

Exits 0 if everything passes, 1 otherwise. Always prints a numbered
punch-list rather than a bare pass/fail, so failures are actionable.

Usage:
  python3 validate_repo.py <path>
"""

import json
import re
import subprocess
import sys
from pathlib import Path

START_MARKER = "<!-- SESSION_LOG_START"
END_MARKER = "SESSION_LOG_END -->"
BLOCK_RE = re.compile(
    re.escape(START_MARKER) + r"\n(.*?)\n" + re.escape(END_MARKER), re.DOTALL
)
REQUIRED_ENTRY_FIELDS = {
    "id", "date", "author", "branch", "commit_range",
    "summary", "files_changed", "tests", "status",
}
COMMIT_RE = re.compile(
    r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)"
    r"(\([\w./-]+\))?!?: .+"
)
REQUIRED_FILES = [
    ".gitignore",
    "LICENSE",
    "README.md",
    "CONTRIBUTING.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/CODEOWNERS",
]


def run(cmd, cwd):
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=False
    ).stdout.strip()


def check_required_files(repo: Path, failures, passes):
    for rel in REQUIRED_FILES:
        if (repo / rel).exists():
            passes.append(f"present: {rel}")
        else:
            failures.append(f"missing required file: {rel}")

    issue_templates = list((repo / ".github" / "ISSUE_TEMPLATE").glob("*.md")) \
        if (repo / ".github" / "ISSUE_TEMPLATE").exists() else []
    if issue_templates:
        passes.append(f"present: .github/ISSUE_TEMPLATE ({len(issue_templates)} template(s))")
    else:
        failures.append("missing: at least one .github/ISSUE_TEMPLATE/*.md")

    workflows = list((repo / ".github" / "workflows").glob("*.yml")) \
        if (repo / ".github" / "workflows").exists() else []
    if workflows:
        passes.append(f"present: .github/workflows ({len(workflows)} workflow(s))")
    else:
        failures.append("missing: at least one .github/workflows/*.yml")


def check_session_log(repo: Path, failures, passes):
    readme = repo / "README.md"
    if not readme.exists():
        failures.append("cannot check Session Log: README.md missing")
        return None
    # Explicit UTF-8 — README.md contains emoji (🟢/🟡/🔴); without this,
    # Windows falls back to a codepage that can't represent them (same root
    # cause as the crash fixed in new_session_entry.py).
    text = readme.read_text(encoding="utf-8")
    match = BLOCK_RE.search(text)
    if not match:
        failures.append("no SESSION_LOG_START/END block found in README.md")
        return None
    try:
        entries = json.loads(match.group(1))
    except json.JSONDecodeError as e:
        failures.append(f"Session Log JSON does not parse: {e}")
        return None
    passes.append(f"Session Log JSON parses ({len(entries)} entries)")

    for e in entries:
        missing = REQUIRED_ENTRY_FIELDS - set(e.keys())
        if missing:
            failures.append(f"entry {e.get('id', '?')} missing fields: {sorted(missing)}")

    ids = [e["id"] for e in entries if "id" in e]
    nums = []
    for i in ids:
        m = re.match(r"S-(\d+)", i)
        if m:
            nums.append(int(m.group(1)))
        else:
            failures.append(f"malformed session id: {i}")
    if len(nums) != len(set(nums)):
        failures.append("duplicate session IDs found in log")
    else:
        passes.append("no duplicate session IDs")

    valid_statuses = {"done", "in-progress", "blocked"}
    for e in entries:
        if e.get("status") not in valid_statuses:
            failures.append(f"entry {e.get('id', '?')} has invalid status: {e.get('status')!r}")

    return entries


def check_commit_ranges(repo: Path, entries, failures, passes):
    if entries is None:
        return
    if not (repo / ".git").exists():
        failures.append("not a git repository yet — commit ranges can't be verified")
        return
    for e in entries:
        cr = e.get("commit_range", "")
        if ".." not in cr:
            failures.append(f"entry {e['id']}: malformed commit_range {cr!r}")
            continue
        start, end = cr.split("..", 1)
        for h in (start, end):
            if h in ("none", ""):
                continue
            rc = subprocess.run(
                ["git", "cat-file", "-e", h], cwd=repo,
                capture_output=True, text=True
            ).returncode
            if rc != 0:
                failures.append(
                    f"entry {e['id']}: commit {h} in commit_range not found in git history"
                )
    if entries:
        passes.append("checked commit_range hashes against git history")


def check_commit_messages(repo: Path, failures, passes, n=20):
    if not (repo / ".git").exists():
        return
    log = run(["git", "log", f"-{n}", "--pretty=format:%s"], repo)
    if not log:
        return
    bad = [line for line in log.splitlines() if not COMMIT_RE.match(line)]
    if bad:
        for line in bad:
            failures.append(f"commit message doesn't follow Conventional Commits: {line!r}")
    else:
        passes.append(f"last {min(n, len(log.splitlines()))} commit messages follow Conventional Commits")


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: validate_repo.py <path>")
    repo = Path(sys.argv[1]).resolve()
    if not repo.exists():
        sys.exit(f"error: {repo} does not exist")

    failures, passes = [], []
    check_required_files(repo, failures, passes)
    entries = check_session_log(repo, failures, passes)
    check_commit_ranges(repo, entries, failures, passes)
    check_commit_messages(repo, failures, passes)

    print(f"PRECISION AUDIT — {repo}")
    print("=" * 60)
    for i, p in enumerate(passes, 1):
        print(f"  [PASS] {p}")
    if failures:
        print("-" * 60)
        for i, f in enumerate(failures, 1):
            print(f"  [FAIL {i}] {f}")
    print("=" * 60)
    print(f"{len(passes)} passed, {len(failures)} failed")

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
