"""LLM access, selected by the LLM_PROVIDER flag: xAI Grok or OpenAI. Both speak the OpenAI Chat Completions API.

Provider settings (key, base URL, primary and fast model) come from `Settings.llm`; nothing else in the app depends on
which provider is active.

Reliability: bounded retries with backoff on transient errors, then a fallback from the primary (reasoning) model to the
fast model. If both fail, LLMUnavailable is raised and the orchestrator serves a deterministic degraded reply.
The interface is deliberately small (`chat`), so tests can use a scripted FakeLLM and providers can be swapped.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.config import get_settings
from app.core.observability import current_trace, log


class LLMUnavailable(Exception):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    raw_arguments: str = ""


@dataclass
class LLMReply:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    usage: Any = None

    def as_message(self) -> dict:
        msg: dict = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [{"id": t.id, "type": "function",
                                  "function": {"name": t.name, "arguments": t.raw_arguments or json.dumps(t.arguments)}}
                                 for t in self.tool_calls]
        return msg


Tier = Literal["primary", "fast"]


class LLM(Protocol):
    def chat(self, messages: list[dict], tools: list[dict] | None = None, tier: Tier = "primary",
             json_mode: bool = False) -> LLMReply: ...


class ChatLLM:
    """OpenAI-compatible chat client for the configured provider (grok | openai)."""

    def __init__(self) -> None:
        from openai import OpenAI

        self.s = get_settings()
        self.cfg = self.s.llm
        self.client = OpenAI(api_key=self.cfg.api_key or "missing", base_url=self.cfg.base_url,
                             timeout=self.s.llm_timeout_s, max_retries=0)

    def _models(self, tier: Tier) -> list[str]:
        p, f = self.cfg.primary_model, self.cfg.fast_model
        return [p, f] if tier == "primary" else [f, p]

    def chat(self, messages, tools=None, tier: Tier = "primary", json_mode: bool = False) -> LLMReply:
        import openai

        if not self.cfg.configured:
            raise LLMUnavailable(f"No API key configured for LLM provider '{self.cfg.provider}'")
        last_err: Exception | None = None
        for i, model in enumerate(self._models(tier)):
            for attempt in range(self.s.llm_max_retries + 1):
                try:
                    kwargs: dict = {"model": model, "messages": messages}
                    if tools:
                        kwargs["tools"] = tools
                        kwargs["tool_choice"] = "auto"
                    elif model == self.cfg.fast_model and self.cfg.fast_reasoning_effort:
                        # OpenAI: reasoning_effort can't be combined with function tools on Chat Completions
                        kwargs["reasoning_effort"] = self.cfg.fast_reasoning_effort
                    if json_mode:
                        kwargs["response_format"] = {"type": "json_object"}
                    resp = self.client.chat.completions.create(**kwargs)
                    msg = resp.choices[0].message
                    calls = []
                    for tc in msg.tool_calls or []:
                        try:
                            args = json.loads(tc.function.arguments or "{}")
                        except json.JSONDecodeError:
                            args = {"__invalid_json__": tc.function.arguments}
                        calls.append(ToolCall(tc.id, tc.function.name, args, tc.function.arguments or "{}"))
                    t = current_trace()
                    if t:
                        t.add_usage(f"{self.cfg.provider}:{model}", resp.usage)
                        if i > 0:
                            t.fallback_used = True
                    return LLMReply(content=msg.content, tool_calls=calls, model=model, usage=resp.usage)
                except (openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError, openai.InternalServerError) as e:
                    last_err = e
                    log.warning("llm_retry", extra={"data": {"provider": self.cfg.provider, "model": model,
                                                             "attempt": attempt, "error": type(e).__name__}})
                    time.sleep(min(2 ** attempt, 4))
                except openai.APIStatusError as e:   # 4xx (bad model name, auth): no retry, try the next model
                    last_err = e
                    log.error("llm_status_error", extra={"data": {"provider": self.cfg.provider, "model": model,
                                                                  "status": e.status_code, "error": _err_message(e)}})
                    break
        raise LLMUnavailable(f"All models failed: {type(last_err).__name__ if last_err else 'unknown'}")


def _err_message(e: Exception) -> str:
    """Provider error text (e.g. 'Incorrect API key provided'), trimmed; it never contains the key itself."""
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        msg = body.get("error", body)
        msg = msg.get("message", msg) if isinstance(msg, dict) else msg
        return str(msg)[:200]
    return str(e)[:200]


GrokLLM = ChatLLM  # backwards-compatible name
