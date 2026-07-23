"""
Tests for the enrichment research layer
(engine/researcher.py, engine/research/extract.py, engine/assessor.py seams).

Cycle 2 of the board-driven hardening (2026-07-20), scoped per the field
notes: Windmill (wrong website attached), Pinnacle/MyLife (team pages
missed), hallucinated contacts ("Retired Unemployed", "Hello"), Clear
Insurance (form-only site), missing social links.

Section 1 is characterization — pins current good behavior before surgery.
Later sections are fix-validation tests written red-first per fix.

Everything runs offline: Tavily and page fetches are monkeypatched at the
module attributes where they are looked up.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from engine import researcher
from engine.research.contact_quality import (
    is_generic_inbox,
    is_plausible_person_name,
    sanitise_contacts,
)
from engine.research.extract import (
    extract_contacts,
    extract_digital_presence,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BROKER_TEXT = (
    "ABM Financial Advisers - Cork insurance broker. We arrange home insurance, "
    "motor insurance and life cover across 14 providers. Get a quote today. "
    "Managing Director: John Murphy. Email john.murphy@abmfinancial.ie or call 021 123 4567."
)

TOURISM_TEXT = (
    "Visit the historic Skerries Windmill. Guided tours daily, tickets from "
    "EUR 8, gift shop and tearoom on site. Explore five centuries of milling "
    "heritage on Dublin's coast. Opening hours 10am-5pm. Book your visit today."
)

PINNACLE_HOME = (
    "Pinnacle Insurance Brokers - Dublin commercial insurance specialists. "
    "Business insurance, fleet cover and professional indemnity quotes for "
    "Irish companies. Talk to our advisers about your renewal today."
)

TEAM_TEXT = (
    "Our Team at Pinnacle. Managing Director: Sarah Kelly. Director: Tom Byrne. "
    "Our advisers bring decades of commercial insurance experience."
)

FORM_TEXT = (
    "Contact Pinnacle Insurance Brokers. Get in touch using the enquiry form "
    "below and one of our team will come back to you. We'd love to hear from you."
)


def _company(legal_name: str, **extra) -> dict:
    firm = {
        "id": "test-id-1",
        "legal_name": legal_name,
        "trading_name": None,
        "cro_number": "112233",
        "cbi_reference": "C445566",
        "cro_status": "Normal",
        "county": "Dublin",
        "registered_address": "5 Mill Road, Dublin",
        "eircode": "D06 K4E5",
    }
    firm.update(extra)
    return firm


def _mock_search(results_by_default: list[dict]):
    """Return a fake _search_tavily that records queries and returns a fixed
    result list for every query (LinkedIn queries return the same list; only
    linkedin.com URLs survive that filter, so usually none)."""
    calls = []

    def fake_search(query: str, max_results: int = 10):
        calls.append(query)
        return results_by_default

    fake_search.calls = calls
    return fake_search


# ---------------------------------------------------------------------------
# Section 1 — characterization (current behavior, must stay green)
# ---------------------------------------------------------------------------

def test_clean_name_strips_legal_and_sector_suffixes():
    assert researcher._clean_name("Acme Financial Services Ltd") == "acme"
    assert researcher._clean_name("ABM Financial Advisers Limited") == "abm"


def test_score_website_match_high_for_matching_domain_and_content():
    score = researcher._score_website_match(
        "https://www.abmfinancial.ie",
        "ABM Financial Advisers Limited",
        None,
        BROKER_TEXT,
    )
    assert score >= 70


def test_extract_digital_presence_with_website():
    research = {"website_text": "[ABM Financial](https://www.abmfinancial.ie/)\nABM is a Cork broker."}

    digital = extract_digital_presence(research)

    assert digital["has_website"] is True
    assert digital["domain"] == "abmfinancial.ie"


def test_extract_digital_presence_without_website_is_honest():
    digital = extract_digital_presence({"website_text": None})

    assert digital["has_website"] is False
    assert digital["domain"] is None
    assert "No verified official website" in digital["quality_notes"]


def test_extract_contacts_named_person_with_matching_email():
    research = {"website_text": BROKER_TEXT}

    contacts = extract_contacts(research, _company("ABM Financial Advisers Limited"))

    by_name = {c["name"]: c for c in contacts}
    assert "John Murphy" in by_name
    assert by_name["John Murphy"]["email"] == "john.murphy@abmfinancial.ie"


def test_extract_contacts_cro_officer_fallback_without_website():
    research = {"website_text": None}
    company = _company(
        "Quiet Firm Limited",
        cro_officers=[{"name": "Mary Byrne", "role": "Director"}],
    )

    contacts = extract_contacts(research, company)

    assert len(contacts) == 1
    assert contacts[0]["name"] == "Mary Byrne"
    assert contacts[0]["source"] == "cro_register"


def test_research_company_selects_matching_site(monkeypatch):
    url = "https://www.abmfinancial.ie/"
    fake_search = _mock_search([{"title": "ABM Financial Advisers", "url": url, "snippet": ""}])
    monkeypatch.setattr(researcher, "_search_tavily", fake_search)
    monkeypatch.setattr(researcher, "_fetch_site_links", lambda u: [], raising=False)

    def fake_fetch(u, max_chars=8000, blocked=None):
        return BROKER_TEXT if u == url else None

    monkeypatch.setattr(researcher, "_fetch_text", fake_fetch)

    research = researcher.research_company(_company("ABM Financial Advisers Limited"))

    assert research["website_text"] is not None
    assert research["website_text"].startswith(f"[ABM Financial Advisers]({url})")


# ---------------------------------------------------------------------------
# Section 2 — P0a: website identity truthfulness (Windmill class)
# Written red-first: these fail until the fix lands.
# ---------------------------------------------------------------------------

def test_research_company_returns_no_website_when_nothing_relevant_found(monkeypatch):
    """The Windmill Insurances failure, reproduced: the only search hit is a
    windmill *tourism* site. The engine must conclude 'no website found' —
    not attach the tourism site, and not fall back to a random fetched URL."""
    url = "https://www.skerrieswindmill.ie/"
    fake_search = _mock_search([{"title": "Skerries Windmill Tours", "url": url, "snippet": ""}])
    monkeypatch.setattr(researcher, "_search_tavily", fake_search)

    def fake_fetch(u, max_chars=8000, blocked=None):
        return TOURISM_TEXT if u == url else None

    monkeypatch.setattr(researcher, "_fetch_text", fake_fetch)

    company = _company("Windmill Insurances Limited")
    research = researcher.research_company(company)

    assert research["website_text"] is None
    # And therefore no fabricated email/contacts from the wrong site:
    assert extract_contacts(research, company) == []


def test_score_website_match_caps_generic_overlap_without_sector_or_anchor():
    """A generic-word domain+content overlap ('windmill') with zero insurance
    vocabulary and zero identity anchors must score below the accept
    threshold — it's a different business that shares a word."""
    score = researcher._score_website_match(
        "https://www.skerrieswindmill.ie/",
        "Windmill Insurances Limited",
        None,
        TOURISM_TEXT,
        company=_company("Windmill Insurances Limited"),
    )
    assert score < 30, f"tourism site scored {score}, above accept threshold"


def test_score_website_match_identity_anchor_lifts_weak_domain():
    """Conversely: a domain that looks nothing like the firm name but whose
    page carries the firm's own eircode (identity anchor) plus insurance
    vocabulary IS the firm's site and must clear the threshold."""
    content = (
        "Independent Dublin insurance brokers. Home, motor and commercial "
        "cover arranged across leading insurers. Find us at 5 Mill Road, D06 K4E5."
    )
    score = researcher._score_website_match(
        "https://www.wmib.ie/",
        "Windmill Insurances Limited",
        None,
        content,
        company=_company("Windmill Insurances Limited"),
    )
    assert score >= 70, f"anchored site scored only {score}"


# ---------------------------------------------------------------------------
# Section 4 — P1: team-page coverage, form-only outcome, social links
# Field evidence: Pinnacle (6-person team page missed), MyLife (6 contacts
# under a "team" category never fetched), Clear Insurance (form-only site),
# digital presence card missing social links. Written red-first.
# ---------------------------------------------------------------------------

def test_try_contact_pages_returns_all_people_bearing_pages(monkeypatch):
    """A form-only /contact page must no longer swallow a fully-staffed
    /our-team page — both come back, as a list."""
    base = "https://www.pinnacle.ie/"
    monkeypatch.setattr(researcher, "_fetch_site_links", lambda u: [], raising=False)

    def fake_fetch(u, max_chars=8000, blocked=None):
        clean = u.rstrip("/")
        if clean.endswith("/our-team"):
            return TEAM_TEXT
        if clean.endswith("/contact"):
            return FORM_TEXT
        return None

    monkeypatch.setattr(researcher, "_fetch_text", fake_fetch)

    hits = researcher._try_contact_pages(base, {})

    assert isinstance(hits, list)
    urls = [u for u, _ in hits]
    assert any("our-team" in u for u in urls), f"team page missing from {urls}"
    assert any("contact" in u for u in urls), f"contact page missing from {urls}"


def test_try_contact_pages_probes_nav_discovered_links(monkeypatch):
    """MyLife class: people pages named outside the fixed path list must be
    reachable via nav-link discovery on the homepage."""
    base = "https://www.mylife.ie/"
    people_url = "https://www.mylife.ie/who-we-are"
    monkeypatch.setattr(researcher, "_fetch_site_links", lambda u: [people_url], raising=False)

    def fake_fetch(u, max_chars=8000, blocked=None):
        return TEAM_TEXT if u == people_url else None

    monkeypatch.setattr(researcher, "_fetch_text", fake_fetch)

    hits = researcher._try_contact_pages(base, {})

    assert isinstance(hits, list)
    assert any("who-we-are" in u for u, _ in hits)


def test_research_company_aggregates_team_page_contacts(monkeypatch):
    """End to end: the Pinnacle scenario. Homepage wins as the site; the
    /our-team page's named directors must reach website_text and contacts."""
    main_url = "https://www.pinnacle.ie/"
    fake_search = _mock_search([{"title": "Pinnacle Insurance Brokers", "url": main_url, "snippet": ""}])
    monkeypatch.setattr(researcher, "_search_tavily", fake_search)
    monkeypatch.setattr(researcher, "_fetch_site_links", lambda u: [], raising=False)

    def fake_fetch(u, max_chars=8000, blocked=None):
        clean = u.rstrip("/")
        if clean == main_url.rstrip("/"):
            return PINNACLE_HOME
        if clean.endswith("/our-team"):
            return TEAM_TEXT
        if clean.endswith("/contact"):
            return FORM_TEXT
        return None

    monkeypatch.setattr(researcher, "_fetch_text", fake_fetch)

    company = _company("Pinnacle Insurance Brokers Limited")
    research = researcher.research_company(company)

    assert "Sarah Kelly" in (research["website_text"] or "")
    names = [c["name"] for c in extract_contacts(research, company)]
    assert "Sarah Kelly" in names
    assert "Tom Byrne" in names


def test_classify_contact_channel():
    from engine.research.extract import classify_contact_channel

    assert classify_contact_channel(FORM_TEXT) == "form_only"
    assert classify_contact_channel(BROKER_TEXT) == "email"
    assert classify_contact_channel("Call our office on 021 123 4567 today.") == "phone"
    assert classify_contact_channel(None) == "none"


def test_extract_digital_presence_surfaces_channel_and_social():
    research = {
        "website_text": "[Pinnacle](https://www.pinnacle.ie/)\n" + FORM_TEXT,
        "social_links": {"facebook": "https://www.facebook.com/pinnacleinsurance"},
    }

    digital = extract_digital_presence(research)

    assert digital["contact_channel"] == "form_only"
    assert digital["social_links"] == {"facebook": "https://www.facebook.com/pinnacleinsurance"}


def test_research_company_collects_social_links(monkeypatch):
    """Real social profiles of the firm must be captured for the digital
    presence card — while unrelated social pages are ignored."""
    main_url = "https://www.pinnacle.ie/"
    fake_search = _mock_search([
        {"title": "Pinnacle Insurance Brokers", "url": main_url, "snippet": ""},
        {"title": "Pinnacle Insurance Brokers | Facebook", "url": "https://www.facebook.com/pinnacleinsurance", "snippet": ""},
        {"title": "Random Cafe | Facebook", "url": "https://www.facebook.com/randomcafe", "snippet": ""},
        {"title": "Pinnacle Insurance Brokers | LinkedIn", "url": "https://www.linkedin.com/company/pinnacle-insurance-brokers", "snippet": ""},
    ])
    monkeypatch.setattr(researcher, "_search_tavily", fake_search)
    monkeypatch.setattr(researcher, "_fetch_site_links", lambda u: [], raising=False)

    def fake_fetch(u, max_chars=8000, blocked=None):
        return PINNACLE_HOME if u == main_url else None

    monkeypatch.setattr(researcher, "_fetch_text", fake_fetch)

    research = researcher.research_company(_company("Pinnacle Insurance Brokers Limited"))

    social = research.get("social_links") or {}
    assert social.get("facebook") == "https://www.facebook.com/pinnacleinsurance"
    assert social.get("linkedin") == "https://www.linkedin.com/company/pinnacle-insurance-brokers"
    assert "randomcafe" not in str(social)


def test_build_context_keeps_additional_pages():
    """The LLM context builder must not truncate away appended team/contact
    pages — that would re-lose the contacts the crawl just found."""
    from engine.llm.summarise import build_context

    website_text = (
        "[Pinnacle](https://www.pinnacle.ie/)\n"
        + ("insurance services and cover details " * 200)  # ~7,400 chars of body
        + "\n\n--- Additional page: https://www.pinnacle.ie/our-team ---\n"
        + TEAM_TEXT
    )

    context = build_context(
        _company("Pinnacle Insurance Brokers Limited"),
        {"website_text": website_text, "search_results": []},
    )

    assert "Sarah Kelly" in context


# ---------------------------------------------------------------------------
# Section 5 — P2: research speed + stage timing. Field evidence: ~136s per
# firm. Written red-first; behavior (not wall-clock) is what's asserted.
# ---------------------------------------------------------------------------

def test_research_speed_constants():
    """5s fetch timeout (dead .ie domains dominated worst-case latency at
    10s), 8-way fetch concurrency, and a hard cap on result fetches."""
    assert researcher.FETCH_TIMEOUT == 5.0
    assert researcher.FETCH_CONCURRENCY == 8
    assert researcher.MAX_RESULT_FETCHES == 15


def test_research_company_caps_fetch_volume(monkeypatch):
    """A flood of search results must not translate into unbounded page
    fetches — at 5s timeout each, 30 dead URLs is 30+ seconds of nothing."""
    flood = [
        {"title": f"Result {i}", "url": f"https://www.unrelated-site-{i}.ie/", "snippet": ""}
        for i in range(30)
    ]
    fake_search = _mock_search(flood)
    monkeypatch.setattr(researcher, "_search_tavily", fake_search)
    monkeypatch.setattr(researcher, "_fetch_site_links", lambda u: [], raising=False)

    fetched_urls = []

    def fake_fetch(u, max_chars=8000, blocked=None):
        fetched_urls.append(u)
        return None

    monkeypatch.setattr(researcher, "_fetch_text", fake_fetch)

    research = researcher.research_company(_company("Windmill Insurances Limited"))

    assert research["website_text"] is None
    assert len(fetched_urls) <= researcher.MAX_RESULT_FETCHES


def test_assess_company_reports_stage_timings(monkeypatch):
    """assess_company must expose where the time went (research vs LLM vs
    guards) — 136s/firm is undiagnosable without per-stage numbers."""
    import engine.assessor as assessor_mod

    monkeypatch.setattr(
        assessor_mod, "research_company",
        lambda company: {"website_text": BROKER_TEXT, "search_results": [], "linkedin_results": [], "social_links": {}},
    )
    monkeypatch.setattr(
        assessor_mod, "summarise",
        lambda company, research: {
            "qualification_score": 55,
            "signal_strength": "medium",
            "executive_summary": "A CBI-authorised Cork broker with a live website and one named director identified.",
            "recommended_angle": "Ask about premium reconciliation across providers.",
            "opening_angle": "Multi-provider firms often lose sight of premium status.",
            "contacts": None,
        },
    )

    result = assessor_mod.assess_company(_company("ABM Financial Advisers Limited"))

    timings = result["stage_timings"]
    for key in ("research_seconds", "llm_seconds", "guards_seconds"):
        assert isinstance(timings[key], float)
        assert timings[key] >= 0.0


def test_enrich_all_runs_assessments_concurrently(monkeypatch):
    """The bulk endpoint processed firms strictly serially — at ~136s/firm
    that's the whole night for 30 firms. Must overlap assessments."""
    import time as _time
    import threading as _threading
    from contextlib import contextmanager

    import db.init_db as init_db_mod
    # api.main calls init_db() at import — keep the test off the real DB.
    monkeypatch.setattr(init_db_mod, "init_db", lambda *a, **k: None)
    import api.main as api_main
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASSESS_CONCURRENCY", "3")
    companies = [{"id": f"c{i}", "legal_name": f"Firm {i}"} for i in range(6)]
    monkeypatch.setattr(api_main, "get_unenriched_companies", lambda: companies)

    lock = _threading.Lock()
    state = {"current": 0, "max": 0}

    def fake_assess(company, on_event=None):
        with lock:
            state["current"] += 1
            state["max"] = max(state["max"], state["current"])
        _time.sleep(0.3)
        with lock:
            state["current"] -= 1
        return {"company_id": company["id"]}

    @contextmanager
    def fake_conn():
        yield None

    monkeypatch.setattr(api_main, "assess_company", fake_assess)
    monkeypatch.setattr(api_main, "get_db_connection", fake_conn)
    monkeypatch.setattr(api_main, "upsert_enrichment", lambda conn, e: None)

    client = TestClient(api_main.app)
    resp = client.post("/api/enrich-all")

    assert resp.status_code == 200
    body = resp.json()
    assert body["enriched"] == 6
    assert body["failed"] == 0
    assert state["max"] >= 2, f"assessments never overlapped (max concurrent: {state['max']})"


# ---------------------------------------------------------------------------
# Section 6 — P2 prompts: personalisation leads with commercial intel,
# LinkedIn titles quarantined, and the reference/reference_points key
# mismatch (email generation never saw the reference facts). Red-first.
# ---------------------------------------------------------------------------

def test_generate_email_reads_reference_points(monkeypatch):
    """The assessment prompt emits personalisation.reference_points, but
    generate_email read personalisation['reference'] — so the reference facts
    never reached the email prompt. They must."""
    import engine.assessor as assessor_mod

    captured = {}

    def fake_complete_json(**kwargs):
        captured["user_prompt"] = kwargs.get("user_prompt")
        return {"subject": "Premium reconciliation", "body": "Short grounded body."}

    monkeypatch.setattr(assessor_mod, "complete_json", fake_complete_json)
    monkeypatch.setattr(assessor_mod, "evaluate_email", lambda s, b, g: {"passed": True, "issues": []})

    company = {
        **_company("ABM Financial Advisers Limited"),
        "enrichment": {
            "narrative_assessment": {
                "personalisation": {
                    "reference_points": ["Works across 14 different providers as a Multi-Agency Intermediary"],
                    "avoid": [],
                },
                "contacts": [],
                "opening_angle": "Multi-provider firms often lose premium visibility.",
            }
        },
    }

    assessor_mod.generate_email(company)

    assert "Works across 14 different providers" in captured["user_prompt"]


def test_build_context_quarantines_linkedin_titles():
    """LinkedIn search-result titles fed to the LLM are where 'Retired
    Unemployed' contacts came from — the context must explicitly forbid
    minting contacts from them."""
    from engine.llm.summarise import build_context

    context = build_context(
        _company("ABM Financial Advisers Limited"),
        {
            "website_text": None,
            "search_results": [],
            "linkedin_results": ["John Murphy - Retired - Unemployed | LinkedIn — https://linkedin.com/in/x"],
        },
    )

    assert "Do not create contacts from these titles" in context


def test_assessment_prompt_contains_ordering_and_linkedin_rules():
    """Pin the two prompt-contract rules so a future prompt edit can't
    silently drop them."""
    text = (ROOT / "engine" / "prompts" / "assessment_system.md").read_text(encoding="utf-8")

    assert "Order `reference_points` by commercial value" in text
    assert "Never create a contact from a LinkedIn search-result title" in text


# ---------------------------------------------------------------------------
# Section 3 — P0b: contact plausibility (hallucinated-contact class)
# Field evidence: "Retired Unemployed Your Partner", "What", "hello" stored
# as contacts. Written red-first.
# ---------------------------------------------------------------------------

def _full_confidence(level: str = "medium") -> dict:
    """A complete ContactConfidence-shaped dict (all 7 fields required by the
    governor schema)."""
    f = {"level": level, "reason": "Grounded in website research context text."}
    return {k: dict(f) for k in ("identity", "role", "email", "phone", "linkedin", "freshness", "overall")}


def test_is_generic_inbox_detection():
    assert is_generic_inbox("hello@acme.ie") is True
    assert is_generic_inbox("info@acme.ie") is True
    assert is_generic_inbox("office@acme.ie") is True
    assert is_generic_inbox("john.murphy@acme.ie") is False


def test_is_plausible_person_name():
    assert is_plausible_person_name("John Murphy") is True
    assert is_plausible_person_name("Sarah O'Brien-Kelly") is True
    # The exact junk seen in the field notes:
    assert is_plausible_person_name("Retired Unemployed Your Partner") is False
    assert is_plausible_person_name("Retired Unemployed What") is False
    assert is_plausible_person_name("What") is False
    assert is_plausible_person_name("Hello") is False
    assert is_plausible_person_name("") is False


def test_sanitise_contacts_drops_junk_and_relabels_generic_inboxes():
    contacts = [
        {"name": "Retired Unemployed Your Partner", "role": None, "email": None,
         "phone": None, "linkedin_url": None, "confidence": _full_confidence()},
        {"name": "What", "role": None, "email": None,
         "phone": None, "linkedin_url": None, "confidence": _full_confidence()},
        {"name": "Hello", "role": None, "email": "hello@acme.ie",
         "phone": None, "linkedin_url": None, "confidence": _full_confidence()},
        {"name": "John Murphy", "role": "Director", "email": "john.murphy@acme.ie",
         "phone": None, "linkedin_url": None, "confidence": _full_confidence("high")},
    ]

    kept, dropped = sanitise_contacts(contacts)

    names = [c["name"] for c in kept]
    assert "John Murphy" in names
    assert "General company inbox" in names  # hello@ kept as a channel, honestly labeled
    assert len(kept) == 2
    assert len(dropped) == 2  # the two junk-name contacts, with reasons
    inbox = next(c for c in kept if c["name"] == "General company inbox")
    assert inbox["email"] == "hello@acme.ie"
    assert inbox["confidence"]["identity"]["level"] == "low"


def test_extract_contacts_generic_inbox_not_named_hello():
    """extract_contacts must not christen hello@domain a person called
    'Hello' — it's a shared company mailbox."""
    research = {"website_text": "Get in touch with our brokers today: hello@acme.ie"}

    contacts = extract_contacts(research, _company("Acme Insurance Limited"))

    assert len(contacts) == 1
    assert contacts[0]["name"] == "General company inbox"
    assert contacts[0]["email"] == "hello@acme.ie"


def test_refresh_contacts_filters_llm_junk(monkeypatch):
    """Seam test on assessor.refresh_contacts: even when the LLM returns a
    schema-valid junk contact (the exact Pinnacle failure), it must be
    filtered before persistence."""
    import engine.assessor as assessor_mod

    monkeypatch.setattr(
        assessor_mod, "research_company",
        lambda company: {"website_text": BROKER_TEXT, "search_results": [], "linkedin_results": []},
    )
    monkeypatch.setattr(
        assessor_mod, "complete_json",
        lambda **kwargs: {"contacts": [
            {"name": "Retired Unemployed What", "role": None, "detail": None, "email": None,
             "phone": None, "linkedin_url": None, "confidence": _full_confidence()},
            {"name": "John Murphy", "role": "Managing Director", "detail": "Named on site",
             "email": "john.murphy@abmfinancial.ie", "phone": None, "linkedin_url": None,
             "confidence": _full_confidence("high")},
        ]},
    )

    validated = assessor_mod.refresh_contacts(_company("ABM Financial Advisers Limited"))

    names = [c["name"] for c in validated]
    assert names == ["John Murphy"]


def test_reconcile_digital_presence_catches_no_insurance_website_phrasing():
    """The live band-aid regex missed the exact phrasing from the field notes
    ('No insurance website found') — the reconciliation must fire on it."""
    from engine.assessor import _reconcile_digital_presence

    digital = {
        "has_website": True,
        "domain": "skerrieswindmill.ie",
        "pages_crawled": 1,
        "quality_notes": "Website successfully crawled",
    }
    llm_data = {
        "executive_summary": (
            "No insurance website found for this firm; the crawled domain is "
            "a heritage tourism attraction unconnected to insurance."
        )
    }

    corrected, fired = _reconcile_digital_presence(digital, llm_data, "Windmill Insurances Limited")

    assert fired is True
    assert corrected["has_website"] is False
    assert corrected["domain"] is None


# ---------------------------------------------------------------------------
# Bot/WAF challenge detection — "found but blocked" must never collapse into
# "no website" (the Clements Insurance / clementsins.com case: real site,
# Cloudflare challenge page, our fetch gets a 403 we must not misreport as
# the domain not existing).
# ---------------------------------------------------------------------------

def _fake_response(status_code, text="", headers=None):
    class _Resp:
        pass
    r = _Resp()
    r.status_code = status_code
    r.text = text
    r.headers = headers or {}
    return r


def test_is_bot_challenge_detects_cloudflare_403():
    resp = _fake_response(
        403,
        text="<title>Just a moment...</title>",
        headers={"server": "cloudflare"},
    )
    assert researcher._is_bot_challenge(resp) is True


def test_is_bot_challenge_detects_generic_challenge_marker_without_cloudflare_header():
    resp = _fake_response(503, text="Please verify you are human to continue.")
    assert researcher._is_bot_challenge(resp) is True


def test_is_bot_challenge_false_for_genuine_404():
    resp = _fake_response(404, text="Not Found")
    assert researcher._is_bot_challenge(resp) is False


def test_is_bot_challenge_false_for_normal_200():
    resp = _fake_response(200, text="<html>Welcome to our site</html>")
    assert researcher._is_bot_challenge(resp) is False


def test_research_company_reports_blocked_candidate_not_absent(monkeypatch):
    """A domain that plausibly matches the firm's name but returns a
    bot-challenge response must surface as blocked_candidates, with
    website_text still None — never silently equated with 'no website'."""
    url = "https://www.clementsins.com/"
    fake_search = _mock_search([{"title": "Clements Insurance", "url": url, "snippet": ""}])
    monkeypatch.setattr(researcher, "_search_tavily", fake_search)
    monkeypatch.setattr(researcher, "_fetch_site_links", lambda u: [], raising=False)

    def fake_fetch(u, max_chars=8000, blocked=None):
        if u == url and blocked is not None:
            blocked.append(u)
        return None

    monkeypatch.setattr(researcher, "_fetch_text", fake_fetch)

    research = researcher.research_company(_company("Clements Insurance Services Ltd"))

    assert research["website_text"] is None
    assert url in research["blocked_candidates"]


def test_research_company_does_not_attribute_unrelated_blocked_domain(monkeypatch):
    """A blocked URL whose domain doesn't plausibly match the firm's name
    must not be reported as this firm's blocked candidate."""
    url = "https://www.totally-unrelated-cafe.ie/"
    fake_search = _mock_search([{"title": "Unrelated", "url": url, "snippet": ""}])
    monkeypatch.setattr(researcher, "_search_tavily", fake_search)
    monkeypatch.setattr(researcher, "_fetch_site_links", lambda u: [], raising=False)

    def fake_fetch(u, max_chars=8000, blocked=None):
        if u == url and blocked is not None:
            blocked.append(u)
        return None

    monkeypatch.setattr(researcher, "_fetch_text", fake_fetch)

    research = researcher.research_company(_company("Clements Insurance Services Ltd"))

    assert research["blocked_candidates"] == []


def test_extract_digital_presence_reports_blocked_as_unverified_not_absent():
    research = {
        "website_text": None,
        "blocked_candidates": ["https://www.clementsins.com/"],
    }
    result = extract_digital_presence(research)

    assert result["has_website"] is False
    assert "clementsins.com" in result["quality_notes"]
    assert "no website" not in result["quality_notes"].lower()
    assert "no digital presence" not in result["quality_notes"].lower()
