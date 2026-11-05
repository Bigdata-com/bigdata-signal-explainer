from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Optional. Each user signs in with their own key; this one only decides who inherits runs made before sign-in.
    BIGDATA_API_KEY: str = ""
    BIGDATA_API_URL: str = "https://api.bigdata.com"
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-6-sol"
    DB_PATH: str = "./data/app.db"
    # Encrypts the sign-in cookie (a Fernet key). Empty: a new one per start, so everyone signs in again after a restart.
    APP_SECRET: str = ""
    # Keep True when served over HTTPS. Browsers also accept secure cookies on http://localhost.
    COOKIE_SECURE: bool = True
    SESSION_HOURS: int = 12

    # History used to judge how unusual a move is (z-score of the lookback change).
    BASELINE_DAYS: int = 365
    # News search: the window is split into at most SEARCH_SLICES slices of up to SLICE_CHUNKS passages each.
    SEARCH_SLICES: int = 6
    SLICE_CHUNKS: int = 120
    # Co-mentioned companies in the same industry group used as the sector comparison.
    PEER_COUNT: int = 4
    EVIDENCE_PER_DRIVER: int = 3
    MAX_DRIVERS: int = 6
    BATCH_CONCURRENCY: int = 2


@lru_cache
def get_settings() -> Settings:
    return Settings()
