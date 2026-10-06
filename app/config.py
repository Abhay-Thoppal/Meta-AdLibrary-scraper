from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_key: str = ""
    host: str = "0.0.0.0"
    port: int = 8000
    proxy_url: str = ""
    headless: bool = True
    session_ttl_minutes: int = 30
    max_concurrency: int = 3
    list_cache_seconds: int = 600
    cache_db_path: str = "./cache.sqlite3"
    whisper_model: str = "small"
    whisper_device: str = "auto"
    max_video_mb: int = 100
    log_level: str = "INFO"


settings = Settings()
