"""
engine/llm/provider.py
LLM Provider Abstraction — LiteLLM unified client.

Drop-in replacement for the previous bare OpenAI client. Callers (assessor.py,
summarise.py, etc.) use the same complete() / complete_json() signatures.

LiteLLM benefits over bare openai client:
  - Groq, OpenAI, Anthropic, OpenRouter all work under one interface
  - Automatic cost tracking per call (litellm.completion_cost)
  - Model fallback chains via fallbacks= kwarg
  - Per-call usage metadata (tokens, cost)
"""
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional, Dict, Any

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent.parent
load_dotenv(ROOT / ".env")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider configuration (unchanged env var names)
# ---------------------------------------------------------------------------
LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "groq").lower()
LLM_BASE_URL = os.getenv("LLM_BASE_URL")
LLM_API_KEY = os.getenv("LLM_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL")

# Was hardcoded to 60.0 at each NVIDIA client construction site, silently
# ignoring this env var entirely. A live test against the real endpoint
# with a production-sized prompt took 33s to complete cleanly and well
# under the token budget — so 30s (the previous .env value, never actually
# applied) would have cut off a legitimately-in-progress response, not just
# a hung one. Defaulting to 75 to give real (denser) company data headroom
# while still failing before the frontend's own request timeout.
_LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "75"))

PROVIDER_DEFAULT_MODELS = {
    "xai": "grok-beta",
    "openai": "gpt-4o-mini",
    "groq": "llama-3.3-70b-versatile",
    "openrouter": "openrouter/auto",
    "anthropic": "claude-3-5-haiku-20241022",
    "nvidia": "nvidia/nemotron-3-ultra-550b-a55b",
}

# Resolve API key from provider-specific env var if not explicitly set
if not LLM_API_KEY:
    LLM_API_KEY = os.getenv(f"{LLM_PROVIDER.upper()}_API_KEY")

# Resolve model from provider default if not explicitly set
if not LLM_MODEL:
    LLM_MODEL = PROVIDER_DEFAULT_MODELS.get(LLM_PROVIDER, "llama-3.3-70b-versatile")

# JSON mode — some providers/models don't support response_format
LLM_JSON_MODE = os.getenv("LLM_JSON_MODE", "true").lower() == "true"

# Fallback chain — comma-separated "provider:model" pairs
# e.g. "groq:llama-3.3-70b-versatile,openai:gpt-4o-mini"
_FALLBACK_CHAIN_RAW = os.getenv("LLM_FALLBACK_CHAIN", "")

# NVIDIA-hosted fallback (separate mechanism — see provider.py comment where
# it's used, LiteLLM has no native "nvidia" provider so this can't go
# through the LLM_FALLBACK_CHAIN/litellm.completion() path above).
LLM_FALLBACK_MODEL = os.getenv("LLM_FALLBACK_MODEL")
LLM_FALLBACK_API_KEY = os.getenv("LLM_FALLBACK_API_KEY") or LLM_API_KEY

if not LLM_API_KEY:
    raise ValueError(
        f"LLM_API_KEY not set. Set LLM_API_KEY or {LLM_PROVIDER.upper()}_API_KEY in .env"
    )

# ---------------------------------------------------------------------------
# LiteLLM model string construction
# ---------------------------------------------------------------------------
# LiteLLM uses "{provider}/{model}" strings for non-OpenAI providers
def _litellm_model_name(provider: str, model: str) -> str:
    """Convert provider+model to LiteLLM format."""
    if provider in ("openai",):
        return model  # OpenAI models don't need a prefix
    if provider == "nvidia":
        return model  # NVIDIA uses OpenAI-compatible API, no prefix needed
    if "/" in model:
        return model  # Already prefixed
    return f"{provider}/{model}"


_MODEL = _litellm_model_name(LLM_PROVIDER, LLM_MODEL)

# Build fallback list for LiteLLM
_FALLBACKS: list[str] = []
if _FALLBACK_CHAIN_RAW:
    for entry in _FALLBACK_CHAIN_RAW.split(","):
        entry = entry.strip()
        if ":" in entry:
            prov, mdl = entry.split(":", 1)
            _FALLBACKS.append(_litellm_model_name(prov.strip(), mdl.strip()))

# Pass API keys to LiteLLM via env vars it recognises
# LiteLLM auto-reads: OPENAI_API_KEY, GROQ_API_KEY, ANTHROPIC_API_KEY, etc.
# Our GROQ_API_KEY is already set in .env — no action needed.

try:
    import litellm
    from litellm import completion as _litellm_completion
    from litellm.exceptions import (
        RateLimitError,
        APIError,
        Timeout as APITimeoutError,
        APIConnectionError,
    )
    litellm.drop_params = True          # Ignore unknown params per-provider
    litellm.set_verbose = False
    
    # NVIDIA uses OpenAI-compatible API but isn't a built-in LiteLLM provider
    # Use OpenAI client directly for NVIDIA
    if LLM_PROVIDER == "nvidia":
        import openai as _openai_pkg
        from openai import OpenAI
        _nvidia_client = None
        _nvidia_fallback_client = None

        def _get_nvidia_client():
            global _nvidia_client
            if _nvidia_client is None:
                base_url = LLM_BASE_URL or "https://integrate.api.nvidia.com/v1"
                # timeout=LLM_TIMEOUT_SECONDS (was hardcoded 60.0, and the
                # .env value was never actually read here — changing it did
                # nothing). max_retries=1 (was the openai SDK default of 2)
                # -- fail fast instead, then actually use the fallback below.
                _nvidia_client = OpenAI(api_key=LLM_API_KEY, base_url=base_url, timeout=_LLM_TIMEOUT_SECONDS, max_retries=1)
            return _nvidia_client

        def _get_nvidia_fallback_client():
            global _nvidia_fallback_client
            if _nvidia_fallback_client is None:
                base_url = LLM_BASE_URL or "https://integrate.api.nvidia.com/v1"
                _nvidia_fallback_client = OpenAI(api_key=LLM_FALLBACK_API_KEY, base_url=base_url, timeout=_LLM_TIMEOUT_SECONDS, max_retries=1)
            return _nvidia_fallback_client

        def _litellm_completion(model, messages, temperature, max_tokens, **kwargs):
            client = _get_nvidia_client()
            try:
                return client.chat.completions.create(
                    model=LLM_MODEL,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    # Previously silently dropped via **kwargs — JSON mode was
                    # never actually enforced by the API, only requested via
                    # prompt text, relying on complete_json()'s regex-extraction
                    # retry loop to catch non-compliant output after the fact.
                    response_format=kwargs.get("response_format") or _openai_pkg.NOT_GIVEN,
                )
            except Exception as e:
                # The configured LLM_FALLBACK_CHAIN was also silently dropped
                # here — litellm's fallback machinery doesn't apply to this
                # raw-client bypass path, so a rate limit or connection error
                # on NVIDIA had zero fallback despite one being configured.
                # LLM_FALLBACK_MODEL is NVIDIA-hosted too (LiteLLM has no
                # native "nvidia" provider), so it needs its own raw client
                # rather than going through litellm.completion() — with its
                # own API key so it isn't sharing the primary's rate limit.
                if LLM_FALLBACK_MODEL:
                    logger.warning(f"[LLM] NVIDIA call failed ({type(e).__name__}: {e}) — falling back to {LLM_FALLBACK_MODEL}")
                    fb_client = _get_nvidia_fallback_client()
                    return fb_client.chat.completions.create(
                        model=LLM_FALLBACK_MODEL,
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        response_format=kwargs.get("response_format") or _openai_pkg.NOT_GIVEN,
                    )
                raise
except ImportError:
    # Graceful fallback: if litellm not installed yet, use openai directly
    logger.warning(
        "[LLM] litellm not installed — falling back to bare openai client. "
        "Run: pip install litellm>=1.40"
    )
    from openai import OpenAI, APIError, RateLimitError, APITimeoutError, APIConnectionError

    _openai_client = None

    def _get_openai_client():
        global _openai_client
        if _openai_client is None:
            base_url_map = {
                "xai": "https://api.x.ai/v1",
                "openai": "https://api.openai.com/v1",
                "openrouter": "https://openrouter.ai/api/v1",
                "groq": "https://api.groq.com/openai/v1",
                "nvidia": "https://integrate.api.nvidia.com/v1",
            }
            base_url = LLM_BASE_URL or base_url_map.get(LLM_PROVIDER, "https://api.openai.com/v1")
            _openai_client = OpenAI(api_key=LLM_API_KEY, base_url=base_url, timeout=120.0)
        return _openai_client

    def _litellm_completion(model, messages, temperature, max_tokens, **kwargs):
        client = _get_openai_client()
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return response

logger.info(f"[LLM Provider] {LLM_PROVIDER} / {_MODEL}")
if _FALLBACKS:
    logger.info(f"[LLM Provider] Fallback chain: {_FALLBACKS}")


# ---------------------------------------------------------------------------
# Cost tracker (lightweight — per-process accumulator)
# ---------------------------------------------------------------------------
_cost_tracker: dict[str, float] = {}


def get_session_cost() -> dict[str, float]:
    """Return accumulated cost per model for this process lifetime."""
    return dict(_cost_tracker)


def _record_cost(model: str, response) -> None:
    """Best-effort cost recording — never raises."""
    try:
        import litellm
        cost = litellm.completion_cost(completion_response=response)
        _cost_tracker[model] = _cost_tracker.get(model, 0.0) + cost
        logger.debug(f"[LLM Cost] {model}: ${cost:.6f} (session total: ${_cost_tracker[model]:.6f})")
    except Exception:
        pass  # Cost tracking is informational only


# ---------------------------------------------------------------------------
# Public API (identical signatures to previous provider.py)
# ---------------------------------------------------------------------------

def complete(
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.7,
    max_tokens: int = 4000,
    response_format: Optional[Dict[str, str]] = None,
) -> str:
    """
    Complete a prompt using the configured LLM provider via LiteLLM.

    Args:
        system_prompt:   System message for the LLM
        user_prompt:     User message for the LLM
        temperature:     Sampling temperature (0–2)
        max_tokens:      Maximum tokens to generate
        response_format: Optional response format (e.g. {"type": "json_object"})

    Returns:
        The LLM's response text

    Raises:
        RateLimitError, APIError, APITimeoutError, APIConnectionError
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_prompt},
    ]

    kwargs: dict[str, Any] = {
        "model": _MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    if response_format:
        kwargs["response_format"] = response_format

    if _FALLBACKS:
        kwargs["fallbacks"] = _FALLBACKS

    try:
        response = _litellm_completion(**kwargs)
        _record_cost(_MODEL, response)
        finish_reason = getattr(response.choices[0], "finish_reason", None)
        if finish_reason == "length":
            # The model hit max_tokens before finishing — for a JSON-schema
            # response this reliably produces a syntactically-valid-but-thin
            # result (fields silently null/empty) rather than a parse
            # failure, so complete_json's parse-retry never catches it.
            # This is the direct signal for that failure mode; without it,
            # truncation is only ever visible indirectly, after the fact,
            # as missing data in the DB.
            logger.warning(
                f"[LLM] Response truncated at max_tokens={max_tokens} "
                f"(finish_reason=length) — output is likely incomplete."
            )
        return response.choices[0].message.content
    except RateLimitError as e:
        logger.error(f"[LLM] Rate limit exceeded: {e}")
        raise
    except APITimeoutError as e:
        logger.error(f"[LLM] Request timeout: {e}")
        raise
    except APIConnectionError as e:
        logger.error(f"[LLM] Connection error: {e}")
        raise
    except APIError as e:
        logger.error(f"[LLM] API error: {e}")
        raise


def _is_placeholder_json(obj: Dict[str, Any]) -> bool:
    """Return True if the dict looks like a prompt template echoed back (all values empty/null or '...')."""
    if not obj:
        return True
    for v in obj.values():
        if v is None:
            continue
        if not isinstance(v, str):
            return False  # has a real non-string value (number, list, etc.)
        if v.strip() not in ("", "..."):
            return False  # has real string content
    return True


def _extract_json(text: str) -> Dict[str, Any] | None:
    """Try to extract a JSON object from text that may have surrounding narrative."""
    # Try parsing the whole text first
    try:
        parsed = json.loads(text)
        if not _is_placeholder_json(parsed):
            return parsed
    except json.JSONDecodeError:
        pass

    # Try finding a JSON code fence
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(1))
            if not _is_placeholder_json(parsed):
                return parsed
        except json.JSONDecodeError:
            pass

    # Try finding a {...} block — use non-greedy inner for safety, but fall back to
    # greedy if nested objects are required.
    for regex in (r"\{[^{}]*\}", r"\{.*\}"):
        for match in re.finditer(regex, text, re.DOTALL if ".*" in regex else 0):
            try:
                parsed = json.loads(match.group(0))
                if not _is_placeholder_json(parsed):
                    return parsed
            except json.JSONDecodeError:
                pass

    return None


def _check_required_keys(obj: Dict[str, Any], required_keys: list[str]) -> bool:
    """Return True if all required_keys exist in obj with non-empty string values."""
    for key in required_keys:
        val = obj.get(key)
        if not isinstance(val, str) or not val.strip():
            return False
    return True


def complete_json(
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.7,
    max_tokens: int = 4000,
    max_retries: int = 2,
    required_keys: list[str] | None = None,
) -> Dict[str, Any]:
    """
    Complete a prompt and return a parsed JSON response.

    Args:
        system_prompt: System message for the LLM
        user_prompt:   User message for the LLM
        temperature:   Sampling temperature (0–2)
        max_tokens:    Maximum tokens to generate
        max_retries:   Number of retries on JSON parse failure
        required_keys: Keys that must be present with non-empty string values

    Returns:
        Parsed JSON dict

    Raises:
        ValueError: If response is not valid JSON after all retries
        RateLimitError, APIError: On LLM errors
    """
    sys_prompt = system_prompt
    usr_prompt = user_prompt

    for attempt in range(max_retries + 1):
        response_text = complete(
            system_prompt=sys_prompt,
            user_prompt=usr_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"} if LLM_JSON_MODE else None,
        )

        result = _extract_json(response_text)
        if result is not None:
            if required_keys:
                if _check_required_keys(result, required_keys):
                    return result
                logger.warning(
                    "[LLM] Retry %d/%d — JSON missing required keys %s, "
                    "re-prompting",
                    attempt + 1, max_retries, required_keys,
                )
            else:
                return result

        if attempt < max_retries:
            logger.warning(
                "[LLM] Retry %d/%d — response was not valid JSON, "
                "re-prompting with stricter instruction",
                attempt + 1, max_retries,
            )
            keys_hint = ""
            if required_keys:
                keys_hint = (
                    f"\n\nYou MUST include these exact keys: {required_keys}. "
                    "Use lowercase. No extra fields."
                )
            sys_prompt = (
                sys_prompt.rstrip("\n ")
                + "\n\nCRITICAL: Output ONLY a raw JSON object. "
                "Do NOT include reasoning, explanations, markdown, or code fences. "
                "Nothing but the JSON."
                + keys_hint
            )
            usr_prompt = (
                usr_prompt.rstrip("\n ")
                + "\n\nIMPORTANT: Return ONLY valid JSON. "
                "No introductory text. No commentary. Just the JSON object."
            )
            temperature = 0.3  # Lower temperature for retries

    logger.error("[LLM] Failed to parse JSON response after %d attempts", max_retries + 1)
    raise ValueError(f"Invalid JSON response after {max_retries + 1} attempts: {response_text[:200]}")
