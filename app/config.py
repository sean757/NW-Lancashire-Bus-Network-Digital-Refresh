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

    # Service area bounds (NW Lancashire)
    # Defaults align with the frontend Leaflet maxBounds.
    service_min_lat: float = 53.5
    service_max_lat: float = 54.3
    service_min_lon: float = -3.1
    service_max_lon: float = -2.2

    # OpenTripPlanner
    # OTP default port is 8080, but the FastAPI backend also uses 8080.
    # Run OTP on 9090 (--port 9090) and set OTP_URL=http://localhost:9090,
    # or set this variable to wherever your OTP instance is listening.
    otp_url: str = "http://localhost:9090"
    # Router ID used by OTP v1 REST fallback (OTP v2 ignores this)
    otp_router: str = "default"

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
