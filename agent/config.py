from __future__ import annotations

import logging
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    """
    Configuración global del Agente de Eleventa.
    Carga valores desde variables de entorno o archivo .env.
    """

    # Conexión al Middleware
    MIDDLEWARE_URL: str = "http://localhost:8000"
    AGENT_API_KEY: str = "test-agent-key-12345"

    # Conexión a la Base de Datos de Eleventa (Firebird)
    ELEVENTA_DB_PATH: str = "C:/Program Files/Eleventa/pdv.fdb"
    ELEVENTA_DB_USER: str = "SYSDBA"
    ELEVENTA_DB_PASS: str = "masterkey"

    # Configuración de los ciclos (workers)
    SYNC_DOWN_INTERVAL_SECONDS: int = 10
    SYNC_UP_INTERVAL_SECONDS: int = 10

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


# Instancia global de la configuración
settings = AgentSettings()

# Configuración básica de logging para el agente
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent / "agent.log", encoding="utf-8"),
    ],
)
