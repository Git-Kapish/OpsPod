"""Versioned prompts: opspod/prompts/<name>/v<N>.md, pinned in config/prompts.yaml.

The prompt id (e.g. "analyze@v1") is recorded in every audit row and trace span. Any prompt
change must pass the eval gate. All case text is untrusted: wrap_untrusted() delimits it.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml

from opspod.core.errors import ConfigError

PROMPT_DIR = Path(__file__).resolve().parent
PIN_FILE = PROMPT_DIR.parents[1] / "config" / "prompts.yaml"
OPEN, CLOSE = "<untrusted_case_text>", "</untrusted_case_text>"


@dataclass(frozen=True)
class Prompt:
    name: str
    version: int
    text: str

    @property
    def id(self) -> str:
        return f"{self.name}@v{self.version}"


def load_prompt(name: str, version: int | None = None) -> Prompt:
    if version is None:
        pins = yaml.safe_load(PIN_FILE.read_text(encoding="utf-8")) or {}
        if name not in pins:
            raise ConfigError(f"prompt {name!r} is not pinned in config/prompts.yaml")
        version = int(str(pins[name]).lstrip("v"))
    path = PROMPT_DIR / name / f"v{version}.md"
    if not path.is_file():
        raise ConfigError(f"missing prompt file {path.name} for {name!r}")
    return Prompt(name, version, path.read_text(encoding="utf-8").strip())


def wrap_untrusted(text: str) -> str:
    """Delimit user text as data; strip our own delimiters so it cannot close the block early."""
    cleaned = text.replace(OPEN, "").replace(CLOSE, "")
    return f"{OPEN}\n{cleaned}\n{CLOSE}"
