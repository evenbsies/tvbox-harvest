"""Global paths and settings.

Everything that touches the filesystem resolves its location through here, so the whole
pipeline can run from any working directory (CLI, pytest, GitHub Actions).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _find_project_root() -> Path:
    """Locate the repository root: the nearest ancestor containing ``sites/`` or ``.git``."""
    env_root = os.environ.get("HARVEST_ROOT")
    if env_root:
        return Path(env_root).resolve()

    here = Path(__file__).resolve()
    for candidate in [here.parent, *here.parents]:
        if (candidate / "sites").is_dir() or (candidate / ".git").is_dir() or (
            candidate / "pyproject.toml"
        ).is_file():
            return candidate
    return Path.cwd()


PROJECT_ROOT = _find_project_root()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HARVEST_", extra="ignore")

    root: Path = PROJECT_ROOT

    # Global politeness floor: never exceed this many requests per second per site.
    max_rate_limit: float = 2.0
    default_rate_limit: float = 1.0
    default_timeout: float = 15.0
    default_retries: int = 3

    # Circuit breaker
    breaker_failures: int = 5
    breaker_cooldown_hours: int = 24

    # Budget defaults
    max_titles_per_run: int = 300
    max_requests_per_site: int = 2000

    # Dry run: adapters must not perform network calls when true.
    offline: bool = False

    @property
    def sites_dir(self) -> Path:
        return self.root / "sites"

    @property
    def registry_path(self) -> Path:
        return self.sites_dir / "registry.yaml"

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def normalized_dir(self) -> Path:
        return self.data_dir / "normalized"

    @property
    def snapshot_dir(self) -> Path:
        return self.data_dir / "snapshot"

    @property
    def tvbox_dir(self) -> Path:
        return self.data_dir / "tvbox"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def fixtures_dir(self) -> Path:
        return self.root / "tests" / "fixtures"

    def ensure_dirs(self) -> None:
        for d in (
            self.raw_dir,
            self.normalized_dir,
            self.snapshot_dir,
            self.tvbox_dir / "sites",
            self.reports_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
