"""Loads config.yaml and the Gemini API key.

The key comes from the process environment. Locally, a .env file in the
project root is loaded first if it exists; on Vercel the key is a project
environment variable. The key is never printed or logged.
"""

from functools import lru_cache
import os
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
ENV_PATH = PROJECT_ROOT / ".env"


class ConfigError(RuntimeError):
    """Raised when config.yaml or a required environment variable is missing."""


@lru_cache(maxsize=1)
def load_config() -> dict:
    """Return the parsed config.yaml (cached after the first call)."""
    if not CONFIG_PATH.exists():
        raise ConfigError(f"config.yaml not found at {CONFIG_PATH}")
    with CONFIG_PATH.open(encoding="utf-8") as f:
        config = yaml.safe_load(f)
    if not isinstance(config, dict):
        raise ConfigError("config.yaml is empty or not a mapping")
    return config


def get_gemini_key() -> str:
    """Return GEMINI_API_KEY, or raise ConfigError if it is missing or empty."""
    if ENV_PATH.exists():
        from dotenv import load_dotenv

        # Real environment variables (e.g. on Vercel) take precedence over .env.
        load_dotenv(ENV_PATH, override=False)

    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise ConfigError(
            "GEMINI_API_KEY is not set. Add it to .env locally "
            "(see .env.example) or to the Vercel project's environment variables."
        )
    return key
