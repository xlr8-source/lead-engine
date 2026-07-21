import logging
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from engine.llm.provider import complete_json, RateLimitError, APIError, LLM_MODEL
from engine.prompt_loader import load_prompt
from engine.researcher import research_company
from engine.research.contact_quality import sanitise_contacts
from engine.research.extract import (
    extract_digital_presence,
    extract_research_coverage,
    extract_contacts,
    compute_research_confidence,
    compute_sources_reviewed,
)
from engine.llm.summarise import summarise, build_context
from engine.activity import OnEvent

# --- Governor ---
from engine.governor.runner import run_guards
from engine.governor.schemas import EnrichmentSchema, Contact
from engine.governor.email_guard import evaluate_email
from pydantic import ValidationError

logger = logging.getLogger(__name__)


# digital_presence is computed deterministically by extract_digital_presence()
# BEFORE the LLM ever runs, from whatever page research_company() crawled —
# with no check that the crawled page actually names this company. The LLM
# sees the same crawled text in context and often *does* work out, in prose,
# that it belongs to a different firm (wrong directory match, similarly-named
# company, etc). But since the LLM's own digital_presence output is discarded
# in favour of the pre-computed one (see below), that correct conclusion never
# reaches the stored record — has_website/domain stay wrong even though the
# LLM said otherwise three sentences later in executive_summary. This scans
# the LLM's own text for that conclusion and reconciles digital_presence to
# match it.
_DOMAIN_MISMATCH_RE = re.compile(
    r"belongs to (a |an )?(different|separate|unrelated) (company|firm|entity)"
    r"|is a (different|separate) (company|firm|entity)"
    r"|crawled content belongs to"
    r"|not (their|this firm'?s|the company'?s) (official )?website"
    r"|no official website .* was identified"
    # Generalised: "No insurance website found", "no company website was
    # identified", etc. The Windmill incident used exactly this phrasing and
    # the narrower patterns above missed it.
    r"|no (\w+ )?website (was |has been )?(found|identified)"
    r"|unrelated to this (company|firm)"
    r"|unconnected to (this|the) (company|firm)",
    re.IGNORECASE,
)


def _reconcile_digital_presence(digital_presence: dict, llm_data: dict, company_name: str) -> tuple[dict, bool]:
    """Correct digital_presence in place if the LLM's own text contradicts it.

    Returns (possibly-corrected digital_presence, whether a correction fired).
    """
    if not digital_presence.get("has_website"):
        return digital_presence, False

    text_blob = " ".join([
        str(llm_data.get("executive_summary") or ""),
        " ".join(str(x) for x in (llm_data.get("personalisation") or {}).get("avoid", [])),
        str(((llm_data.get("opportunity_signal") or {}).get("digital_maturity") or {}).get("reason") or ""),
    ])

    if not _DOMAIN_MISMATCH_RE.search(text_blob):
        return digital_presence, False

    logger.warning(
        f"[Governor] digital_presence self-contradiction detected and corrected for "
        f"{company_name}: domain='{digital_presence.get('domain')}' — assessment text "
        f"indicated this does not belong to the company being assessed."
    )
    corrected = {
        "has_website": False,
        "domain": None,
        "pages_crawled": digital_presence.get("pages_crawled", 0),
        "quality_notes": (
            f"Candidate domain '{digital_presence.get('domain')}' was crawled but discarded — "
            f"assessment text indicated it belongs to a different company, not this one."
        ),
    }
    return corrected, True


# On some responses the LLM duplicates a cluster of assessment-level fields
# (opening_angle, recommended_angle, personalisation, discovery_questions,
# billing_pain_points, assessment_breakdown) by nesting a second, populated
# copy inside contacts[0] instead of — or as well as — the top level the
# schema expects them at. When that happens the top-level copy frequently
# comes back null/empty, which is what the UI and Pydantic schema actually
# read, so a real, well-formed value the model DID generate silently never
# reaches storage. This is a structural self-consistency slip on the LLM's
# part (same failure family as the digital_presence contradiction above),
# not something any single prompt instruction reliably prevents — so it's
# rescued here rather than re-prompted for.
_RESCUABLE_FIELDS = [
    "opening_angle", "recommended_angle", "personalisation",
    "discovery_questions", "billing_pain_points", "assessment_breakdown",
]


def _rescue_misplaced_narrative_fields(llm_data: dict, company_name: str) -> dict:
    """If a top-level narrative field is null/empty but contacts[0] carries
    a populated value under the same key, hoist it up to the top level.

    Returns llm_data (mutated in place for the rescued keys).
    """
    contacts = llm_data.get("contacts")
    if not isinstance(contacts, list) or not contacts:
        return llm_data
    first_contact = contacts[0]
    if not isinstance(first_contact, dict):
        return llm_data

    rescued = []
    for key in _RESCUABLE_FIELDS:
        top_value = llm_data.get(key)
        is_empty = top_value is None or top_value == "" or top_value == [] or top_value == {}
        contact_value = first_contact.get(key)
        contact_has_value = not (contact_value is None or contact_value == "" or contact_value == [] or contact_value == {})
        if is_empty and contact_has_value:
            llm_data[key] = contact_value
            rescued.append(key)

    if rescued:
        logger.warning(
            f"[Governor] Rescued misplaced narrative field(s) for {company_name}: "
            f"{rescued} were null/empty at the schema-expected top level but populated "
            f"inside contacts[0] — hoisted up rather than lost."
        )
    return llm_data


def assess_company(company: dict, on_event: OnEvent | None = None) -> dict:
    def _emit(step, label, status, metadata=None):
        if on_event:
            on_event(step, label, status, metadata)

    company = dict(company)
    company_name = company.get("legal_name", "Unknown")
    company_id = company.get("id", "Unknown")

    logger.info(f"[ASSESSMENT START] Company: {company_name} (ID: {company_id})")

    try:
        _emit("company_research", "Collecting company evidence", "running")
        _t_research = time.perf_counter()
        research = research_company(company)
        research_seconds = round(time.perf_counter() - _t_research, 2)
        logger.info(f"[RESEARCH COMPLETE] Company: {company_name} - Website found: {bool(research.get('website_text'))} ({research_seconds}s)")
        _emit("company_research", "Collecting company evidence", "complete", {
            "evidence_sources": len(research.get("search_results", []) or []) + (1 if research.get("website_text") else 0),
            "elapsed_seconds": research_seconds,
        })

        digital_presence = extract_digital_presence(research)
        research_coverage = extract_research_coverage(company, research, digital_presence)
        regex_contacts = extract_contacts(research, company)
        research_confidence = compute_research_confidence(company, research, digital_presence)
        sources_reviewed = compute_sources_reviewed(research)

        logger.info(f"[LLM ASSESSMENT START] Company: {company_name} - Model: {LLM_MODEL}")
        _emit("company_identity", "Validating company identity", "running")
        _emit("contact_discovery", "Finding contact candidates", "running")
        _t_llm = time.perf_counter()
        llm_data = summarise(company, research)
        llm_seconds = round(time.perf_counter() - _t_llm, 2)
        logger.info(f"[LLM ASSESSMENT COMPLETE] Company: {company_name} - Score: {llm_data.get('qualification_score')} ({llm_seconds}s)")

        # Backfill any narrative fields the LLM nested inside contacts[0]
        # instead of at the top level it should have used.
        llm_data = _rescue_misplaced_narrative_fields(llm_data, company_name)

        # Reconcile digital_presence against what the LLM's own reasoning
        # concluded — see _reconcile_digital_presence for why this can't be
        # trusted as computed. If it fires, research_coverage/confidence are
        # recomputed against the corrected (not the crawled-but-wrong) signal.
        digital_presence, _dp_corrected = _reconcile_digital_presence(digital_presence, llm_data, company_name)
        if _dp_corrected:
            research_coverage = extract_research_coverage(company, research, digital_presence)
            research_confidence = compute_research_confidence(company, research, digital_presence)

        _emit("company_identity", "Validating company identity", "complete", {
            "has_website": digital_presence.get("has_website", False),
            "research_confidence": research_confidence,
        })

        # Prefer the LLM's own contact extraction over the regex-based one.
        # Previously this was hardcoded to always use extract_contacts(),
        # which silently discarded the contacts the LLM was explicitly
        # prompted to produce (with instructions on how to disambiguate
        # name/role/email) in favour of a much cruder proximity-regex over
        # the same text. The LLM output is only trusted here if it's a
        # non-empty list of dicts that actually have a name — otherwise we
        # fall back to the regex extraction as before.
        llm_contacts = llm_data.get("contacts")
        if isinstance(llm_contacts, list) and llm_contacts and all(
            isinstance(c, dict) and c.get("name") for c in llm_contacts
        ):
            contacts = llm_contacts
        else:
            contacts = regex_contacts

        # Plausibility gate — junk names ("Retired Unemployed Your Partner",
        # "What") are dropped and generic inboxes (hello@) honestly relabeled,
        # whichever extractor produced them. Schema-valid junk must not reach
        # the sales UI.
        contacts, dropped_contacts = sanitise_contacts(contacts)
        if dropped_contacts:
            logger.warning(
                f"[ContactQuality] Dropped {len(dropped_contacts)} implausible "
                f"contact(s) for {company_name}: {dropped_contacts}"
            )

        _emit("contact_discovery", "Finding contact candidates", "complete", {
            "contacts_checked": len(contacts),
            "contacts_dropped": len(dropped_contacts),
        })

        result = {
            "company_id": company["id"],
            "llm_model": llm_data.get("llm_model", LLM_MODEL),
            "qualification_score": llm_data.get("qualification_score"),
            "signal_strength": llm_data.get("signal_strength", "medium"),
            "executive_summary": llm_data.get("executive_summary"),
            "opportunity_signal": llm_data.get("opportunity_signal"),
            "personalisation": llm_data.get("personalisation"),
            "discovery_questions": llm_data.get("discovery_questions", []),
            "opening_angle": llm_data.get("opening_angle"),
            "recommended_angle": llm_data.get("recommended_angle"),
            "billing_pain_points": llm_data.get("billing_pain_points", []),
            "assessment_breakdown": llm_data.get("assessment_breakdown", {}),
            "digital_presence": digital_presence,
            "research_coverage": research_coverage,
            "contacts": contacts,
            "research_confidence": research_confidence,
            "sources_reviewed": sources_reviewed,
        }

        structured_keys = [
            "executive_summary", "research_confidence", "research_coverage",
            "sources_reviewed", "opportunity_signal", "contacts",
            "digital_presence", "personalisation", "discovery_questions",
            "opening_angle",
        ]
        na = {}
        for k in structured_keys:
            if k in result:
                na[k] = result.pop(k)

        result["narrative_assessment"] = na

        # ------------------------------------------------------------------
        # Phase 1: Pydantic schema validation
        # ------------------------------------------------------------------
        flat_for_validation = {
            **result,
            # Hoist fields from narrative_assessment for schema validation.
            # Previously opportunity_signal/personalisation/opening_angle
            # were NOT hoisted here, meaning the schema (and, by extension,
            # the opportunity-signal guard) never actually saw real data for
            # the scorecard — it was popped into narrative_assessment first
            # and the validator only ever saw the field-default None.
            "executive_summary": na.get("executive_summary", ""),
            "research_confidence": na.get("research_confidence", 0),
            "sources_reviewed": na.get("sources_reviewed", []),
            "opportunity_signal": na.get("opportunity_signal"),
            "personalisation": na.get("personalisation"),
            "opening_angle": na.get("opening_angle"),
            "contacts": na.get("contacts"),
        }
        _emit("contact_validation", "Checking contact evidence", "running")
        try:
            EnrichmentSchema.model_validate(flat_for_validation)
            logger.info(f"[Governor] Schema validation PASSED: {company_name}")
        except ValidationError as ve:
            # Schema violations are logged but do NOT block storage —
            # they surface in the guard report instead.
            logger.warning(
                f"[Governor] Schema validation WARNING: {company_name} — "
                f"{ve.error_count()} issue(s): {ve.errors()[0]['msg'] if ve.errors() else 'unknown'}"
            )
        _high_conf_contacts = sum(
            1 for c in (contacts or [])
            if isinstance(c, dict) and (c.get("confidence") or {}).get("overall", {}).get("level") == "high"
        )
        _emit("contact_validation", "Checking contact evidence", "complete", {"high_confidence_contacts": _high_conf_contacts})

        # ------------------------------------------------------------------
        # Phase 2: Run guard pipeline
        # ------------------------------------------------------------------
        _emit("sales_context", "Building commercial assessment", "running")
        _t_guards = time.perf_counter()
        guard_report = run_guards(flat_for_validation)
        guards_seconds = round(time.perf_counter() - _t_guards, 2)
        guard_dict = guard_report.to_dict()

        result["guard_passed"] = guard_report.passed
        result["guard_score"] = round(guard_report.overall_score, 1)
        result["guard_failures"] = guard_report.failed_guards
        _emit("sales_context", "Building commercial assessment", "complete", {
            "guard_score": round(guard_report.overall_score, 1), "guard_passed": guard_report.passed,
        })
        result["guard_warnings"] = guard_report.warning_guards
        result["guard_report"] = guard_dict

        logger.info(
            f"[Governor] Guards {'PASSED' if guard_report.passed else 'FAILED'}: "
            f"{company_name} — score={guard_report.overall_score:.1f}, "
            f"failed={guard_report.failed_guards}"
        )

        # Per-stage wall-clock breakdown — the only way a slow assessment
        # (research-bound vs LLM-bound) is diagnosable from logs/events.
        result["stage_timings"] = {
            "research_seconds": research_seconds,
            "llm_seconds": llm_seconds,
            "guards_seconds": guards_seconds,
        }
        logger.info(
            f"[TIMING] {company_name}: research={research_seconds}s "
            f"llm={llm_seconds}s guards={guards_seconds}s "
            f"total={round(research_seconds + llm_seconds + guards_seconds, 2)}s"
        )

        logger.info(f"[ASSESSMENT COMPLETE] Company: {company_name} (ID: {company_id}) - Score: {result.get('qualification_score')}")
        return result

    except RateLimitError as e:
        logger.error(f"[RATE LIMIT] Company: {company_name} - {str(e)}")
        raise
    except APIError as e:
        logger.error(f"[API ERROR] Company: {company_name} - {str(e)}")
        raise
    except Exception as e:
        logger.error(f"[ASSESSMENT FAILED] Company: {company_name} - {str(e)}", exc_info=True)
        raise


def generate_email(company: dict, on_event: OnEvent | None = None) -> dict:
    """
    Generate an outreach email using stored assessment data.
    ``company`` may be a raw companies row OR a full detail dict
    (with ``enrichment`` key) from get_company_detail().
    We always prefer stored assessment over re-running research.
    """
    def _emit(step, label, status, metadata=None):
        if on_event:
            on_event(step, label, status, metadata)

    company = dict(company)
    company_name = company.get("legal_name", "Unknown")
    company_id = company.get("id", "Unknown")

    logger.info(f"[EMAIL GENERATION START] Company: {company_name} (ID: {company_id})")

    try:
        _emit("outreach_prep", "Preparing outreach context", "running")
        # ------------------------------------------------------------------
        # Pull from stored enrichment first (fast path — no new Tavily call)
        # ------------------------------------------------------------------
        stored_enrichment = company.get("enrichment") or {}
        na = stored_enrichment.get("narrative_assessment") or {}
        if isinstance(na, str):
            import json as _json
            try:
                na = _json.loads(na)
            except Exception:
                na = {}

        stored_contacts = na.get("contacts") or company.get("contacts") or []
        stored_opening_angle = (
            na.get("opening_angle")
            or stored_enrichment.get("opening_angle")
            or company.get("opening_angle", "")
        )
        stored_personalisation = na.get("personalisation") or stored_enrichment.get("personalisation") or {}
        stored_discovery = na.get("discovery_questions") or stored_enrichment.get("discovery_questions") or []
        digital = na.get("digital_presence") or {}
        domain = digital.get("domain", "")

        # Personalisation reference / avoid / discovery as text.
        # The assessment prompt's contract key is "reference_points" —
        # reading only the legacy "reference" key meant the reference facts
        # never reached the email prompt at all.
        perso_ref = ""
        if isinstance(stored_personalisation, dict):
            refs = (
                stored_personalisation.get("reference_points")
                or stored_personalisation.get("reference")
                or []
            )
            avoids = stored_personalisation.get("avoid", [])
            if refs:
                perso_ref += "Reference facts:\n" + "\n".join(f"- {r}" for r in refs)
            if avoids:
                perso_ref += "\nAvoid:\n" + "\n".join(f"- {a}" for a in avoids)
        discovery_text = "\n".join(f"- {q}" for q in stored_discovery) if stored_discovery else ""

        # Recipient from stored contacts. Confidence is now a per-field
        # object ({"identity": {...}, "overall": {...}, ...}), not a bare
        # number — map overall.level to a sortable rank instead of the old
        # c.get("confidence", 0), which would compare dicts and crash.
        _LEVEL_RANK = {"high": 3, "medium": 2, "low": 1}

        def _overall_rank(c: dict) -> int:
            conf = c.get("confidence")
            if isinstance(conf, dict):
                level = (conf.get("overall") or {}).get("level", "low")
                return _LEVEL_RANK.get(level, 0)
            if isinstance(conf, (int, float)):  # legacy flat shape, still tolerated
                return int(conf)
            return 0

        recipient_name = "the team"
        recipient_role = "General contact"
        if stored_contacts:
            best = max(stored_contacts, key=_overall_rank)
            recipient_name = best.get("name") or "the team"
            recipient_role = best.get("role") or "Director"

        # Company context block
        cbi_auths = company.get("cbi_authorisations", [])
        auth_str = ", ".join(cbi_auths) if isinstance(cbi_auths, list) and cbi_auths else ""
        company_context_parts = [
            f"Legal name: {company.get('legal_name', 'N/A')}",
            f"County: {company.get('county', 'N/A')}",
            f"CRO status: {company.get('cro_status', 'N/A')}",
            f"Incorporation date: {company.get('incorporation_date', 'N/A')}",
        ]
        if auth_str:
            company_context_parts.append(f"CBI authorisations: {auth_str}")
        if company.get("trading_name"):
            company_context_parts.append(f"Trading name: {company['trading_name']}")
        if domain:
            company_context_parts.append(f"Official website: {domain}")
        company_context = "\n".join(company_context_parts)

        # Rich assessment context (replaces web_context when available)
        exec_summary = na.get("executive_summary") or stored_enrichment.get("executive_summary") or ""
        web_context = exec_summary[:1500] if exec_summary else "No prior assessment summary available."
        if perso_ref:
            web_context += f"\n\n{perso_ref}"
        if discovery_text:
            web_context += f"\n\nDiscovery priorities:\n{discovery_text}"

        # Verified atomic facts from research_coverage — computed during
        # assessment but previously never passed into the email prompt at
        # all, even though they're often more concrete/citable than the
        # executive_summary (which is itself already a compressed
        # narrative, and opening_angle is a compression of that narrative
        # again). Gives the email model fresh material instead of only
        # ever paraphrasing the same already-paraphrased text.
        research_coverage_data = na.get("research_coverage") or stored_enrichment.get("research_coverage") or {}
        verified_list = research_coverage_data.get("verified") if isinstance(research_coverage_data, dict) else None
        verified_facts = "\n".join(f"- {v}" for v in verified_list) if verified_list else "(none beyond the above)"

        outreach_user = load_prompt("outreach_user.md")
        prompt = outreach_user.format(
            company_name=company.get("legal_name", "the company"),
            recipient_name=recipient_name,
            recipient_role=recipient_role,
            opening_angle=stored_opening_angle or "Focus on the firm's regulatory footprint and ask about their payment collection workflow.",
            company_context=company_context,
            web_context=web_context,
            verified_facts=verified_facts,
        )

        logger.info(f"[LLM EMAIL GENERATION START] Company: {company_name} - Model: {LLM_MODEL}")

        system_prompt = load_prompt("outreach_system.md")
        grounding_text = f"{stored_opening_angle}\n{company_context}\n{verified_facts}"

        data = complete_json(
            system_prompt=system_prompt,
            user_prompt=prompt,
            temperature=0.5,
            max_tokens=2048,
            required_keys=["subject", "body"],
        )

        # ------------------------------------------------------------------
        # Email quality guard — previously nonexistent. complete_json() only
        # checks that subject/body are present, so a well-formed-but-generic
        # email (cliches, a fixed company-agnostic PayBrix description, no
        # actual reference to this firm) would go straight to storage. Give
        # the model one shot at a corrected retry with the specific issues
        # named, then ship the result either way but flag what's wrong
        # rather than silently hiding it.
        check = evaluate_email(data.get("subject", ""), data.get("body", ""), grounding_text)
        if not check["passed"]:
            logger.warning(f"[EMAIL QUALITY] Company: {company_name} — issues: {check['issues']}")
            retry_prompt = (
                prompt
                + "\n\nYour previous draft had specific problems — fix all of them in this rewrite:\n"
                + "\n".join(f"- {issue}" for issue in check["issues"])
                + "\nDo not just reword around the problem — address it directly."
            )
            try:
                retry_data = complete_json(
                    system_prompt=system_prompt,
                    user_prompt=retry_prompt,
                    temperature=0.6,
                    max_tokens=2048,
                    required_keys=["subject", "body"],
                )
                recheck = evaluate_email(retry_data.get("subject", ""), retry_data.get("body", ""), grounding_text)
                logger.info(f"[EMAIL QUALITY] Company: {company_name} — retry {'PASSED' if recheck['passed'] else 'still has issues: ' + str(recheck['issues'])}")
                data = retry_data
                data["quality_warnings"] = recheck["issues"]
            except Exception as retry_err:
                logger.warning(f"[EMAIL QUALITY] Company: {company_name} — retry failed, keeping original draft: {retry_err}")
                data["quality_warnings"] = check["issues"]
        else:
            data["quality_warnings"] = []

        data["llm_model"] = LLM_MODEL
        logger.info(f"[EMAIL GENERATION COMPLETE] Company: {company_name} (ID: {company_id})")
        _emit("outreach_prep", "Preparing outreach context", "complete", {"quality_warnings": len(data.get("quality_warnings", []))})
        return data

    except RateLimitError as e:
        logger.error(f"[RATE LIMIT] Email generation for {company_name} - {str(e)}")
        raise
    except APIError as e:
        logger.error(f"[API ERROR] Email generation for {company_name} - {str(e)}")
        raise
    except Exception as e:
        logger.error(f"[EMAIL GENERATION FAILED] Company: {company_name} - {str(e)}", exc_info=True)
        raise


def refresh_contacts(company: dict, on_event: OnEvent | None = None) -> list[dict]:
    """Re-check contact channels (email/LinkedIn/phone) only. Does not
    touch qualification_score, opportunity_signal, or any other scored
    field — caller persists the returned list via
    db.dal.update_enrichment_contacts(), which patches only the
    `contacts` key inside narrative_assessment."""
    def _emit(step, label, status, metadata=None):
        if on_event:
            on_event(step, label, status, metadata)

    company = dict(company)
    company_name = company.get("legal_name", "Unknown")

    _emit("contact_discovery", "Finding contact candidates", "running")
    research = research_company(company)
    _emit("contact_discovery", "Finding contact candidates", "complete", {
        "evidence_sources": len(research.get("search_results", []) or []) + (1 if research.get("website_text") else 0),
    })

    _emit("contact_validation", "Checking contact evidence", "running")
    context = build_context(company, research)
    system_prompt = load_prompt("contacts_refresh_system.md")
    try:
        data = complete_json(
            system_prompt=system_prompt,
            user_prompt=context,
            temperature=0.3,
            max_tokens=1200,
            required_keys=["contacts"],
        )
        raw_contacts = data.get("contacts") or []
    except Exception as e:
        logger.warning(f"[Contacts Refresh] LLM contact re-check failed for {company_name}: {e}")
        raw_contacts = []

    validated = []
    for c in raw_contacts:
        try:
            validated.append(Contact.model_validate(c).model_dump())
        except ValidationError as ve:
            logger.warning(
                f"[Contacts Refresh] Dropped malformed contact for {company_name}: "
                f"{ve.errors()[0]['msg'] if ve.errors() else ve}"
            )

    # Plausibility gate on the schema-valid survivors — shape-valid junk
    # (LinkedIn-title fragments, tagline words) is filtered here.
    validated, dropped = sanitise_contacts(validated)
    if dropped:
        logger.warning(
            f"[ContactQuality] Dropped {len(dropped)} implausible refreshed "
            f"contact(s) for {company_name}: {dropped}"
        )

    if not validated:
        fallback, fallback_dropped = sanitise_contacts(extract_contacts(research, company))
        if fallback_dropped:
            logger.warning(
                f"[ContactQuality] Dropped {len(fallback_dropped)} implausible "
                f"regex-extracted contact(s) for {company_name}: {fallback_dropped}"
            )
        validated = fallback

    high_conf = sum(
        1 for c in validated
        if (c.get("confidence") or {}).get("overall", {}).get("level") == "high"
    )
    _emit("contact_validation", "Checking contact evidence", "complete", {"high_confidence_contacts": high_conf})
    return validated
