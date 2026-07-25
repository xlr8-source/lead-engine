"""
api/redaction.py

Response redaction for read-only deployments.

The dashboard carries personal data: named individuals at Irish regulated
firms, their direct email addresses and phone numbers, and outreach drafts
written to them by name. Under GDPR that data should only reach viewers with
a lawful basis to see it, so a deployment that widens the audience beyond
the operator redacts it.

Company-level facts (legal name, county, CRO status, scores, angles) are
business data and stay visible — the point of the demo is to show those.
"""
from __future__ import annotations

import copy
import re

REDACTED = "[redacted]"

# Keys carrying information about a named person, wherever they appear.
_PII_KEYS = (
    "name", "full_name", "email", "phone", "linkedin_url",
    "detail", "subject", "body",
)


def _redact_contact(contact: dict) -> dict:
    """Blank the identifying fields, keep the structural ones.

    `role` is deliberately preserved: "we found a Director" is the useful,
    non-identifying signal, and keeping it means the UI still renders a
    sensible card rather than an empty one.
    """
    if not isinstance(contact, dict):
        return contact
    out = dict(contact)
    for key in ("name", "full_name", "email", "phone", "linkedin_url", "detail"):
        if out.get(key):
            out[key] = REDACTED
    return out


def _collect_identifiers(detail: dict) -> list[str]:
    """Every string that identifies a person in this payload.

    Gathered before redaction, because the same values are quoted verbatim in
    the model's own prose — `opportunity_signal.decision_maker_access.reason`
    and `personalisation.reference_points` both name the contact — and
    blanking the structured fields alone leaves those untouched.
    """
    sources = []
    sources.extend(detail.get("contacts") or [])
    enrichment = detail.get("enrichment")
    if isinstance(enrichment, dict):
        na = enrichment.get("narrative_assessment")
        if isinstance(na, dict):
            sources.extend(na.get("contacts") or [])

    identifiers: list[str] = []
    for c in sources:
        if not isinstance(c, dict):
            continue
        for key in ("full_name", "name", "email", "phone", "linkedin_url"):
            value = c.get(key)
            if isinstance(value, str) and len(value.strip()) >= 4:
                identifiers.append(value.strip())

    # Longest first so "John Murphy" is replaced before a bare "John" would
    # leave a dangling surname behind.
    return sorted(set(identifiers), key=len, reverse=True)


def _scrub(text: str, identifiers: list[str], protected: str) -> str:
    """Remove identifiers from free text, leaving the company's own name.

    Surnames are deliberately NOT scrubbed on their own: "Murphy" appears in
    "AIDAN MURPHY INSURANCES", and blanking it would mangle the company name
    the demo exists to show. Full names, emails, phones and profile URLs are
    removed as units; a first name is removed only when it does not appear in
    the company's own name.
    """
    out = text
    protected_lower = (protected or "").lower()
    for ident in identifiers:
        out = re.sub(re.escape(ident), REDACTED, out, flags=re.IGNORECASE)
        if " " in ident:
            first = ident.split()[0]
            if len(first) >= 3 and first.lower() not in protected_lower:
                out = re.sub(rf"\b{re.escape(first)}\b", REDACTED, out, flags=re.IGNORECASE)
    return out


def _scrub_tree(node, identifiers: list[str], protected: str):
    """Walk the payload and scrub every free-text string in place."""
    if isinstance(node, dict):
        return {k: _scrub_tree(v, identifiers, protected) for k, v in node.items()}
    if isinstance(node, list):
        return [_scrub_tree(v, identifiers, protected) for v in node]
    if isinstance(node, str):
        return _scrub(node, identifiers, protected)
    return node


def redact_company_detail(detail: dict) -> dict:
    """Return a copy of a company-detail payload with contact PII removed."""
    if not isinstance(detail, dict):
        return detail

    out = copy.deepcopy(detail)
    identifiers = _collect_identifiers(out)

    out["contacts"] = [_redact_contact(c) for c in (out.get("contacts") or [])]

    enrichment = out.get("enrichment")
    if isinstance(enrichment, dict):
        na = enrichment.get("narrative_assessment")
        if isinstance(na, dict) and isinstance(na.get("contacts"), list):
            na["contacts"] = [_redact_contact(c) for c in na["contacts"]]

    # An outreach draft is addressed to a named person and quotes them
    # throughout; there is no partial redaction of a free-text body that can
    # be trusted, so the whole draft is withheld.
    email = out.get("email")
    if isinstance(email, dict):
        out["email"] = {
            **{k: v for k, v in email.items() if k not in ("subject", "body")},
            "subject": REDACTED,
            "body": REDACTED,
        }

    # Second pass over the model's free text, where the same names are quoted
    # in prose. Structured redaction alone left these visible.
    if identifiers:
        protected = " ".join(
            str(out.get(k) or "") for k in ("legal_name", "trading_name")
        )
        enrichment = out.get("enrichment")
        if isinstance(enrichment, dict):
            out["enrichment"] = _scrub_tree(enrichment, identifiers, protected)

    return out
