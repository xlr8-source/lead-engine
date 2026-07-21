# Security Policy

## Scope

PayBrix Lead Engine is proprietary software, licensed for exclusive use by Sremium Limited and its members (see [LICENSE](LICENSE)). This policy covers the code in this repository — not the deployed production environment, infrastructure, or third-party services it integrates with (CBI/CRO registries, Tavily, NVIDIA/Groq LLM providers), which have their own security boundaries and reporting channels.

## Reporting a vulnerability

Do not open a public GitHub issue for a security concern — that discloses it to everyone with repo access before it's fixed.

Instead:
1. If GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability) is enabled on this repo, use it (Security tab -> Report a vulnerability).
2. Otherwise, contact the maintainer directly: @xlr8-source, or [add a dedicated security contact email here before this repo is shared beyond its current maintainers].

Include what you found, how to reproduce it, and its likely impact. Expect acknowledgement within [agree on a team SLA] and a fix timeline once triaged.

## In scope

- Credential and secret handling (`.env`, API keys, the Tavily/LLM provider keys this project uses)
- Exposure of CRO/CBI company records, extracted contact information, or generated assessments beyond their intended audience
- Injection, authentication, or authorization issues in `api/` endpoints
- Supply-chain issues in dependencies (`requirements.txt`)

## Out of scope

- Findings that only demonstrate access already granted to a Sremium Limited member under the [LICENSE](LICENSE)
- Denial-of-service against your own local instance via excessive automated requests
- Vulnerabilities in third-party providers themselves (report those directly to NVIDIA, Groq, or Tavily)

## Secrets handling

`.env` is gitignored and must never be committed. If a secret is accidentally committed or otherwise exposed, treat it as compromised and rotate it immediately — deleting the file in a later commit does not undo the exposure, since it remains in git history.

## Supported versions

This is a single-branch (`main`), continuously-developed internal tool — there is no version support matrix. Security fixes land on `main` and are expected to be pulled immediately by every Sremium Limited member running this software.
