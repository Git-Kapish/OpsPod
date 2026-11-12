from pathlib import Path

import yaml

from opspod.core.errors import ConfigError
from opspod.policy.models import RegionPolicy

POLICY_DIR = Path(__file__).resolve().parents[2] / "config" / "policies"


def load_policy(region: str, directory: Path = POLICY_DIR) -> RegionPolicy:
    path = directory / f"{region}.yaml"
    if not path.is_file():
        raise ConfigError(f"no policy for region {region!r}")
    return RegionPolicy.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_all(directory: Path = POLICY_DIR) -> dict[str, RegionPolicy]:
    return {p.stem: load_policy(p.stem, directory) for p in sorted(directory.glob("*.yaml"))}
