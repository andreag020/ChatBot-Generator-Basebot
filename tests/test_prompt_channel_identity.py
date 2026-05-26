from app.prompts.builder import PromptBuilder


def build_prompt(config: dict) -> str:
    builder = PromptBuilder.__new__(PromptBuilder)
    builder._config = config
    return builder._assemble(None)


def test_prompt_omits_channel_by_default():
    prompt = build_prompt({"business": {"name": "ACME", "bot_name": "Ava"}})

    assert "You are Ava, the virtual assistant for ACME." in prompt
    assert "currently assisting through" not in prompt
    assert "WhatsApp commercial assistant" not in prompt


def test_prompt_uses_known_channel_label_when_present():
    prompt = build_prompt(
        {
            "business": {"name": "ACME", "bot_name": "Ava"},
            "runtime": {"channel": "facebook"},
        }
    )

    assert "You are currently assisting through Facebook Messenger." in prompt


def test_prompt_humanizes_future_channel_names():
    prompt = build_prompt(
        {
            "business": {"name": "ACME", "bot_name": "Ava"},
            "runtime": {"channel": "telegram_business"},
        }
    )

    assert "You are currently assisting through Telegram Business." in prompt


def test_prompt_prefers_explicit_channel_label():
    prompt = build_prompt(
        {
            "business": {"name": "ACME", "bot_name": "Ava"},
            "runtime": {"channel": "custom_partner", "channel_label": "Partner Portal"},
        }
    )

    assert "You are currently assisting through Partner Portal." in prompt
