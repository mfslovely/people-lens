"""LLM provider flag: Grok vs OpenAI selection, request shape, and fallback behaviour (no network calls)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent import llm as llm_mod
from app.config import Settings


def settings(**kw) -> Settings:
    base = dict(xai_api_key="xai-test", openai_api_key="sk-test", _env_file=None)
    return Settings(**{**base, **kw})


def test_provider_selection():
    g = settings(llm_provider="grok").llm
    o = settings(llm_provider="openai").llm
    assert (g.provider, g.base_url, g.api_key, g.primary_model) == ("grok", "https://api.x.ai/v1", "xai-test", "grok-4")
    assert (o.provider, o.base_url, o.api_key, o.primary_model, o.fast_model) == \
        ("openai", "https://api.openai.com/v1", "sk-test", "gpt-5.5", "gpt-5.4-mini")
    assert o.fast_reasoning_effort == "low" and g.fast_reasoning_effort is None


def test_invalid_provider_rejected():
    with pytest.raises(Exception):
        settings(llm_provider="unsupported_provider")


class _Recorder:
    def __init__(self, fail_models=()):
        self.calls, self.fail_models = [], set(fail_models)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        import httpx
        import openai

        self.calls.append(kw)
        if kw["model"] in self.fail_models:
            raise openai.BadRequestError("bad", response=httpx.Response(400, request=httpx.Request("POST", "http://x")),
                                         body={"error": {"message": "bad model"}})
        msg = SimpleNamespace(content="ok", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))


def client_for(monkeypatch, **kw) -> tuple[llm_mod.ChatLLM, _Recorder]:
    monkeypatch.setattr(llm_mod, "get_settings", lambda: settings(**kw))
    c = llm_mod.ChatLLM()
    rec = _Recorder(kw.pop("_fail", ()))
    c.client = rec
    return c, rec


def test_openai_reasoning_effort_only_without_tools(monkeypatch):
    c, rec = client_for(monkeypatch, llm_provider="openai")
    c.chat([{"role": "user", "content": "x"}], tier="fast", json_mode=True)
    c.chat([{"role": "user", "content": "x"}], tools=[{"type": "function", "function": {"name": "t", "parameters": {}}}], tier="fast")
    c.chat([{"role": "user", "content": "x"}], tier="primary")
    assert rec.calls[0]["model"] == "gpt-5.4-mini" and rec.calls[0]["reasoning_effort"] == "low"
    assert "reasoning_effort" not in rec.calls[1] and rec.calls[1]["tools"]
    assert rec.calls[2]["model"] == "gpt-5.5" and "reasoning_effort" not in rec.calls[2]


def test_grok_requests_never_send_reasoning_effort(monkeypatch):
    c, rec = client_for(monkeypatch, llm_provider="grok")
    c.chat([{"role": "user", "content": "x"}], tier="fast", json_mode=True)
    assert rec.calls[0]["model"] == "grok-3-mini" and "reasoning_effort" not in rec.calls[0]


def test_falls_back_to_other_model_on_4xx(monkeypatch):
    monkeypatch.setattr(llm_mod, "get_settings", lambda: settings(llm_provider="openai"))
    c = llm_mod.ChatLLM()
    c.client = rec = _Recorder(fail_models={"gpt-5.5"})
    r = c.chat([{"role": "user", "content": "x"}])
    assert [k["model"] for k in rec.calls] == ["gpt-5.5", "gpt-5.4-mini"] and r.model == "gpt-5.4-mini"


def test_missing_key_for_selected_provider_is_unavailable(monkeypatch):
    monkeypatch.setattr(llm_mod, "get_settings", lambda: settings(llm_provider="openai", openai_api_key=""))
    with pytest.raises(llm_mod.LLMUnavailable, match="openai"):
        llm_mod.ChatLLM().chat([{"role": "user", "content": "x"}])
