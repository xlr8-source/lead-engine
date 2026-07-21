# Code of Conduct

## Scope

This Code of Conduct applies to everyone interacting within the PayBrix Lead Engine repository and its associated spaces — issues, pull requests, code review, and commit history — access to which is restricted, per the [LICENSE](LICENSE), to Sremium Limited and its authorized members. This project does not accept outside contributions, so this document governs internal collaboration rather than a public community.

## Expected behavior

- Communicate directly and respectfully in reviews, issues, and commit discussions — critique the work, not the person.
- Assume good intent, but say clearly when something is wrong, unsafe, or doesn't meet the bar. Vague feedback wastes everyone's time and lets real problems through.
- Attribute work honestly in commit messages and the Session Log (see [CONTRIBUTING.md](CONTRIBUTING.md)) rather than folding someone else's changes into your own without credit.
- Treat the data this project handles — company registry records, contact details sourced during enrichment — with the same care you'd want for your own. It describes real firms and real people, not synthetic test fixtures.

## Unacceptable behavior

- Harassment, discriminatory language, or personal attacks directed at any contributor, human or AI agent.
- Committing secrets, credentials, or another person's private data (see [SECURITY.md](SECURITY.md)).
- Bypassing review, branch protection, or the license's use restrictions to grant access to anyone outside Sremium Limited.
- Deliberately introducing misleading or fabricated data into assessments, contacts, or the audit trail. This tool exists to give sales accurate information — a hallucinated contact or an invented score defeats that purpose (see `engine/research/contact_quality.py` for the concrete, hard-won reasons this matters here specifically).

## Enforcement

Report unacceptable behavior to the repository owner (@xlr8-source) directly. Confirmed violations may result in review of the individual's continued access under the terms of the [LICENSE](LICENSE), up to and including revocation, at Sremium Limited's discretion.

## Attribution

Written for this repository's actual shape — an internally-restricted, proprietary, currently single-maintainer project — rather than adapted from a public open-source community template.
