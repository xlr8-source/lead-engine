# Contributing to PayBrix Lead Engine

## Branch naming

```
<type>/<short-description>
```
Examples: `feat/mcp-config`, `fix/npx-path`, `docs/readme-session-log`.

## Commits

This repo uses [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <summary>

Session: S-000X
```

Types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`. The `Session: S-000X` footer links the commit to its Session Log entry in `README.md` — see `references/commit_convention.md` in the skill (or ask whoever set up the repo) for the full cheat sheet.

## Before opening a PR

1. Rebase on the latest `main`.
2. Run the precision audit: `python3 scripts/validate_repo.py .` — fix anything it flags.
3. Add a Session Log entry if you haven't already this session: `python3 scripts/new_session_entry.py --author "<you>" --summary "<what changed>" --status done`.
4. Fill out the PR template — don't delete sections, mark them N/A if they don't apply.
5. Request review from the relevant `CODEOWNERS` entry for the paths you touched.

## Code review expectations

- Reviewers respond within [agree on a team SLA — e.g. 24h].
- At least one approval required before merge (adjust in branch protection settings on GitHub — this file doesn't enforce it, the repo settings do).
- Squash-merge preferred, so `main` history stays one Conventional Commit per PR.

## Reporting issues

Use the issue templates in `.github/ISSUE_TEMPLATE/` — bug reports and feature requests are separate so triage is faster.
