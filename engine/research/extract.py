import re
from urllib.parse import urlparse

from engine.research.contact_quality import GENERIC_INBOX_NAME, is_generic_inbox


_EMAIL_RE_STR = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
_PHONE_RE = re.compile(r"(?:\+353\s?\d{1,2}|\b0\d{1,2})[\s\-/]?\d{3}[\s\-]?\d{3,4}\b")
_CONTACT_FORM_RE = re.compile(
    r"contact us|get in touch|contact form|enquiry form|send us a message",
    re.IGNORECASE,
)


def classify_contact_channel(website_text) -> str:
    """How can this firm actually be reached, per the crawled pages?

    "email" > "phone" > "form_only" > "none". form_only is a first-class,
    honest outcome (the Clear Insurance case: a contact page exists but
    publishes no address/number — sales needs to know that, not see a blank).
    """
    if not website_text:
        return "none"
    if re.search(_EMAIL_RE_STR, website_text):
        return "email"
    if _PHONE_RE.search(website_text):
        return "phone"
    if _CONTACT_FORM_RE.search(website_text):
        return "form_only"
    return "none"


def extract_digital_presence(research: dict) -> dict:
    website_text = research.get("website_text")
    has_website = bool(website_text)
    result = {
        "has_website": has_website,
        "domain": None,
        "pages_crawled": 0,
        "quality_notes": None,
        "contact_channel": classify_contact_channel(website_text),
        "social_links": research.get("social_links") or {},
    }
    if has_website and website_text:
        first_line = website_text.split("\n")[0]
        m = re.search(r"\]\(([^)]+)\)", first_line)
        if m:
            url = m.group(1)
            domain = urlparse(url).netloc
            domain = domain.replace("www.", "")
            result["domain"] = domain
            if result["contact_channel"] == "form_only":
                result["quality_notes"] = (
                    "Website crawled — contact is via web form only; "
                    "no public email address or phone number published."
                )
            else:
                result["quality_notes"] = "Website successfully crawled"
        result["pages_crawled"] = 1 + website_text.count("--- Additional page:")
    else:
        result["quality_notes"] = "No verified official website identified. The research process searched for an official domain but found none confidently attributable to this firm."
    return result


# Maps common CBI authorisation keywords to human-readable descriptions
_CBI_AUTH_LABELS = {
    "insurance intermediary": "CBI-authorised insurance intermediary",
    "investment business": "Authorised investment business firm",
    "investment intermediar": "Authorised investment intermediary",
    "mortgage intermediar": "Authorised mortgage intermediary",
    "mortgage broker": "Authorised mortgage broker",
    "mortgage credit intermediar": "Authorised mortgage credit intermediary",
    "moneylender": "Authorised moneylender",
    "bureau de change": "Authorised bureau de change",
    "credit institution": "Authorised credit institution",
}


def _describe_cbi_auth(auth_str: str) -> str:
    """Return a human-readable description of a CBI authorisation."""
    lower = auth_str.lower()
    for key, label in _CBI_AUTH_LABELS.items():
        if key in lower:
            return label
    return f"CBI authorisation: {auth_str}"


def extract_research_coverage(company: dict, research: dict, digital: dict) -> dict:
    verified = []
    missing = []

    name = company.get("legal_name", "")
    cro_status = company.get("cro_status", "")
    cro_number = company.get("cro_number", "")
    county = company.get("county", "")
    incorp = company.get("incorporation_date", "")
    cbi_ref = company.get("cbi_reference", "")
    company_type = company.get("company_type", "")
    trading_name = company.get("trading_name", "")
    cbi_auths = company.get("cbi_authorisations", [])
    last_return = company.get("last_annual_return", "")

    # Identity
    if cro_number and cro_status:
        verified.append(f"Company active — CRO status {cro_status}")
    if incorp:
        date_str = incorp[:10] if len(incorp) > 10 else incorp
        verified.append(f"Incorporated {_format_date(date_str)}, {county}-based" if county else f"Incorporated {date_str}")
    if cro_number:
        verified.append(f"CRO registration confirmed — {cro_number}")

    # CBI authorisations — list each one
    if isinstance(cbi_auths, list) and cbi_auths:
        for auth in cbi_auths:
            verified.append(_describe_cbi_auth(auth))
    elif cbi_ref:
        verified.append(f"CBI authorisation confirmed — Reference {cbi_ref}")

    # Company type / trading name
    if trading_name:
        verified.append(f"Trading as {trading_name}")
    if last_return:
        verified.append(f"Annual return filed — {last_return[:10] if len(last_return) > 10 else last_return}")

    # Website
    if digital.get("has_website") and digital.get("domain"):
        verified.append(f"Official website identified — {digital['domain']}")

    # Website-derived facts
    website_text = research.get("website_text", "")
    if website_text:
        has_email = bool(re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", website_text))
        has_named_person = bool(re.search(
            r"(?:Director|Founder|Co-founder|CEO|Managing Director|Partner|Owner|Adviser|Advisor|Principal)\s*[:\–-]?\s*[A-Z][a-z]+",
            website_text
        ))
        if has_named_person:
            verified.append("Named directors or advisers identified on website")
        if not has_email:
            missing.append("Public email address")
    else:
        missing.append("Official website or online presence")
        missing.append("Public email address")

    if not cbi_ref and not cbi_auths:
        missing.append("CBI authorisation reference")

    # Payment-related gaps — always present as these are core to the product
    missing.append("Payment provider or gateway in use")
    missing.append("Premium collection workflow")
    missing.append("Reconciliation process across providers")

    return {"verified": verified, "missing": missing}


def _format_date(date_str: str) -> str:
    """Format ISO date string (YYYY-MM-DD) to readable form."""
    try:
        parts = date_str.split("-")
        if len(parts) == 3:
            months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
            month = months[int(parts[1]) - 1]
            return f"{int(parts[2])} {month} {parts[0]}"
    except (ValueError, IndexError):
        pass
    return date_str


def _mechanical_confidence(overall_level: str, overall_reason: str, email: str | None = None,
                            phone: str | None = None, identity_level: str = "medium",
                            identity_reason: str = "Matched via regex pattern, not model reasoning.") -> dict:
    """Build a ContactConfidence-shaped dict for the regex fallback path.
    Reasons are honest about being mechanical, not model-reasoned — this
    fallback only runs when the LLM's own (reasoned) contact extraction
    came back empty/malformed, so its confidence should read as such."""
    def field(level, reason):
        return {"level": level, "reason": reason}
    return {
        "identity": field(identity_level, identity_reason),
        "role": field("low", "Role not reliably distinguishable via regex pattern matching."),
        "email": field("medium" if email else "low", "Email pattern found near this name in page text." if email else "No email pattern found near this name."),
        "phone": field("low", "Regex fallback does not extract phone numbers."),
        "linkedin": field("low", "Regex fallback does not search LinkedIn."),
        "freshness": field("low", "No freshness signal available from regex extraction."),
        "overall": field(overall_level, overall_reason),
    }


def extract_contacts(research: dict, company: dict) -> list:
    contacts = []
    website_text = research.get("website_text", "")
    if not website_text:
        # Fallback: try CRO officers/directors if available
        cro_officers = company.get("cro_officers", []) or company.get("directors", [])
        for officer in cro_officers:
            name = officer.get("name") or officer.get("full_name")
            role = officer.get("role") or officer.get("position", "Director")
            if name:
                contacts.append({
                    "name": name,
                    "role": role,
                    "detail": "Listed as officer in CRO register",
                    "email": None,
                    "phone": None,
                    "linkedin_url": None,
                    "confidence": _mechanical_confidence(
                        "medium", "Name and role confirmed via CRO officer register — a reliable but not contactable source.",
                        identity_level="medium", identity_reason="Listed as a company officer in the CRO register.",
                    ),
                    "source": "cro_register"
                })
        return contacts

    email_pattern = r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})"
    emails = set(re.findall(email_pattern, website_text))

    name_pattern = (
        r"(?:Director|Founder|Co-founder|CEO|Managing Director|Partner|"
        r"Owner|Adviser|Advisor|Principal)\s*(?::|–|-|&)?\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)"
    )
    names = list(dict.fromkeys(re.findall(name_pattern, website_text)))  # preserve order, deduplicate

    for email in emails:
        # A shared company mailbox is a channel, not a person — never
        # christen hello@/info@ a contact named "Hello"/"Info".
        if is_generic_inbox(email):
            contacts.append({
                "name": GENERIC_INBOX_NAME,
                "role": None,
                "detail": (
                    f"Generic inbox address ({email}) published on the website — "
                    "a usable company channel, not a named person."
                ),
                "email": email,
                "phone": None,
                "linkedin_url": None,
                "confidence": _mechanical_confidence(
                    "low",
                    "Shared company mailbox — usable channel, but not a named individual.",
                    email=email,
                    identity_level="low",
                    identity_reason="Generic mailbox local-part (info@/hello@ style); no person identified.",
                ),
            })
            continue

        local_part = email.split("@")[0]
        local_clean = re.sub(r"[._-]", " ", local_part).title()
        matched_name = None
        for n in names:
            if any(part.lower() in local_clean.lower() for part in n.split()):
                matched_name = n
                break
        display_name = matched_name or local_clean
        contacts.append({
            "name": display_name,
            "role": None,
            "detail": "Extracted from website",
            "email": email,
            "phone": None,
            "linkedin_url": None,
            "confidence": _mechanical_confidence(
                "medium" if matched_name else "low",
                "Email matched to a named role on the site." if matched_name else "Email found on site but not clearly tied to a named individual.",
                email=email,
                identity_level="medium" if matched_name else "low",
                identity_reason="Name matched near a role keyword in page text." if matched_name else "Name inferred from email address local-part only.",
            ),
        })

    for name in names:
        if not any(c["name"] == name for c in contacts):
            contacts.append({
                "name": name,
                "role": None,
                "detail": "Named on website as director or adviser",
                "email": None,
                "phone": None,
                "linkedin_url": None,
                "confidence": _mechanical_confidence(
                    "low", "Name found near a role keyword but no other channel (email/phone) confirmed.",
                    identity_level="medium", identity_reason="Matched near a role keyword (Director/Founder/etc.) in page text.",
                ),
            })

    return contacts


def compute_research_confidence(company: dict, research: dict, digital: dict) -> int:
    """
    Calibrated confidence scoring:
    - No website, CRO only: ~30–45
    - CBI + CRO, no website: ~50–65
    - Website found: +25–35
    - Named contacts found: +10
    - Public email found: +5
    """
    score = 20  # baseline: company exists in our DB

    # CRO confirmed
    if company.get("cro_number"):
        score += 10
    if company.get("cro_status", "").lower() == "normal":
        score += 5

    # CBI authorisation — significant quality signal
    cbi_auths = company.get("cbi_authorisations", [])
    if company.get("cbi_reference"):
        score += 15
    if isinstance(cbi_auths, list) and cbi_auths:
        score += min(len(cbi_auths) * 5, 15)  # up to 15 for multiple auths

    # Website found
    if research.get("website_text"):
        score += 25
        if digital.get("domain"):
            score += 5

    # Named contacts on website
    website_text = research.get("website_text", "")
    if website_text:
        if re.search(r"(?:Director|Founder|Co-founder|CEO|Managing Director|Partner|Owner|Adviser|Advisor)\s*[:\–-]?\s*[A-Z]", website_text):
            score += 10
        if re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", website_text):
            score += 5

    # Filing currency
    if company.get("last_annual_return"):
        score += 5
    if company.get("incorporation_date"):
        score += 5

    return min(score, 100)


def compute_sources_reviewed(research: dict) -> list[str]:
    """Return a list of source identifiers for the guard pipeline.
    Each source is a string identifier: 'cro_register', 'cbi_register', 
    'website:<domain>', or search result URL."""
    sources = []
    
    # CRO/CBI registry records always count as one source
    sources.append("cro_register")
    sources.append("cbi_register")
    
    # Website content
    if research.get("website_text"):
        sources.append("website")
    
    # Search results - use domain or URL as identifier
    sr = research.get("search_results", [])
    for s in sr:
        if isinstance(s, str) and "://" in s:
            # Extract domain from URL
            match = re.search(r"https?://([^/]+)", s)
            if match:
                sources.append(f"search:{match.group(1)}")
            else:
                sources.append(f"search:result")
        elif isinstance(s, dict) and s.get("url"):
            sources.append(f"search:{s.get('url')}")
        elif isinstance(s, dict) and s.get("title"):
            sources.append(f"search:{s.get('title')[:30]}")
    
    return sources if sources else ["cro_register", "cbi_register"]
