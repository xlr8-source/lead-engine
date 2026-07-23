import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from engine.llm.provider import complete_json, LLM_MODEL
from engine.prompt_loader import load_prompt


def _years_operating(incorporation_date: str | None) -> str | None:
    """Compute years operating from ISO incorporation date string."""
    if not incorporation_date:
        return None
    try:
        year = int(incorporation_date[:4])
        years = date.today().year - year
        return str(years) if years >= 0 else None
    except (ValueError, TypeError):
        return None


def build_context(company: dict, research: dict) -> str:
    """Build a rich, structured context string for the LLM."""
    name = company.get("legal_name", "N/A")
    trading = company.get("trading_name")
    cro_number = company.get("cro_number", "N/A")
    cro_status = company.get("cro_status", "N/A")
    company_type = company.get("company_type", "N/A")
    county = company.get("county", "N/A")
    incorp = company.get("incorporation_date", "N/A")
    years_op = _years_operating(company.get("incorporation_date"))
    address = company.get("registered_address", "N/A")
    eircode = company.get("eircode", "")
    cbi_ref = company.get("cbi_reference", "")
    cbi_auths = company.get("cbi_authorisations", [])
    last_return = company.get("last_annual_return", "")
    last_accounts = company.get("last_accounts_date", "")
    principal_obj = company.get("principal_object", "")

    lines = [
        "=== COMPANY RECORD ===",
        f"Legal name: {name}",
    ]
    if trading:
        lines.append(f"Trading name: {trading}")
    lines += [
        f"CRO number: {cro_number}",
        f"CRO status: {cro_status}",
        f"Company type: {company_type}",
        f"County: {county}",
        f"Registered address: {address}{(' ' + eircode) if eircode else ''}",
        f"Incorporation date: {incorp}",
    ]
    if years_op:
        lines.append(f"Years operating: {years_op}")
    if cbi_ref:
        lines.append(f"CBI reference: {cbi_ref}")
    if cbi_auths:
        if isinstance(cbi_auths, list):
            lines.append(f"CBI authorisations: {', '.join(cbi_auths)}")
        else:
            lines.append(f"CBI authorisations: {cbi_auths}")
    if last_return:
        lines.append(f"Last annual return: {last_return}")
    if last_accounts:
        lines.append(f"Last accounts date: {last_accounts}")
    if principal_obj:
        lines.append(f"Principal object (NACE): {principal_obj}")

    # Web research
    web_text = research.get("website_text", "")
    search_results = research.get("search_results", [])
    blocked_candidates = research.get("blocked_candidates") or []

    if web_text:
        # Strip the markdown link line from the first line (used as domain marker)
        lines_web = web_text.split("\n")
        domain_line = lines_web[0] if lines_web else ""
        body = "\n".join(lines_web[1:]).strip() if len(lines_web) > 1 else web_text
        lines += [
            "",
            "=== WEBSITE RESEARCH ===",
            f"Source: {domain_line}",
            "IMPORTANT: Analyze this website content for:",
            "- Specific insurance products/services mentioned",
            "- Evidence of payment collection features (direct debit, premium collection, payment portals)",
            "- Digital maturity indicators (online quotes, policy management, mobile apps, active content)",
            "- Provider relationships mentioned",
            "- Geographic focus or client segments",
            "- Named individuals or team members",
            "",
            # 12000: the body now aggregates the homepage PLUS appended
            # contact/team pages ("--- Additional page: ---" sections, up to
            # 3 x 4000 chars). A 6000 cap silently truncated away the very
            # team pages the crawl fetched — re-losing the named contacts.
            body[:12000],
        ]
    elif search_results:
        lines += [
            "",
            "=== SEARCH RESULTS (no website crawled) ===",
            "IMPORTANT: These are search result snippets only. No full website content was available.",
            "Use these to identify potential domains or business mentions, but do not invent details.",
        ]
        for snippet in search_results[:6]:
            lines.append(f"- {snippet}")

    if not web_text and blocked_candidates:
        lines += [
            "",
            "=== WEB RESEARCH ===",
            f"A candidate website ({blocked_candidates[0]}) appears to belong to this firm, but our "
            "automated research process was blocked from reading it (bot-protection challenge) — its "
            "content is unknown, not absent. Do NOT say this firm has 'no website' or 'no digital "
            "presence' — say the website could not be verified/accessed instead. Assessment must still "
            "rely primarily on CRO/CBI records for this reason.",
        ]
    elif not web_text and not search_results:
        lines += [
            "",
            "=== WEB RESEARCH ===",
            "No website found and no search results returned. Assessment must rely entirely on CRO/CBI records.",
            "Be explicit about this limitation in your assessment.",
        ]

    linkedin_results = research.get("linkedin_results") or []
    if linkedin_results:
        lines += [
            "",
            "=== LINKEDIN SEARCH RESULTS ===",
            "Search result titles/URLs only — these pages were not fetched (LinkedIn requires login).",
            "Do not create contacts from these titles — a person appearing here is NOT",
            "confirmed to work at this company (titles routinely surface unrelated or",
            "former people, e.g. 'Retired'/'Unemployed' profiles). Use them ONLY to",
            "corroborate a person already named in the website content above, or to",
            "attach a linkedin_url to such a person when a result clearly names them",
            "at this specific company. Never construct or guess a",
            "linkedin.com/in/... URL from a name — that is inventing data.",
            "",
        ]
        for hit in linkedin_results:
            lines.append(f"- {hit}")

    return "\n".join(lines)


def summarise(company: dict, research: dict) -> dict:
    """Call the LLM to produce a structured assessment of the company."""
    # Load the assessment system prompt — this includes the PayBrix product context
    system = load_prompt("assessment_system.md")

    context = build_context(company, research)

    data = complete_json(
        system_prompt=system,
        user_prompt=context,
        temperature=0.3,
        max_tokens=8000,
        # executive_summary/opening_angle missing or empty is the clearest
        # signal of a truncated/rushed response (large nested schema vs a
        # tight token budget) — force a retry rather than silently
        # persisting a thin result. See provider.py finish_reason logging
        # for the underlying truncation signal this is compensating for.
        required_keys=["executive_summary", "opening_angle"],
    )
    
    # Add model info for tracking
    data["llm_model"] = LLM_MODEL
    return data
