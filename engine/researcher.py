import html
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse
from typing import Optional, List, Dict

import httpx
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Tavily search (replaces DuckDuckGo scraper)
from engine.research.tavily_search import search_tavily_sync

# 5s (was 10s): dead/parked .ie domains dominated worst-case research time —
# every unreachable candidate cost a full timeout slot.
FETCH_TIMEOUT = 5.0
FETCH_CONCURRENCY = 8
# Hard cap on how many search-result pages one company's research may fetch.
MAX_RESULT_FETCHES = 15
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# Social/media domains that should never be picked as "the company website"
# — nothing previously excluded these, so a Facebook or LinkedIn page that
# happened to fuzzy-match the company name well could win the best_website
# slot instead of the real site. Also used to identify LinkedIn hits within
# the dedicated LinkedIn search below.
_SOCIAL_DOMAINS = (
    "linkedin.com", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "youtube.com", "tiktok.com",
)

# Company-suffix noise stripped before any name comparison — shared by every
# matcher below so "Acme Financial Services Ltd" and "Acme" compare cleanly.
_SUFFIX_RE = re.compile(
    r"\b(limited|ltd|plc|llc|inc|corp|co|company|financial|services|money|"
    r"advisers|advisors|group|holdings)\b",
    re.IGNORECASE,
)


def _search_tavily(query: str, max_results: int = 10) -> list[dict]:
    """Search using Tavily API and extract result links/titles."""
    try:
        return search_tavily_sync(query, max_results=max_results)
    except Exception as e:
        # Log error but don't crash - fall back to empty results
        print(f"[researcher] Tavily search failed for '{query}': {e}")
        return []


# Markers for a bot/WAF challenge page (Cloudflare "Just a moment...",
# generic "Attention Required" interstitials, etc.) as opposed to a genuine
# 403/dead-domain — deliberately narrow and text/header-based only. This is
# used purely to report an honest "found but couldn't verify" note instead
# of a false "no website" — never to attempt to get past the challenge
# itself, which we don't do.
_BOT_CHALLENGE_MARKERS = (
    "just a moment", "checking your browser", "cf-chl", "attention required",
    "enable javascript and cookies", "verify you are human", "captcha",
)


def _is_bot_challenge(resp) -> bool:
    if resp.status_code not in (403, 503):
        return False
    server = (resp.headers.get("server") or "").lower()
    body_sample = (resp.text or "")[:2000].lower()
    if "cloudflare" in server and resp.status_code in (403, 503):
        return True
    return any(marker in body_sample for marker in _BOT_CHALLENGE_MARKERS)


def _fetch_text(url: str, max_chars: int = 8000, blocked: Optional[list] = None) -> Optional[str]:
    """Fetch a page and extract readable text using a real HTML parser
    (BeautifulSoup) instead of hand-rolled tag-stripping regexes — handles
    malformed markup, nested tags, and HTML entities correctly.

    `blocked`, if given, collects URLs that returned a bot/WAF challenge
    response rather than genuinely having no content — this lets the caller
    report "found but couldn't verify" instead of silently equating a block
    with "doesn't exist" (the Clements Insurance / clementsins.com case:
    real site, Cloudflare-protected, our fetch gets a 403 challenge page —
    we deliberately do not try to get past that)."""
    try:
        with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": USER_AGENT})
            if blocked is not None and _is_bot_challenge(resp):
                blocked.append(url)
                return None
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "noscript", "svg", "head"]):
                tag.decompose()
            text = soup.get_text(separator=" ", strip=True)
            text = html.unescape(text)
            text = re.sub(r"\s+", " ", text).strip()
            text = re.sub(r"[^\x20-\x7E\xA0-\xFF]", "", text)
            if len(text) > max_chars:
                text = text[:max_chars] + "..."
            return text if len(text) > 100 else None
    except Exception:
        return None


def _clean_name(name: str) -> str:
    """Strip legal-entity suffixes and non-alphanumerics for name comparison."""
    if not name:
        return ""
    return re.sub(r"[^a-z0-9]", "", _SUFFIX_RE.sub("", name.lower()))


# Insurance/financial-sector vocabulary. A candidate page with NO identity
# anchor and NONE of these words is presumed to be a different business that
# merely shares a word with the firm's name (the "Windmill Insurances vs
# windmill tourism site" failure) — name similarity alone must not win.
_SECTOR_VOCAB_RE = re.compile(
    r"\b(insurance|insurances|insurer|insurers|broker|brokers|brokerage|"
    r"intermediary|intermediaries|underwrit\w*|premium|premiums|"
    r"policy|policies|cover|claims?|pension|pensions|mortgage|mortgages|"
    r"financial advis\w*)\b",
    re.IGNORECASE,
)


def _has_identity_anchor(content: str, company: Optional[dict]) -> bool:
    """True if the page carries a firm-specific identifier (eircode, CRO
    number, CBI reference, or the registered street address) — near-certain
    proof the page belongs to this firm, however unlike the domain name is."""
    if not content or not company:
        return False
    haystack = content.lower()
    squashed = re.sub(r"\s+", "", haystack)

    eircode = (company.get("eircode") or "").replace(" ", "").lower()
    if len(eircode) == 7 and eircode in squashed:
        return True
    cro = str(company.get("cro_number") or "").strip().lower()
    if len(cro) >= 5 and cro in haystack:
        return True
    cbi = str(company.get("cbi_reference") or "").strip().lower()
    if len(cbi) >= 5 and cbi in haystack:
        return True
    street = (company.get("registered_address") or "").split(",")[0].strip().lower()
    if len(street) >= 8 and street in haystack:
        return True
    return False


def _score_website_match(
    url: str,
    company_name: str,
    trading_name: str = None,
    content: str = None,
    company: Optional[dict] = None,
) -> int:
    """Score how likely `url` is the company's real website (0-100).

    Base score: rapidfuzz name-vs-domain/content similarity (as in
    ingestion/cro_resolver.py). On top of that, two identity gates:
      - an identity anchor in the content (firm's eircode/CRO/CBI/street
        address) floors the score at 75 — the page is provably theirs;
      - no anchor AND no insurance-sector vocabulary caps the score at 25 —
        a name-alike page about a different business must never be accepted.
    """
    domain = urlparse(url).netloc.lower()
    domain_clean = domain.replace("www.", "").split(".")[0]
    if not domain_clean:
        return 0

    candidates = [_clean_name(n) for n in (company_name, trading_name) if n]
    candidates = [c for c in candidates if c]
    if not candidates:
        return 0

    # Domain-vs-name similarity carries most of the weight — WRatio handles
    # partial/substring/reordered matches well (e.g. "abm" in "abmfinancial").
    domain_score = max(fuzz.WRatio(domain_clean, c) for c in candidates)
    score = domain_score * 0.7

    # Content relevance — does the crawled page actually mention the company?
    if content:
        content_norm = re.sub(r"[^a-z0-9 ]", "", content.lower())
        content_score = max(fuzz.partial_ratio(c, content_norm) for c in candidates)
        score += content_score * 0.3

        if _has_identity_anchor(content, company):
            score = max(score, 75.0)
        elif not _SECTOR_VOCAB_RE.search(content):
            score = min(score, 25.0)

    return round(min(score, 100))


# Common paths for the contact/about/team page on a small business site —
# this is where named decision-makers and direct emails/phones usually live,
# and it's frequently a *different* page from whichever one scored best as
# "the company website" (which is often the homepage — company history and
# service descriptions, not who to contact). Confirmed case: abingdon.ie's
# homepage has none of this; abingdon.ie/contact/ has named directors with
# direct phone/mobile/email each. Without this, contact extraction silently
# fails not because the site lacks the info, but because we never looked.
_CONTACT_PATH_CANDIDATES = [
    "/contact/", "/contact", "/contact-us",
    "/about/", "/about", "/about-us",
    "/team", "/team/", "/our-team", "/our-team/",
    "/meet-the-team", "/our-people", "/people", "/staff",
]

# Nav links whose path or anchor text matches this are candidate people pages
# — covers naming the fixed path list can't ("/who-we-are", "/leadership").
_PEOPLE_LINK_RE = re.compile(
    r"(contact|team|people|staff|about|meet|who[-_ ]?we[-_ ]?are|advis[eo]rs|"
    r"our[-_ ]people|leadership|management)",
    re.IGNORECASE,
)

# A page "bears people" if it names a role-holder or exposes a direct
# email/phone — the pages contact extraction can actually work with.
_PEOPLE_SIGNAL_RE = re.compile(
    r"(?:Director|Founder|Co-founder|CEO|Managing Director|Partner|Owner|"
    r"Adviser|Advisor|Principal|Manager|Head of)"
    r"|[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
    r"|(?:\+353\s?\d{1,2}|\b0\d{1,2})[\s\-/]?\d{3}[\s\-]?\d{3,4}\b"
)


def _looks_people_bearing(content: Optional[str]) -> bool:
    return bool(content and _PEOPLE_SIGNAL_RE.search(content))


def _fetch_site_links(base_url: str, max_links: int = 6) -> list[str]:
    """Fetch base_url's raw HTML and return same-domain nav links that look
    like contact/team/about pages. This is how team pages with site-specific
    naming (MyLife's 'team' category page) get found at all — the fixed path
    list alone cannot cover every site's routing."""
    try:
        with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(base_url, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
    except Exception:
        return []

    base_netloc = urlparse(base_url).netloc.lower().replace("www.", "")
    found: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        abs_url = urljoin(base_url, href).split("#")[0]
        parsed = urlparse(abs_url)
        if parsed.scheme not in ("http", "https"):
            continue
        if parsed.netloc.lower().replace("www.", "") != base_netloc:
            continue
        if abs_url.rstrip("/") == base_url.rstrip("/"):
            continue
        label = f"{parsed.path} {anchor.get_text(' ', strip=True)}"
        if not _PEOPLE_LINK_RE.search(label):
            continue
        if abs_url not in found:
            found.append(abs_url)
        if len(found) >= max_links:
            break
    return found


def _try_contact_pages(base_url: str, fetched_content: dict) -> list[tuple[str, str]]:
    """Probe nav-discovered links plus common contact/about/team paths on
    base_url's domain. Returns ALL people-bearing pages plus the contact page
    itself (deduplicated, capped at 3) — a form-only /contact page must never
    swallow a fully-staffed /our-team page (the Pinnacle/MyLife failure).
    Reuses the fetched_content cache to avoid double-fetching."""
    parsed = urlparse(base_url)
    if not parsed.netloc:
        return []
    root = f"{parsed.scheme}://{parsed.netloc}"

    candidates: list[str] = []
    for url in _fetch_site_links(base_url) + [root + path for path in _CONTACT_PATH_CANDIDATES]:
        if url in candidates or url in fetched_content:
            continue
        if url.rstrip("/") == base_url.rstrip("/"):
            continue
        candidates.append(url)
    candidates = candidates[:14]
    if not candidates:
        return []

    hits: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(FETCH_CONCURRENCY, len(candidates))) as pool:
        future_to_url = {pool.submit(_fetch_text, url): url for url in candidates}
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                content = future.result()
            except Exception:
                content = None
            fetched_content[url] = content
            if content and len(content) > 100:
                hits[url] = content

    if not hits:
        return []

    selected = [(url, content) for url, content in hits.items() if _looks_people_bearing(content)]
    contact_hit = next(
        ((url, content) for url, content in hits.items() if "contact" in url.lower()),
        None,
    )
    if contact_hit and contact_hit not in selected:
        selected.append(contact_hit)
    if not selected:
        selected = [next(iter(hits.items()))]

    # /contact and /contact/ serve the same page — dedupe by content prefix.
    deduped: list[tuple[str, str]] = []
    seen_prefixes: set[str] = set()
    for url, content in selected:
        prefix = content[:200]
        if prefix in seen_prefixes:
            continue
        seen_prefixes.add(prefix)
        deduped.append((url, content))
    return deduped[:3]


def _collect_social_links(
    results: list[dict],
    name: str,
    trading_name: Optional[str],
    social_links: dict[str, str],
) -> None:
    """Capture the firm's own social profiles from search results for the
    digital-presence card. A social URL is kept only when the firm's core
    name token appears in the URL/title — a random cafe's Facebook page that
    surfaced in the same search must not be attributed to the firm."""
    cores = []
    for candidate in (trading_name, name):
        if candidate:
            tokens = re.findall(r"[a-z0-9]+", candidate.lower())
            if tokens and len(tokens[0]) >= 4:
                cores.append(tokens[0])
    if not cores:
        return

    for r in results:
        url = r.get("url") or ""
        lowered = url.lower()
        for domain in _SOCIAL_DOMAINS:
            if domain not in lowered:
                continue
            platform = domain.split(".")[0]
            if platform == "x":
                platform = "twitter"
            if platform not in social_links:
                haystack = re.sub(r"[^a-z0-9]", "", lowered + (r.get("title") or "").lower())
                if any(core in haystack for core in cores):
                    social_links[platform] = url
            break


def research_company(company: dict) -> dict:
    """Research a company online. Returns dict with website_text,
    search_results, linkedin_results and social_links."""
    _t_start = time.perf_counter()
    name = company.get("legal_name", "")
    trading_name = company.get("trading_name", "")
    cro_number = company.get("cro_number", "")
    cbi_ref = company.get("cbi_reference", "")
    county = company.get("county", "")
    address = company.get("registered_address", "")
    
    # Build comprehensive search queries
    queries = []
    
    # Primary: trading name searches (often more distinctive)
    if trading_name:
        # Extract the core part of trading name (e.g., "123.ie" from "123.ie Germany (FOS)")
        trading_core = re.sub(r"\s*(Germany|FOS|Ireland|UK|Ltd|Limited|Services|Financial|Group|Holdings)\b.*", "", trading_name, flags=re.IGNORECASE).strip()
        if trading_core:
            queries.append(f'"{trading_core}" Ireland')
            queries.append(f'"{trading_core}".ie')
            queries.append(f'"{trading_name}" Ireland')
        queries.append(f'"{trading_name}"')
    
    # Secondary: legal name searches
    queries.append(f'"{name}" Ireland')
    queries.append(f'"{name}"')
    
    # Tertiary: regulatory identifier searches
    if cro_number:
        queries.append(f'"{cro_number}" Ireland')
        queries.append(f'CRO {cro_number}')
    if cbi_ref:
        queries.append(f'"{cbi_ref}" Ireland')
        queries.append(f'CBI {cbi_ref}')
    
    # Quaternary: address-based searches
    if county:
        if trading_name:
            queries.append(f'"{trading_name}" {county}')
        queries.append(f'"{name}" {county}')
    if address:
        # Extract city/town from address (simple heuristic)
        address_parts = address.split(',')
        if len(address_parts) >= 2:
            city = address_parts[-2].strip()
            if city and len(city) > 3:
                if trading_name:
                    queries.append(f'"{trading_name}" {city}')
                queries.append(f'"{name}" {city}')
    
    # Quinary: domain-based searches from both names
    name_parts = re.findall(r"[a-z0-9]+", name.lower())
    if len(name_parts) >= 1:
        core_name = name_parts[0]
        queries.append(f'"{core_name}.ie"')
        queries.append(f'"{core_name}" Ireland')
    
    if trading_name:
        trading_parts = re.findall(r"[a-z0-9]+", trading_name.lower())
        if len(trading_parts) >= 1:
            trading_core = trading_parts[0]
            queries.append(f'"{trading_core}.ie"')
            queries.append(f'"{trading_core}" Ireland')

    seen_urls = set()
    fetched_content: dict[str, Optional[str]] = {}
    website_text = None
    search_results_list = []
    social_links: dict[str, str] = {}
    best_score = 0
    best_website = None
    # URLs that returned a bot/WAF challenge page rather than genuinely
    # having no content — reported separately so "no website" and "found a
    # website but couldn't verify it" are never conflated (see
    # _is_bot_challenge).
    blocked_urls: list[str] = []

    # Cap to the 4 most specific queries — avoids 12+ sequential Tavily calls
    queries = queries[:4]

    # First, try direct domain patterns from company names (only for simple names)
    def try_direct_domains(name: str) -> Optional[dict]:
        if not name:
            return None
        # Only try direct domains for very simple names (1-2 words, no spaces in core)
        parts = re.findall(r"[a-z0-9]+", name.lower())
        if not parts or len(parts) > 2:
            return None
        core = parts[0]
        # Only try if core is reasonably distinctive (3+ chars)
        if len(core) < 3:
            return None
        # Try common domain patterns
        domains_to_try = [
            f"https://{core}.ie",
            f"https://www.{core}.ie",
        ]
        for domain in domains_to_try:
            content = _fetch_text(domain)
            fetched_content[domain] = content
            if content:
                score = _score_website_match(domain, name, trading_name, content, company=company)
                return {"url": domain, "content": content, "score": score, "title": name}
        return None

    # Try trading name domains first (only if it looks like a domain)
    if trading_name:
        # Check if trading name looks like it contains a domain
        if re.search(r"\.ie|\.com|\.net|\.org", trading_name, re.IGNORECASE):
            trading_core = re.sub(r"\s*(Germany|FOS|Ireland|UK|Ltd|Limited|Services|Financial|Group|Holdings)\b.*", "", trading_name, flags=re.IGNORECASE).strip()
            if trading_core and len(trading_core.split()) <= 2:
                result = try_direct_domains(trading_core)
                if result:
                    best_score = result["score"]
                    best_website = result

    # Fire all queries concurrently — Tavily latency (1-3s each) was paid
    # serially per query before, and each round then ran its own fetch wave.
    # One merged round: search in parallel, dedupe, cap, fetch once.
    all_results: list[dict] = []
    if queries:
        with ThreadPoolExecutor(max_workers=min(4, len(queries))) as pool:
            future_to_query = {pool.submit(_search_tavily, q, 5): q for q in queries}
            for future in as_completed(future_to_query):
                try:
                    all_results.extend(future.result() or [])
                except Exception as e:
                    print(f"[researcher] Tavily search failed for '{future_to_query[future]}': {e}")

    new_urls = []
    for r in all_results:
        if r["url"] in seen_urls:
            continue
        seen_urls.add(r["url"])
        new_urls.append(r)

    # Capture the firm's own social profiles for the digital-presence
    # card before excluding social domains from website scoring.
    _collect_social_links(new_urls, name, trading_name, social_links)

    # Never fetch/score social/media domains as "the company website" —
    # nothing previously excluded these.
    new_urls = [r for r in new_urls if not any(d in r["url"].lower() for d in _SOCIAL_DOMAINS)]

    # Hard cap: a flood of low-quality results must not become a flood of
    # page fetches — each dead URL costs up to FETCH_TIMEOUT seconds.
    new_urls = new_urls[:MAX_RESULT_FETCHES]

    if new_urls:
        with ThreadPoolExecutor(max_workers=FETCH_CONCURRENCY) as pool:
            future_to_result = {pool.submit(_fetch_text, r["url"], 8000, blocked_urls): r for r in new_urls}
            for future in as_completed(future_to_result):
                r = future_to_result[future]
                url = r["url"]
                try:
                    content = future.result()
                except Exception:
                    content = None
                fetched_content[url] = content
                search_results_list.append(f"{r['title']}: {url}")
                if content is None:
                    # A page we couldn't read can never be verified as
                    # the firm's site — don't let a bare domain-name
                    # similarity win on an unreadable page.
                    continue
                score = _score_website_match(url, name, trading_name, content, company=company)

                if score > best_score and score >= 30:  # Minimum threshold of 30
                    best_score = score
                    best_website = {"url": url, "content": content, "score": score, "title": r["title"]}

    # Only report a blocked URL as "this firm's site, unverified" when its
    # domain plausibly matches the firm's name — a Cloudflare challenge on
    # some unrelated result that happened to surface in search must not be
    # attributed to this company. No content is available to check, so this
    # relies on domain-vs-name similarity alone (same fuzzy match used
    # elsewhere, just without the content half of the score).
    blocked_candidates = [
        url for url in dict.fromkeys(blocked_urls)
        if _score_website_match(url, name, trading_name, content=None, company=company) >= 40
    ]

    # Use the best scoring website if found
    if best_website:
        main_content = best_website['content'][:5000] if best_website['content'] else ''
        website_text = f"[{best_website['title']}]({best_website['url']})\n{main_content}"

        # The best-matching page is often the homepage — company history,
        # services — not the page with named contacts. Probe nav-discovered
        # and common contact/team paths and append EVERY people-bearing page
        # found (a form-only contact page must not displace the team page).
        for contact_url, contact_content in _try_contact_pages(best_website['url'], fetched_content):
            website_text += f"\n\n--- Additional page: {contact_url} ---\n{contact_content[:4000]}"

        linkedin_results = _search_linkedin(name, trading_name)
        print(
            f"[researcher] Research for '{name}' took {time.perf_counter() - _t_start:.1f}s "
            f"({len(fetched_content)} pages fetched, site verified)"
        )
        return {
            "website_text": website_text,
            "search_results": search_results_list[:5],
            "linkedin_results": linkedin_results,
            "social_links": social_links,
            "blocked_candidates": blocked_candidates,
        }

    # No candidate cleared the identity threshold: the honest answer is "no
    # verified website". The previous fallback here attached an arbitrary
    # below-threshold URL as [Result](...) — which is how an unrelated
    # tourism site became a firm's official website and sourced a wrong
    # email. Unverified pages must never masquerade as the firm's site.
    print(
        f"[researcher] No verified website for '{name}' — best candidate scored "
        f"{best_score} (threshold 30); reporting no website rather than guessing."
    )
    linkedin_results = _search_linkedin(name, trading_name)
    print(
        f"[researcher] Research for '{name}' took {time.perf_counter() - _t_start:.1f}s "
        f"({len(fetched_content)} pages fetched, no verified site)"
    )
    if blocked_candidates:
        print(
            f"[researcher] {len(blocked_candidates)} candidate(s) for '{name}' blocked "
            f"automated access (bot/WAF challenge), reporting as unverified rather than absent: "
            f"{blocked_candidates}"
        )
    return {
        "website_text": None,
        "search_results": search_results_list[:5],
        "linkedin_results": linkedin_results,
        "social_links": social_links,
        "blocked_candidates": blocked_candidates,
    }


def _search_linkedin(name: str, trading_name: str = "") -> list[str]:
    """One targeted search for the company's LinkedIn presence. Doesn't fetch
    linkedin.com pages (they're behind an auth wall and wouldn't return
    useful text anyway) — just surfaces search-result titles/snippets/URLs,
    which are usually enough to see a named individual's profile ("David
    Ryder - Director - Abingdon Insurances | LinkedIn") or the company page,
    for the LLM to correlate against contacts already found on the website.
    One extra Tavily call per company — kept to exactly one on purpose,
    cost-consciousness was an explicit requirement here."""
    query_name = trading_name or name
    if not query_name:
        return []
    try:
        results = _search_tavily(f'"{query_name}" LinkedIn', max_results=5)
    except Exception as e:
        print(f"[researcher] LinkedIn search failed for '{query_name}': {e}")
        return []
    hits = []
    for r in results:
        url = r.get("url", "")
        if "linkedin.com" in url.lower():
            hits.append(f"{r.get('title', '')} — {url}")
    return hits[:5]


# Backward compatibility - async version for future use
async def research_company_async(company: dict) -> dict:
    """Async version of research_company using Tavily async API."""
    # For now, delegate to sync version
    # Can be updated to use true async when needed
    return research_company(company)