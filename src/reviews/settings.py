from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    twg_bin: str = "twg"

    bitbucket_workspace: str = ""
    bitbucket_repo: str = ""
    bitbucket_webhook_secret: str = "change-me"

    teams_webhook_url: str = ""

    redis_url: str = "redis://localhost:6379/0"
    database_url: str = "postgresql://reviews:reviews@localhost:5432/reviews"

    log_level: str = "INFO"

    sandbox_enabled: bool = False
    sandbox_registry_user: str = ""
    sandbox_registry_token: str = ""

    autonomy_config: str = "config/autonomy.yaml"
    repo_profile_config: str = "config/repo_profile.emotivoo-v2.yaml"


@lru_cache
def get_settings() -> Settings:
    return Settings()
