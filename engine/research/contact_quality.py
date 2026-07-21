"""
engine/research/contact_quality.py

Contact plausibility filtering — the gate between contact *extraction*
(regex or LLM) and contact *storage*.

Field evidence that motivated this (2026-07 engine run): contacts stored as
"Retired Unemployed Your Partner" and "Retired Unemployed What" (LinkedIn
search-result title fragments and page marketing copy), and "hello" (the
local-part of hello@domain christened as a person). The governor's Contact
schema validates *shape* only, so schema-valid junk reached the sales UI.

Design tradeoff, stated openly: the junk-token lexicon will very rarely
reject a real person whose surname collides with web-copy vocabulary
(e.g. the rare surname "Read"). At B2B lead quality, a rare false drop is
far cheaper than a hallucinated contact reaching a salesperson — and the
drop is logged, never silent.
"""
from typing import Optional

GENERIC_INBOX_NAME = "General company inbox"

# Local-parts that denote a shared company mailbox, not a person.
GENERIC_INBOX_LOCALPARTS = {
    "hello", "info", "office", "admin", "administrator", "contact",
    "contactus", "enquiries", "enquiry", "inquiries", "inquiry", "mail",
    "post", "reception", "sales", "support", "team", "accounts", "help",
    "service", "services", "quotes", "quote", "claims", "general",
    "welcome", "webmaster", "noreply", "no-reply",
}

# Tokens that do not occur in real person names but constantly occur in the
# page furniture both extractors read (nav labels, taglines, marketing copy,
# LinkedIn title fragments). Any one of these in a candidate name rejects it.
_JUNK_NAME_TOKENS = {
    # the exact field-note failures
    "what", "hello", "retired", "unemployed", "your", "partner", "partners",
    # navigation / page furniture
    "welcome", "home", "about", "contact", "contacts", "team", "our", "the",
    "menu", "page", "site", "click", "here", "more", "read", "learn", "get",
    "touch", "us", "we", "you", "meet", "staff", "people",
    # legal / marketing boilerplate
    "privacy", "policy", "cookie", "cookies", "terms", "conditions",
    "testimonial", "testimonials", "faq", "faqs", "blog", "news", "careers",
    # sector words that mark a company/tagline, not a person
    "insurance", "insurances", "broker", "brokers", "financial", "services",
    "service", "limited", "ltd", "group", "company", "quote", "quotes",
    "claim", "claims", "call", "email", "phone", "info",
}

_NAME_TOKEN_MIN_LEN = 2
_NAME_MIN_TOKENS = 2
_NAME_MAX_TOKENS = 5


def is_generic_inbox(email: Optional[str]) -> bool:
    """True if `email` is a shared company mailbox (info@/hello@ style)."""
    if not email or "@" not in email:
        return False
    local = email.split("@", 1)[0].strip().lower()
    local = local.replace(".", "").replace("_", "").replace("-", "")
    return local in GENERIC_INBOX_LOCALPARTS


def _token_looks_like_name_part(token: str) -> bool:
    if len(token) < _NAME_TOKEN_MIN_LEN:
        return False
    if not token[0].isalpha() or not token[0].isupper():
        return False
    # Letters plus the punctuation real names carry (O'Brien, Anne-Marie, J.)
    return all(ch.isalpha() or ch in "'’.-" for ch in token)


def is_plausible_person_name(name: Optional[str]) -> bool:
    """True if `name` is shaped like a real person's full name and carries no
    page-furniture vocabulary."""
    if not name:
        return False
    tokens = name.strip().split()
    if not (_NAME_MIN_TOKENS <= len(tokens) <= _NAME_MAX_TOKENS):
        return False
    for token in tokens:
        if token.strip("'’.-").lower() in _JUNK_NAME_TOKENS:
            return False
        if not _token_looks_like_name_part(token):
            return False
    return True


def _relabel_as_inbox(contact: dict) -> dict:
    """Return a copy of `contact` honestly labeled as a shared mailbox."""
    relabeled = dict(contact)
    email = relabeled.get("email")
    relabeled["name"] = GENERIC_INBOX_NAME
    relabeled["role"] = None
    relabeled["detail"] = (
        f"Generic inbox address ({email}) published on the website — "
        "a usable company channel, not a named person."
    )
    confidence = relabeled.get("confidence")
    if isinstance(confidence, dict):
        confidence = {
            k: (dict(v) if isinstance(v, dict) else v) for k, v in confidence.items()
        }
        confidence["identity"] = {
            "level": "low",
            "reason": "Generic shared mailbox (info@/hello@ style), not a named individual.",
        }
        confidence["overall"] = {
            "level": "low",
            "reason": "Usable company channel, but no named person behind it.",
        }
        relabeled["confidence"] = confidence
    return relabeled


def sanitise_contacts(contacts: list) -> tuple[list, list[str]]:
    """Filter a contact list down to plausible entries.

    Returns (kept, dropped_reasons):
      - plausible person names pass through (deduplicated by name);
      - implausible names carrying a generic-inbox email are relabeled as
        '{GENERIC_INBOX_NAME}' rather than dropped (deduplicated by email);
      - everything else is dropped, one human-readable reason per drop.
    """
    kept: list = []
    dropped: list[str] = []
    seen_people: set[str] = set()
    seen_inboxes: set[str] = set()

    for contact in contacts or []:
        if not isinstance(contact, dict):
            dropped.append(f"non-dict contact entry: {contact!r}")
            continue
        name = (contact.get("name") or "").strip()
        email = contact.get("email")

        if is_plausible_person_name(name):
            key = name.lower()
            if key in seen_people:
                dropped.append(f"duplicate contact name: '{name}'")
                continue
            seen_people.add(key)
            kept.append(contact)
        elif email and is_generic_inbox(email):
            key = email.strip().lower()
            if key in seen_inboxes:
                dropped.append(f"duplicate generic inbox: '{email}'")
                continue
            seen_inboxes.add(key)
            kept.append(_relabel_as_inbox(contact))
        else:
            dropped.append(
                f"implausible contact name: '{name}'"
                + (f" (email: {email})" if email else "")
            )

    return kept, dropped
