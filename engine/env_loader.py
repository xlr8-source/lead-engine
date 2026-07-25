"""
engine/env_loader.py

Single, explicit entry point for loading the .env file.

Three modules previously called `load_dotenv(ROOT / ".env")` independently,
which meant any `.env` present at the repo root was loaded automatically at
import. Configuration should be explicit rather than discovered: `LLM_BASE_URL`
in particular redirects every prompt, every scraped page and the
`Authorization` header carrying `LLM_API_KEY` to whatever endpoint it names,
so an unintended value there is an exfiltration path.

Resolution order:

  1. `LEAD_ENGINE_ENV_FILE` — an absolute path. This is the supported way.
  2. Repo-root `.env` — retained so a local checkout still runs, but logged
     as a warning naming the risk.
  3. `LEAD_ENGINE_STRICT_ENV=1` disables step 2 entirely: nothing is
     auto-discovered and configuration must come from the real process
     environment. This is the correct setting under Docker, where secrets
     arrive via `--env-file` and no `.env` should be in the image at all.

Real environment variables always win: `load_dotenv(override=False)` never
overwrites something already set by the container or the shell.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent

def load_env() -> Path | None:
    """Load environment configuration. Returns the file used, or None.

    Deliberately not memoised. `load_dotenv(override=False)` never clobbers a
    variable that is already set, so repeated calls from different modules at
    import time are harmless — and a cached first result would make the
    resolution rules untestable and mask a later config change.
    """
    explicit = (os.getenv("LEAD_ENGINE_ENV_FILE") or "").strip()
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            raise ValueError(
                f"LEAD_ENGINE_ENV_FILE must be an absolute path, got {explicit!r}. "
                "A relative path re-introduces the repo-root scan this exists to remove."
            )
        if not path.is_file():
            raise ValueError(f"LEAD_ENGINE_ENV_FILE points at {path}, which is not a file.")
        load_dotenv(path, override=False)
        logger.info(f"[config] Environment loaded from explicit path: {path}")
        return path

    if (os.getenv("LEAD_ENGINE_STRICT_ENV") or "").strip() == "1":
        logger.info(
            "[config] LEAD_ENGINE_STRICT_ENV=1 — no .env discovery. "
            "Configuration must come from the process environment."
        )
        return None

    fallback = ROOT / ".env"
    if fallback.is_file():
        load_dotenv(fallback, override=False)
        logger.warning(
            "[config] Loaded .env by scanning the repo root (%s). This path can be "
            "written by anything with access to this directory — an unintended .env can "
            "redirect LLM_BASE_URL and exfiltrate prompts and API keys. Set "
            "LEAD_ENGINE_ENV_FILE to an absolute path outside this repository, or "
            "LEAD_ENGINE_STRICT_ENV=1 to disable this fallback.",
            fallback,
        )
        return fallback

    logger.info("[config] No .env found; using the process environment only.")
    return None
