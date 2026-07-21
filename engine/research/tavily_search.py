"""
engine/research/tavily_search.py
Tavily Search API integration for web research.
Replaces DuckDuckGo HTML scraping which hit bot verification CAPTCHAs.
"""
import os
import httpx
from typing import List, Dict, Optional

TAVILY_URL = "https://api.tavily.com/search"

# API key must come from the environment — no hardcoded fallback.
# (Previously this defaulted to a live dev key baked into source, which
# meant it kept working even with a broken/missing .env and got pushed to
# the repo. Fail loudly instead of masking a misconfiguration.)
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def search_tavily_sync(
    query: str,
    max_results: int = 10,
    api_key: Optional[str] = None,
    search_depth: str = "basic",
    country: str = "ireland"
) -> List[Dict]:
    """
    Synchronous wrapper for Tavily search.
    Useful for non-async contexts.
    """
    key = api_key or TAVILY_API_KEY
    if not key:
        raise ValueError("TAVILY_API_KEY not set in environment or passed as argument")
    
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT
    }
    payload = {
        "api_key": key,
        "query": query,
        "max_results": max_results,
        "search_depth": search_depth,
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
        "country": country
    }
    
    with httpx.Client(timeout=20.0) as client:
        resp = client.post(TAVILY_URL, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    
    results = []
    for r in data.get("results", []):
        results.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("content", "")
        })
    
    return results


if __name__ == "__main__":
    import sys

    query = sys.argv[1] if len(sys.argv) > 1 else "insurance broker Ireland"
    results = search_tavily_sync(query, max_results=5)
    for i, r in enumerate(results, 1):
        print(f"{i}. {r['title']}")
        print(f"   URL: {r['url']}")
        print(f"   Snippet: {r['snippet'][:200]}...")
        print()