from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # WhatsApp Cloud API
    WHATSAPP_TOKEN: str = "your_whatsapp_token_here"
    WHATSAPP_PHONE_ID: str = "your_phone_id_here"
    WHATSAPP_VERIFY_TOKEN: str = "your_verify_token_here"

    # Admin — número que recibe alertas (sin + ni espacios)
    ADMIN_WHATSAPP_NUMBER: str = ""

    # AI provider
    AI_PROVIDER: str = "openrouter"  # openrouter | ollama | anthropic
    AI_MODEL: str = "openrouter/free"

    # OpenRouter
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    OPENROUTER_MODEL: str = "openrouter/free"
    OPENROUTER_HTTP_REFERER: str = ""
    OPENROUTER_TITLE: str = "AI Chatbot"
    OPENROUTER_TIMEOUT_SECONDS: float = 90.0
    OPENROUTER_MAX_TOKENS: int = 500
    OPENROUTER_TEMPERATURE: float = 0.3
    OPENROUTER_TOP_P: float = 0.9

    # Safety / behavior controls
    ENABLE_TOOLS: bool = False
    HISTORY_MAX_MESSAGES: int = 8
    AI_FALLBACK_MESSAGE: str = (
        "Thank you for your message. Currently, I am unable to process a response. "
        "An advisor will review your request and contact you shortly."
    )

    # Anthropic
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_BASE_URL: str = "https://api.anthropic.com"

    # Ollama
    OLLAMA_BASE_URL: str = "http://ollama:11434"
    OLLAMA_MODEL: str = "qwen2:0.5b"
    OLLAMA_KEEP_ALIVE: str = "10m"
    OLLAMA_THINK: bool = False
    OLLAMA_NUM_CTX: int = 8192

    ADMIN_TOKEN: str = ""  # Set to a strong secret (min 32 chars)

    # Bot identity
    BOT_NAME: str = "Virtual Assistant"
    CONFIG_PATH: str = "config/bot_config.yaml"
    TOOLS_PATH: str = "config/tools_config.yaml"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings():
    return Settings()


settings = get_settings()
