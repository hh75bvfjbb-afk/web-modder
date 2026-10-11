"""Model adapters. The conversation is stored in one provider-neutral shape so a session can move between keys
and providers mid-task without losing anything:

    {"role": "user" | "assistant", "content": [block, ...]}
    text        {"type": "text", "text": ...}
    image       {"type": "image", "media_type": ..., "data": <base64>}                  (user turns)
    tool_use    {"type": "tool_use", "id": ..., "name": ..., "input": {...}}             (assistant turns)
    tool_result {"type": "tool_result", "tool_use_id": ..., "content": str, "images": [...], "is_error": bool}
    thinking    the raw Anthropic block plus "_provider": "anthropic"; replayed only to Anthropic, unchanged

`complete()` returns {"blocks", "stop" (end | tool_use | max_tokens | refusal), "usage", "model"} or raises
ProviderError whose `kind` tells the key pool what to do with the key.
"""
from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid

from webmodder.keys import PROVIDERS

MAX_TOKENS_ANTHROPIC = 64000
MAX_TOKENS_OPENAI = 16000
ANTHROPIC_BETAS = ["thinking-binding-controls-2026-08-01", "thinking-display-updates-2026-08-18",
                   "server-side-fallback-2026-07-01", "context-management-2025-06-27"]
_COMPAT_HINTS = ("anthropic-beta", "extra inputs", "not permitted", "not supported", "unexpected value", "fallbacks",
                 "thinking", "context_management", "output_config", "effort", "cache_control", "display",
                 "block_binding", "unsupported parameter", "unrecognized", "max_tokens", "max_completion_tokens",
                 "tool_choice", "unknown parameter")
_CONTEXT_HINTS = ("prompt is too long", "context length", "context_length", "maximum context", "too many tokens",
                  "reduce the length", "input is too long", "request too large", "token limit")
_QUOTA_HINTS = ("credit balance", "insufficient_quota", "insufficient balance", "quota", "billing", "payment",
                "exceeded your current", "out of credits", "insufficient credits", "balance")


class ProviderError(Exception):
    def __init__(self, kind: str, message: str, retry_after: float | None = None, status: int = 0):
        super().__init__(message)
        self.kind, self.message, self.retry_after, self.status = kind, message, retry_after, status


def new_id() -> str:
    return "tu_" + uuid.uuid4().hex[:20]


def _classify(status: int, text: str) -> str:
    t = text.lower()
    if any(h in t for h in _CONTEXT_HINTS) or status == 413:
        return "context"
    if status == 402 or (status in (400, 403, 429) and any(h in t for h in _QUOTA_HINTS)):
        return "quota"
    if status in (401, 403):
        return "auth"
    if status == 404:
        return "model"
    if status in (408, 409, 429):
        return "rate_limit"
    if status >= 500 or status == 0:
        return "overloaded" if status else "network"
    if status == 400 and ("does not support tools" in t or "tool use is not supported" in t or "function calling" in t):
        return "model"
    return "bad_request"


def _retry_after(headers) -> float | None:
    try:
        v = headers.get("retry-after") if headers else None
        return float(v) if v else None
    except (TypeError, ValueError):
        return None


# ====================================================================== Anthropic (official SDK)

def _anthropic_messages(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        blocks = []
        for b in m["content"]:
            t = b.get("type")
            if t == "text":
                if b.get("text", "").strip():
                    blocks.append({"type": "text", "text": b["text"]})
            elif t == "image":
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": b["media_type"], "data": b["data"]}})
            elif t == "tool_use":
                blocks.append({"type": "tool_use", "id": b["id"], "name": b["name"], "input": b.get("input") or {}})
            elif t == "tool_result":
                content = [{"type": "text", "text": b.get("content") or "(пусто)"}]
                for im in b.get("images") or []:
                    content.append({"type": "image", "source": {"type": "base64", "media_type": im["media_type"], "data": im["data"]}})
                blocks.append({"type": "tool_result", "tool_use_id": b["tool_use_id"], "content": content,
                               "is_error": bool(b.get("is_error"))})
            elif t in ("thinking", "redacted_thinking") and b.get("_provider") == "anthropic":
                blocks.append({k: v for k, v in b.items() if not k.startswith("_")})
        if not any(x["type"] not in ("thinking", "redacted_thinking") for x in blocks):
            blocks.append({"type": "text", "text": "…"})
        out.append({"role": m["role"], "content": blocks})
    return out


def _anthropic_call(key: dict, system: str, messages: list[dict], tools: list[dict], effort: str, on_progress) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=key["secret"], base_url=key.get("base") or None, max_retries=0, timeout=900)
    level = key.get("compat", 2)
    while True:
        kw = dict(model=key["model"], max_tokens=MAX_TOKENS_ANTHROPIC, system=system,
                  messages=_anthropic_messages(messages),
                  tools=[{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools])
        extra: dict = {}
        if level >= 2:
            kw["betas"] = ANTHROPIC_BETAS
            extra = {"thinking": {"type": "adaptive", "display": "updates",
                                  "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
                     "output_config": {"effort": effort}, "fallbacks": "default",
                     "context_management": {"edits": [{"type": "clear_tool_uses_20250919"}]},
                     "cache_control": {"type": "ephemeral"}}
        elif level == 1:
            extra = {"thinking": {"type": "adaptive"}}
        try:
            api = client.beta.messages if level >= 2 else client.messages
            with api.stream(**kw, extra_body=extra or None) as stream:
                msg = stream.get_final_message()
            break
        except anthropic.APIStatusError as e:
            text = str(e.message if hasattr(e, "message") else e)
            try:
                text += " " + json.dumps(e.body)[:600]
            except (TypeError, ValueError):
                pass
            kind = _classify(e.status_code, text)
            if kind == "bad_request" and level > 0 and any(h in text.lower() for h in _COMPAT_HINTS):
                level -= 1  # this model or account rejects a newer feature: retry with a plainer request
                key["compat"] = level
                continue
            raise ProviderError(kind, f"HTTP {e.status_code}: {text[:500]}", _retry_after(e.response.headers), e.status_code)
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            raise ProviderError("network", str(e))

    blocks = []
    for b in msg.content:
        d = b.model_dump() if hasattr(b, "model_dump") else dict(b)
        t = d.get("type")
        if t == "text":
            blocks.append({"type": "text", "text": d.get("text", "")})
        elif t == "tool_use":
            blocks.append({"type": "tool_use", "id": d["id"], "name": d["name"], "input": d.get("input") or {}})
        elif t in ("thinking", "redacted_thinking"):
            d = {k: v for k, v in d.items() if v is not None}
            d["_provider"] = "anthropic"
            blocks.append(d)
            if t == "thinking" and d.get("thinking") and on_progress:
                on_progress(d["thinking"])
    u = msg.usage
    usage = {"input": (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0)
             + (getattr(u, "cache_creation_input_tokens", 0) or 0), "output": u.output_tokens or 0}
    stop = {"tool_use": "tool_use", "max_tokens": "max_tokens", "refusal": "refusal"}.get(msg.stop_reason or "", "end")
    if stop != "tool_use" and any(b["type"] == "tool_use" for b in blocks):
        stop = "tool_use"
    return {"blocks": blocks, "stop": stop, "usage": usage, "model": getattr(msg, "model", key["model"])}


# ====================================================================== OpenAI-compatible (raw HTTP)

def _short_id(i: str, provider: str) -> str:
    if provider == "mistral":  # Mistral wants exactly 9 alphanumerics
        return hashlib.md5(i.encode()).hexdigest()[:9]
    return i


def _openai_messages(system: str, messages: list[dict], provider: str) -> list[dict]:
    out = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "assistant":
            text = "\n".join(b["text"] for b in m["content"] if b.get("type") == "text" and b.get("text"))
            calls = [{"id": _short_id(b["id"], provider), "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}}
                     for b in m["content"] if b.get("type") == "tool_use"]
            msg = {"role": "assistant", "content": text or ("" if not calls else None)}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        images, parts = [], []
        for b in m["content"]:
            t = b.get("type")
            if t == "tool_result":
                out.append({"role": "tool", "tool_call_id": _short_id(b["tool_use_id"], provider),
                            "content": ("ОШИБКА: " if b.get("is_error") else "") + (b.get("content") or "(пусто)")})
                images += b.get("images") or []
            elif t == "text" and b.get("text"):
                parts.append({"type": "text", "text": b["text"]})
            elif t == "image":
                images.append(b)
        if images:
            parts = parts or [{"type": "text", "text": "Изображения к предыдущему шагу:"}]
            for im in images:
                parts.append({"type": "image_url", "image_url": {"url": f"data:{im['media_type']};base64,{im['data']}"}})
        if parts:
            if all(p["type"] == "text" for p in parts):
                out.append({"role": "user", "content": "\n\n".join(p["text"] for p in parts)})
            else:
                out.append({"role": "user", "content": parts})
    return out


def _openai_call(key: dict, system: str, messages: list[dict], tools: list[dict], effort: str) -> dict:
    provider = key["provider"]
    base = (key.get("base") or PROVIDERS[provider]["base"]).rstrip("/")
    level = key.get("compat", 2)
    id_map = {_short_id(b["id"], provider): b["id"] for m in messages for b in m["content"] if b.get("type") == "tool_use"}
    while True:
        body = {"model": key["model"], "messages": _openai_messages(system, messages, provider),
                "tools": [{"type": "function", "function": t} for t in tools]}
        if level >= 1:
            body["tool_choice"] = "auto"
        if level >= 2:
            body["max_completion_tokens" if provider == "openai" else "max_tokens"] = MAX_TOKENS_OPENAI
        headers = {"Content-Type": "application/json", "User-Agent": "web-modder"}
        if key.get("secret"):
            headers["Authorization"] = "Bearer " + key["secret"]
        if provider == "freellmapi":
            # tell the router these are tool-using coding turns, so it leans to stronger models
            headers["X-FreeLLM-Task-Type"] = "code"
        if provider == "openrouter":
            headers.update({"HTTP-Referer": "https://github.com/rehan-remade/universal-modder", "X-Title": "web-modder"})
        req = urllib.request.Request(base + "/chat/completions", data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                js = json.loads(r.read().decode("utf-8", "replace"))
                routed = urllib.parse.unquote(r.headers.get("X-Routed-Via") or "")
            break
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")[:1500]
            kind = _classify(e.code, text)
            if kind == "bad_request" and level > 0 and any(h in text.lower() for h in _COMPAT_HINTS):
                level -= 1
                key["compat"] = level
                continue
            raise ProviderError(kind, f"HTTP {e.code}: {text[:500]}", _retry_after(e.headers), e.code)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise ProviderError("network", str(e))
        except ValueError as e:
            raise ProviderError("overloaded", f"неверный ответ сервера: {e}")
    if js.get("error"):
        err = js["error"]
        text = json.dumps(err)[:500]
        raise ProviderError(_classify(int(err.get("code") or 500) if str(err.get("code", "")).isdigit() else 500, text), text)
    choices = js.get("choices") or []
    if not choices:
        raise ProviderError("overloaded", "пустой ответ: " + json.dumps(js)[:300])
    msg = choices[0].get("message") or {}
    blocks = []
    if msg.get("content"):
        content = msg["content"]
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        blocks.append({"type": "text", "text": content})
    for c in msg.get("tool_calls") or []:
        fn = c.get("function") or {}
        raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(args, dict):
                args = {"_raw": raw}
        except ValueError:
            args = {"_raw": raw}
        cid = id_map.get(c.get("id"), None) or new_id()
        blocks.append({"type": "tool_use", "id": cid, "name": fn.get("name", ""), "input": args})
    fr = choices[0].get("finish_reason") or ""
    stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else ("max_tokens" if fr == "length" else "end")
    if fr == "content_filter" and not blocks:
        stop = "refusal"
    u = js.get("usage") or {}
    return {"blocks": blocks, "stop": stop, "model": routed or js.get("model", key["model"]),
            "usage": {"input": u.get("prompt_tokens", 0) or 0, "output": u.get("completion_tokens", 0) or 0}}


def complete(key: dict, system: str, messages: list[dict], tools: list[dict], effort: str = "high", on_progress=None) -> dict:
    kind = PROVIDERS[key["provider"]]["kind"]
    if kind == "anthropic":
        return _anthropic_call(key, system, messages, tools, effort, on_progress)
    if kind == "openai":
        return _openai_call(key, system, messages, tools, effort)
    raise ProviderError("model", f"ключ {key['provider']} не подходит для агента")
