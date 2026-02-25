from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "transport"
    db_password: str = "transport_dev"
    db_name: str = "transport_db"

    # App
    debug: bool = True
    app_title: str = "SCC200 Transport API"
    api_prefix: str = "/api/v1"

    # STOMP (National Rail feed)
    stomp_host: str = ""
    stomp_port: int = 61613
    stomp_user: str = ""
    stomp_password: str = ""

    @property
    def database_url(self) -> str:
        return f"postgresql+asyncpg://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
