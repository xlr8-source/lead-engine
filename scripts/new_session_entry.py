#!/usr/bin/env python3
"""
new_session_entry.py — append one Session Log entry to README.md.

Reads the existing JSON block between the SESSION_LOG_START/END markers,
computes the real commit range and changed-file list from git (unless
--no-git is passed), assigns the next sequential Session ID, prepends the
new entry, and rewrites both the JSON block and the human-readable table.

Usage:
  python3 new_session_entry.py --repo . --author "taha" \\
      --summary "Wired Desktop Commander MCP config" --status in-progress

  python3 new_session_entry.py --repo . --author "taha" --no-git \\
      --summary "First pass, nothing committed yet" --status in-progress \\
      --files "claude_desktop_config.json,README.md"
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

START_MARKER = "<!-- SESSION_LOG_START"
END_MARKER = "SESSION_LOG_END -->"
BLOCK_RE = re.compile(
    re.escape(START_MARKER) + r"\n(.*?)\n" + re.escape(END_MARKER), re.DOTALL
)
STATUS_EMOJI = {"done": "🟢", "in-progress": "🟡", "blocked": "🔴"}


def run(cmd, cwd):
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=False
    ).stdout.strip()


def git_info(repo_path: Path, last_hash: Optional[str]):
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo_path) or "unknown"
    head = run(["git", "rev-parse", "HEAD"], repo_path)
    if not head:
        return branch, "none..none", []
    if last_hash and last_hash not in ("none", ""):
        commit_range = f"{last_hash}..{head}"
        files_raw = run(["git", "diff", "--name-only", last_hash, head], repo_path)
    else:
        commit_range = f"none..{head}"
        files_raw = run(
            ["git", "show", "--name-only", "--pretty=format:", head], repo_path
        )
    files = [f for f in files_raw.splitlines() if f.strip()]
    return branch, commit_range, files


def load_entries(readme_text: str):
    match = BLOCK_RE.search(readme_text)
    if not match:
        return None, None
    entries = json.loads(match.group(1))
    return entries, match


def next_id(entries):
    if not entries:
        return "S-0001"
    last_num = max(int(e["id"].split("-")[1]) for e in entries)
    return f"S-{last_num + 1:04d}"


def render_table(entries):
    header = "| Session | Date (UTC) | Author | Branch | Summary | Status |\n|---|---|---|---|---|---|"
    rows = []
    for e in entries:
        emoji = STATUS_EMOJI.get(e["status"], "⚪")
        date_human = e["date"].replace("T", " ").replace("Z", "")
        rows.append(
            f"| {e['id']} | {date_human} | {e['author']} | {e['branch']} | "
            f"{e['summary']} | {emoji} {e['status']} |"
        )
    return header + "\n" + "\n".join(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".", help="Path to the repo (default: cwd)")
    ap.add_argument("--author", required=True)
    ap.add_argument("--summary", required=True)
    ap.add_argument(
        "--status", required=True, choices=["done", "in-progress", "blocked"]
    )
    ap.add_argument("--tests", default="not recorded")
    ap.add_argument("--no-git", action="store_true", help="Skip git introspection")
    ap.add_argument(
        "--files", default="", help="Comma-separated file list (required with --no-git)"
    )
    args = ap.parse_args()

    repo_path = Path(args.repo).resolve()
    readme_path = repo_path / "README.md"
    if not readme_path.exists():
        sys.exit(f"error: {readme_path} not found — run scaffold_repo.sh first")

    # Explicit UTF-8: README.md and this script's own STATUS_EMOJI table
    # (🟢/🟡/🔴) are UTF-8. Without an explicit encoding, Path.read_text()/
    # write_text() fall back to locale.getpreferredencoding(), which on
    # Windows is typically a legacy codepage (cp1252) that can't represent
    # those emoji — read_text() there can mis-decode existing entries, and
    # write_text() crashes outright (confirmed: it raised UnicodeEncodeError
    # and truncated README.md to empty before this fix, since write_text()
    # opens the file in truncating mode before the encode error surfaces).
    readme_text = readme_path.read_text(encoding="utf-8")
    entries, match = load_entries(readme_text)
    if entries is None:
        sys.exit(
            "error: no SESSION_LOG_START/END block found in README.md — "
            "run scaffold_repo.sh first, or add the block manually per "
            "references/session_log_schema.md"
        )

    last_hash = None
    if entries:
        prev_range = entries[0].get("commit_range", "")
        if ".." in prev_range:
            last_hash = prev_range.split("..")[-1]

    if args.no_git:
        branch = "unknown"
        commit_range = "none..none"
        files = [f.strip() for f in args.files.split(",") if f.strip()]
        if not files:
            print(
                "warning: --no-git with no --files given — files_changed will be empty",
                file=sys.stderr,
            )
    else:
        branch, commit_range, files = git_info(repo_path, last_hash)

    new_entry = {
        "id": next_id(entries),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "author": args.author,
        "branch": branch,
        "commit_range": commit_range,
        "summary": args.summary,
        "files_changed": files,
        "tests": args.tests,
        "status": args.status,
    }

    entries.insert(0, new_entry)

    new_json_block = json.dumps(entries, indent=2)
    new_block = f"{START_MARKER}\n{new_json_block}\n{END_MARKER}"
    readme_text = readme_text[: match.start()] + new_block + readme_text[match.end() :]

    # Replace the table immediately following the block, if present.
    table_re = re.compile(
        r"(\| Session \| Date \(UTC\) \| Author \| Branch \| Summary \| Status \|\n\|---\|---\|---\|---\|---\|---\|\n(?:\|.*\|\n?)*)"
    )
    new_table = render_table(entries)
    if table_re.search(readme_text):
        readme_text = table_re.sub(new_table + "\n", readme_text, count=1)
    else:
        # No existing table found right after the block — append one.
        insert_at = readme_text.find(END_MARKER) + len(END_MARKER)
        readme_text = (
            readme_text[:insert_at] + "\n\n" + new_table + "\n" + readme_text[insert_at:]
        )

    readme_path.write_text(readme_text, encoding="utf-8")
    print(f"Added {new_entry['id']} to {readme_path}")
    print(json.dumps(new_entry, indent=2))


if __name__ == "__main__":
    main()
