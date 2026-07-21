from functools import lru_cache
from pathlib import Path


PROMPT_DIR = Path(__file__).parent / "prompts"


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """Load a prompt file from engine/prompts by filename."""
    if Path(name).name != name:
        raise ValueError(f"Prompt names must be filenames, got: {name!r}")

    path = PROMPT_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8").strip()
