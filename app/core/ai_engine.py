import json
import logging
from typing import Any

import anthropic
import httpx

from app.core.config import settings
from app.prompts.builder import PromptBuilder
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class AIEngine:
    def __init__(self):
        self.provider = settings.AI_PROVIDER.lower()
        self.prompt_builder = PromptBuilder()
        self.tool_registry = ToolRegistry()
        self.anthropic_client = None

        if self.provider == "anthropic":
            self.anthropic_client = anthropic.AsyncAnthropic(
                api_key=settings.ANTHROPIC_API_KEY,
                base_url=settings.ANTHROPIC_BASE_URL,
            )

    async def process(
        self,
        user_message: str,
        history: list[dict] | None = None,
        phone_number: str = "",
    ) -> tuple[str, list[dict]]:
        history = history or []
        try:
            if self.provider == "openrouter":
                return await self._process_with_openrouter(user_message, history, phone_number)
            if self.provider == "ollama":
                return await self._process_with_ollama(user_message, history, phone_number)
            if self.provider == "anthropic":
                return await self._process_with_anthropic(user_message, history, phone_number)
            raise ValueError(f"AI_PROVIDER no soportado: {self.provider}")
        except Exception:
            logger.exception("AI processing failed; returning fallback response")
            fallback_history = self._ensure_single_system_prompt(
                self._normalize_history_for_openai(history),
                self.prompt_builder.build(phone_number=phone_number),
            )
            fallback_history.append({"role": "user", "content": user_message})
            fallback_history.append({"role": "assistant", "content": settings.AI_FALLBACK_MESSAGE})
            return settings.AI_FALLBACK_MESSAGE, fallback_history

    async def _process_with_openrouter(
        self,
        user_message: str,
        history: list[dict],
        phone_number: str,
    ) -> tuple[str, list[dict]]:
        system_prompt = self.prompt_builder.build(phone_number=phone_number)
        tools = self.tool_registry.get_openai_tools() if settings.ENABLE_TOOLS else []

        messages = self._ensure_single_system_prompt(
            self._normalize_history_for_openai(history),
            system_prompt,
        )
        messages.append({"role": "user", "content": user_message})

        final_response = ""

        for _ in range(5):
            payload: dict[str, Any] = {
                "model": settings.OPENROUTER_MODEL or settings.AI_MODEL,
                "messages": messages,
                "temperature": settings.OPENROUTER_TEMPERATURE,
                "top_p": settings.OPENROUTER_TOP_P,
                "max_tokens": settings.OPENROUTER_MAX_TOKENS,
            }
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"

            response = await self._openrouter_chat(payload)
            choices = response.get("choices") or []
            if not choices:
                raise ValueError("OpenRouter devolvió una respuesta sin choices")

            message = choices[0].get("message") or {}
            tool_calls = message.get("tool_calls") or []

            if tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": message.get("content") or "",
                        "tool_calls": tool_calls,
                    }
                )

                for call in tool_calls:
                    fn = call.get("function") or {}
                    tool_name = fn.get("name")
                    tool_args = self._normalize_tool_arguments(fn.get("arguments", {}))
                    logger.info("Executing OpenRouter tool: %s with args: %s", tool_name, tool_args)
                    result = await self.tool_registry.execute(tool_name, tool_args)
                    logger.info("Tool result: %s", result)

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "name": tool_name,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                continue

            final_response = self._extract_openrouter_content(message)
            if not final_response:
                final_response = settings.AI_FALLBACK_MESSAGE

            messages.append({"role": "assistant", "content": final_response})
            break

        return final_response or settings.AI_FALLBACK_MESSAGE, messages

    async def _process_with_ollama(
        self,
        user_message: str,
        history: list[dict],
        phone_number: str,
    ) -> tuple[str, list[dict]]:
        system_prompt = self.prompt_builder.build(phone_number=phone_number)
        tools = self.tool_registry.get_openai_tools() if settings.ENABLE_TOOLS else []

        messages = self._ensure_single_system_prompt(
            self._normalize_history_for_ollama(history),
            system_prompt,
        )
        messages.append({"role": "user", "content": user_message})

        final_response = ""

        for _ in range(5):
            payload: dict[str, Any] = {
                "model": settings.OLLAMA_MODEL or settings.AI_MODEL,
                "messages": messages,
                "stream": False,
            }
            if tools:
                payload["tools"] = tools

            response = await self._ollama_chat(payload)
            message = response.get("message", {})
            tool_calls = message.get("tool_calls") or []

            if tool_calls:
                assistant_message = {
                    "role": "assistant",
                    "content": message.get("content", ""),
                    "tool_calls": tool_calls,
                }
                messages.append(assistant_message)

                for call in tool_calls:
                    function = call.get("function", {})
                    tool_name = function.get("name")
                    raw_args = function.get("arguments", {})
                    tool_args = self._normalize_tool_arguments(raw_args)
                    logger.info("Executing Ollama tool: %s with args: %s", tool_name, tool_args)

                    result = await self.tool_registry.execute(tool_name, tool_args)
                    logger.info("Tool result: %s", result)

                    messages.append(
                        {
                            "role": "tool",
                            "tool_name": tool_name,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                continue

            final_response = (message.get("content") or "").strip()
            if not final_response:
                final_response = settings.AI_FALLBACK_MESSAGE

            messages.append({"role": "assistant", "content": final_response})
            break

        return final_response or settings.AI_FALLBACK_MESSAGE, messages

    async def _process_with_anthropic(
        self,
        user_message: str,
        history: list[dict],
        phone_number: str,
    ) -> tuple[str, list[dict]]:
        system_prompt = self.prompt_builder.build(phone_number=phone_number)
        tools = self.tool_registry.get_openai_tools() if settings.ENABLE_TOOLS else []

        messages = [m for m in list(history) if m.get("role") != "system"]
        messages.append({"role": "user", "content": user_message})

        anthropic_tools = []
        for t in tools:
            fn = t["function"]
            anthropic_tools.append(
                {
                    "name": fn["name"],
                    "description": fn["description"],
                    "input_schema": fn["parameters"],
                }
            )

        final_response = ""

        for _ in range(5):
            kwargs = {
                "model": settings.AI_MODEL,
                "max_tokens": 600,
                "system": system_prompt,
                "messages": messages,
            }
            if anthropic_tools:
                kwargs["tools"] = anthropic_tools

            response = await self.anthropic_client.messages.create(**kwargs)

            if response.stop_reason == "tool_use":
                assistant_content = response.content
                messages.append({"role": "assistant", "content": assistant_content})

                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        tool_name = block.name
                        tool_args = block.input
                        logger.info("Executing tool: %s with args: %s", tool_name, tool_args)

                        result = await self.tool_registry.execute(tool_name, tool_args)
                        logger.info("Tool result: %s", result)

                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": json.dumps(result, ensure_ascii=False),
                            }
                        )

                messages.append({"role": "user", "content": tool_results})
            else:
                for block in response.content:
                    if hasattr(block, "text"):
                        final_response = block.text
                        break

                if not final_response:
                    final_response = settings.AI_FALLBACK_MESSAGE

                messages.append({"role": "assistant", "content": final_response})
                break

        return final_response or settings.AI_FALLBACK_MESSAGE, messages

    async def _openrouter_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{settings.OPENROUTER_BASE_URL.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        }
        if settings.OPENROUTER_HTTP_REFERER:
            headers["HTTP-Referer"] = settings.OPENROUTER_HTTP_REFERER
        if settings.OPENROUTER_TITLE:
            headers["X-OpenRouter-Title"] = settings.OPENROUTER_TITLE

        async with httpx.AsyncClient(timeout=settings.OPENROUTER_TIMEOUT_SECONDS) as client:
            response = await client.post(url, headers=headers, json=payload)
            if response.status_code >= 400:
                logger.error("OpenRouter status=%s body=%s", response.status_code, response.text)
            response.raise_for_status()
            return response.json()

    async def _ollama_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/chat"
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(url, json=payload)
            if response.status_code >= 400:
                logger.error("Ollama status=%s body=%s", response.status_code, response.text)
            response.raise_for_status()
            return response.json()

    @staticmethod
    def _extract_openrouter_content(message: dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text")
                    if text:
                        parts.append(str(text))
            return "\n".join(p.strip() for p in parts if p and str(p).strip()).strip()
        return ""

    @staticmethod
    def _normalize_tool_arguments(raw_args: Any) -> dict[str, Any]:
        if isinstance(raw_args, dict):
            return raw_args
        if isinstance(raw_args, str):
            try:
                return json.loads(raw_args)
            except json.JSONDecodeError:
                return {}
        return {}

    @staticmethod
    def _normalize_history_for_ollama(history: list[dict]) -> list[dict]:
        normalized: list[dict] = []
        for msg in history:
            role = msg.get("role")
            if role not in {"system", "user", "assistant", "tool"}:
                continue

            normalized_msg = {"role": role}
            if "content" in msg:
                normalized_msg["content"] = msg["content"]
            if role == "assistant" and msg.get("tool_calls"):
                normalized_msg["tool_calls"] = msg["tool_calls"]
            if role == "tool" and msg.get("tool_name"):
                normalized_msg["tool_name"] = msg["tool_name"]
            normalized.append(normalized_msg)
        return normalized

    @staticmethod
    def _normalize_history_for_openai(history: list[dict]) -> list[dict]:
        normalized: list[dict] = []
        for msg in history:
            role = msg.get("role")
            if role not in {"system", "user", "assistant", "tool"}:
                continue

            clean: dict[str, Any] = {"role": role}
            if "content" in msg:
                clean["content"] = msg["content"]
            if role == "assistant" and msg.get("tool_calls"):
                clean["tool_calls"] = msg["tool_calls"]
            if role == "tool":
                if msg.get("tool_call_id"):
                    clean["tool_call_id"] = msg["tool_call_id"]
                if msg.get("name"):
                    clean["name"] = msg["name"]
            normalized.append(clean)
        return normalized

    @staticmethod
    def _ensure_single_system_prompt(history: list[dict], system_prompt: str) -> list[dict]:
        filtered = [msg for msg in history if msg.get("role") != "system"]
        return [{"role": "system", "content": system_prompt}, *filtered]


# Module-level singleton reference — set by main.py for admin hot-reload
ai_engine_instance = None
