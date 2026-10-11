"""The key pool: paste any API key, the app works out whose it is, and the agent never stops on one key.

A FreeLLMAPI router (github.com/tashfeenahmed/freellmapi) is a key like any other: its unified
`freellmapi-...` key fronts dozens of free-tier provider keys, and the router itself rotates them.
Every key can carry a monthly token limit that refills by itself on the 1st of the month (UTC).

Every key is tried in priority order. A key that hits a rate limit cools down and comes back by itself; a key
that is out of credit or revoked is parked until you top it up and press "check". When no key is usable the
agent waits (it does not fail) and carries on the moment a key is added or a cooldown ends.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from webmodder.config import data_dir, read_json, write_json

# kind: "anthropic" = Messages API, "openai" = any OpenAI-compatible /chat/completions, "fal" = asset generation
PROVIDERS: dict[str, dict] = {
    "anthropic": {"name": "Anthropic (Claude)", "kind": "anthropic", "base": "https://api.anthropic.com",
                  "default": "claude-opus-5-5",
                  "prefer": ["claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5-5", "claude-opus-5", "claude-sonnet-5",
                             "claude-opus-4", "claude-sonnet-4", "claude-haiku-5-5"]},
    "openai": {"name": "OpenAI", "kind": "openai", "base": "https://api.openai.com/v1", "default": "gpt-5",
               "prefer": ["gpt-6", "gpt-5.5", "gpt-5", "gpt-4.1", "gpt-4o"]},
    "openrouter": {"name": "OpenRouter", "kind": "openai", "base": "https://openrouter.ai/api/v1",
                   "default": "anthropic/claude-opus-5.5",
                   "prefer": ["anthropic/claude-opus-5.5", "anthropic/claude-sonnet-5.5", "anthropic/claude-opus",
                              "anthropic/claude-sonnet", "openai/gpt-5", "google/gemini"]},
    "gemini": {"name": "Google Gemini", "kind": "openai", "base": "https://generativelanguage.googleapis.com/v1beta/openai",
               "default": "gemini-2.5-pro", "prefer": ["gemini-3", "gemini-2.5-pro", "gemini-2.5-flash"]},
    "deepseek": {"name": "DeepSeek", "kind": "openai", "base": "https://api.deepseek.com/v1", "default": "deepseek-chat",
                 "prefer": ["deepseek-chat", "deepseek-reasoner"]},
    "groq": {"name": "Groq", "kind": "openai", "base": "https://api.groq.com/openai/v1",
             "default": "llama-3.3-70b-versatile", "prefer": ["moonshotai/kimi", "openai/gpt-oss-120b", "llama-3.3-70b"]},
    "mistral": {"name": "Mistral", "kind": "openai", "base": "https://api.mistral.ai/v1", "default": "mistral-large-latest",
                "prefer": ["mistral-large-latest", "devstral", "codestral-latest"]},
    "xai": {"name": "xAI (Grok)", "kind": "openai", "base": "https://api.x.ai/v1", "default": "grok-4",
            "prefer": ["grok-5", "grok-4", "grok-3"]},
    "together": {"name": "Together AI", "kind": "openai", "base": "https://api.together.xyz/v1",
                 "default": "Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8", "prefer": ["Qwen/Qwen3-Coder", "deepseek-ai/DeepSeek-V3"]},
    "fireworks": {"name": "Fireworks", "kind": "openai", "base": "https://api.fireworks.ai/inference/v1",
                  "default": "accounts/fireworks/models/qwen3-coder-480b-a35b-instruct", "prefer": ["qwen3-coder", "deepseek-v3"]},
    "freellmapi": {"name": "FreeLLMAPI (роутер бесплатных ключей)", "kind": "openai", "base": "http://localhost:31415/v1",
                   "default": "auto:smart", "prefer": [], "fixed_default": True},
    "custom": {"name": "Свой сервер (OpenAI-совместимый)", "kind": "openai", "base": "", "default": "", "prefer": []},
    "fal": {"name": "fal.ai (ассеты)", "kind": "fal", "base": "https://fal.run", "default": "", "prefer": []},
}

def month_key(t: float | None = None) -> str:
    return time.strftime("%Y-%m", time.gmtime(t if t is not None else time.time()))


def next_month_start(t: float | None = None) -> float:
    """00:00 UTC on the 1st of next month: when monthly limits refill (FreeLLMAPI resets its caps then too)."""
    g = time.gmtime(t if t is not None else time.time())
    y, m = (g.tm_year + 1, 1) if g.tm_mon == 12 else (g.tm_year, g.tm_mon + 1)
    import calendar

    return float(calendar.timegm((y, m, 1, 0, 0, 0)))


def discover_freellmapi(bases=("http://localhost:31415", "http://localhost:3001")) -> str:
    """Where a FreeLLMAPI router is running: the desktop app listens on 31415, Docker on 3001.
    Its /v1/providers answers 401 without the unified key. Returns the origin, or ""."""
    for base in bases:
        status, _, err = _http_json(base.rstrip("/") + "/v1/providers", {}, timeout=2)
        if status == 200 or (status == 401 and "api key" in err.lower()):
            return base
    return ""


COOLDOWN_MIN, COOLDOWN_MAX = 20, 900
QUOTA_COOLDOWN = 1800


def detect(secret: str) -> list[str]:
    """Candidate providers for a key, most likely first. Several candidates are resolved by probing."""
    s = secret.strip()
    if s.startswith("freellmapi-"):
        return ["freellmapi"]
    if re.fullmatch(r"[0-9a-f-]{20,}:[0-9a-zA-Z]{16,}", s):
        return ["fal"]
    for prefix, prov in (("sk-ant-", "anthropic"), ("sk-or-", "openrouter"), ("AIza", "gemini"), ("gsk_", "groq"),
                         ("xai-", "xai"), ("fw_", "fireworks"), ("sk-proj-", "openai"), ("sk-svcacct-", "openai")):
        if s.startswith(prefix):
            return [prov]
    if s.startswith("sk-"):
        return ["openai", "deepseek"]
    return ["mistral", "together", "openai", "deepseek"]


def mask(secret: str) -> str:
    s = secret.strip()
    return s[:7] + "…" + s[-4:] if len(s) > 14 else "…" + s[-3:]


def _http_json(url: str, headers: dict, timeout: float = 20) -> tuple[int, dict | None, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "web-modder", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, json.loads(body), ""
    except urllib.error.HTTPError as e:
        return e.code, None, e.read().decode("utf-8", "replace")[:400]
    except (urllib.error.URLError, OSError, ValueError) as e:
        return 0, None, str(e)


def list_models(provider: str, secret: str, base: str | None = None) -> tuple[int, list[str], str]:
    """(http status, model ids, error). Status 200 means the key works."""
    p = PROVIDERS[provider]
    base = (base or p["base"]).rstrip("/")
    if p["kind"] == "fal":
        return 200, [], ""
    if p["kind"] == "anthropic":
        status, js, err = _http_json(base + "/v1/models?limit=100", {"x-api-key": secret, "anthropic-version": "2023-06-01"})
    else:
        status, js, err = _http_json(base + "/models", {"Authorization": "Bearer " + secret} if secret else {})
    ids = []
    if js:
        for m in js.get("data") or js.get("models") or []:
            mid = m.get("id") or m.get("name") if isinstance(m, dict) else None
            if mid:
                ids.append(mid.removeprefix("models/"))
    return status, ids, err


def pick_model(provider: str, models: list[str]) -> str:
    p = PROVIDERS[provider]
    if p.get("fixed_default"):  # a router: let it choose ("auto:smart") instead of pinning one upstream model
        return p["default"]
    for pref in p["prefer"]:
        for m in models:
            if m.startswith(pref):
                return m
    if p["default"] and (not models or p["default"] in models):
        return p["default"]
    return models[0] if models else p["default"]


class KeyPool:
    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / "keys.json"
        self.cond = threading.Condition()
        self.keys: list[dict] = read_json(self.path, {"keys": []}).get("keys", [])
        for k in self.keys:  # cooldowns from a previous run are stale
            if k.get("status") == "cooldown" and k.get("cooldown_until", 0) < time.time():
                k["status"] = "active"

    # ------------------------------------------------------------------ persistence / views
    def save(self):
        write_json(self.path, {"keys": self.keys}, private=True)

    def public(self) -> list[dict]:
        out = []
        now = time.time()
        for k in self.keys:
            v = {x: k.get(x) for x in ("id", "provider", "label", "model", "base", "status", "priority", "budget",
                                       "last_error", "usage", "added", "models")}
            v["masked"] = mask(k["secret"])
            v["provider_name"] = PROVIDERS.get(k["provider"], {}).get("name", k["provider"])
            v["kind"] = PROVIDERS.get(k["provider"], {}).get("kind")
            self._roll(k)
            if k.get("status") == "cooldown":
                v["cooldown_left"] = max(0, int(k.get("cooldown_until", 0) - now))
            v["month_tokens"] = k["usage"].get("month_tokens", 0)
            v["month_requests"] = k["usage"].get("month_requests", 0)
            v["refill_at"] = next_month_start()
            v["budget_hit"] = bool(k.get("budget_hit"))
            out.append(v)
        return out

    def summary(self) -> dict:
        llm = [k for k in self.keys if PROVIDERS.get(k["provider"], {}).get("kind") != "fal"]
        return {"llm_total": len(llm), "llm_ready": sum(1 for k in llm if self._usable(k)),
                "fal_total": sum(1 for k in self.keys if k["provider"] == "fal"),
                "fal_ready": sum(1 for k in self.keys if k["provider"] == "fal" and self._usable(k))}

    def get(self, key_id: str) -> dict | None:
        return next((k for k in self.keys if k["id"] == key_id), None)

    # ------------------------------------------------------------------ adding ("top up")
    def add(self, secret: str, provider: str | None = None, base: str | None = None, model: str | None = None,
            label: str | None = None, probe: bool = True) -> dict:
        secret = secret.strip()
        if not secret and not base:
            raise ValueError("пустой ключ")
        if not base and (secret.startswith("freellmapi-") or provider == "freellmapi"):
            base = discover_freellmapi() or "http://localhost:31415"
        if base and (secret.startswith("freellmapi-") or provider == "freellmapi"):
            provider, base = "freellmapi", base.rstrip("/") + ("" if base.rstrip("/").endswith("/v1") else "/v1")
        elif base:
            provider = provider if provider and provider != "auto" else "custom"
        for k in self.keys:
            if k["secret"] == secret and secret and (not base or k.get("base") == base):
                # the same key pasted again = "top up": wake it up and re-check
                self.recheck(k["id"]) if probe else self._revive(k)
                return k
        cands = [provider] if provider and provider != "auto" else detect(secret)
        chosen, models, last_err = cands[0], [], ""
        if probe and PROVIDERS[cands[0]]["kind"] != "fal":
            for c in cands:
                status, ids, err = list_models(c, secret, base)
                if status == 200:
                    chosen, models, last_err = c, ids, ""
                    break
                last_err = f"{PROVIDERS[c]['name']}: HTTP {status} {err[:160]}"
            else:
                if len(cands) > 1:
                    raise ValueError("не удалось определить провайдера ключа: " + last_err)
        rec = {"id": uuid.uuid4().hex[:10], "provider": chosen, "secret": secret, "base": base or "",
               "model": model or pick_model(chosen, models), "label": label or f"{PROVIDERS[chosen]['name'].split(' (')[0]} {mask(secret)}",
               "status": "active" if not last_err else "dead", "last_error": last_err,
               # a router already spreads load over many free keys: try it before single keys
               "priority": (min([x.get("priority", 0) for x in self.keys] or [0]) - 1) if chosen == "freellmapi" else len(self.keys),
               "budget": None, "usage": {"input": 0, "output": 0, "requests": 0, "errors": 0},
               "models": models[:300], "added": time.time(), "fails": 0}
        with self.cond:
            self.keys.append(rec)
            self.save()
            self.cond.notify_all()
        return rec

    def update(self, key_id: str, **fields) -> dict:
        k = self.get(key_id)
        if not k:
            raise KeyError(key_id)
        with self.cond:
            for f in ("label", "model", "priority", "base"):
                if f in fields and fields[f] is not None:
                    k[f] = fields[f]
            if "budget" in fields:
                b = fields["budget"]
                k["budget"] = int(b) if b not in (None, "", 0, "0") else None
                if k.get("budget_hit") and (not k["budget"] or k["usage"].get("month_tokens", 0) < k["budget"]):
                    self._revive(k)  # raising the monthly limit tops the key up right away
            if "enabled" in fields:
                if fields["enabled"]:
                    self._revive(k)
                else:
                    k["status"] = "disabled"
            self.save()
            self.cond.notify_all()
        return k

    def remove(self, key_id: str):
        with self.cond:
            self.keys = [k for k in self.keys if k["id"] != key_id]
            self.save()
            self.cond.notify_all()

    def recheck(self, key_id: str) -> dict:
        k = self.get(key_id)
        if not k:
            raise KeyError(key_id)
        status, ids, err = list_models(k["provider"], k["secret"], k.get("base") or None)
        with self.cond:
            if status == 200:
                if ids:
                    k["models"] = ids[:300]
                self._revive(k)
            else:
                k["status"], k["last_error"] = "dead", f"проверка: HTTP {status} {err[:200]}"
            self.save()
            self.cond.notify_all()
        return k

    def _revive(self, k: dict):
        k["status"], k["last_error"], k["fails"], k["cooldown_until"] = "active", "", 0, 0
        k.pop("compat", None)  # a re-check also retries the newer API features
        k.pop("budget_hit", None)  # if the month's limit is still spent, _usable parks it again

    def _roll(self, k: dict):
        """New month: the monthly counter starts from zero and a key parked by its limit comes back."""
        u = k["usage"]
        m = month_key()
        if u.get("month") != m:
            u["month"], u["month_tokens"], u["month_requests"] = m, 0, 0
            u.pop("budget_base", None)
            if k.pop("budget_hit", None) and k["status"] == "cooldown":
                k["status"], k["cooldown_until"], k["last_error"] = "active", 0, ""

    # ------------------------------------------------------------------ choosing a key
    def _usable(self, k: dict) -> bool:
        self._roll(k)
        if k["status"] == "cooldown" and k.get("cooldown_until", 0) <= time.time():
            k["status"] = "active"
            k.pop("budget_hit", None)
        if k["status"] != "active":
            return False
        b = k.get("budget")
        if b and k["usage"].get("month_tokens", 0) >= b:
            refill = next_month_start()
            k["status"], k["cooldown_until"], k["budget_hit"] = "cooldown", refill, True
            k["last_error"] = ("месячный лимит токенов исчерпан — пополнится сам " + time.strftime("%d.%m.%Y", time.gmtime(refill))
                               + " (или увеличьте лимит)")
            self.save()
            return False
        return True

    def acquire(self, kind: str = "llm", prefer: str | None = None, exclude: set | None = None) -> dict | None:
        """The key to use now. Sticky: the session's current key wins while it is healthy (keeps caches warm)."""
        exclude = exclude or set()
        with self.cond:
            cands = [k for k in self.keys if k["id"] not in exclude and self._usable(k)
                     and ((PROVIDERS[k["provider"]]["kind"] == "fal") == (kind == "fal"))]
            if not cands:
                return None
            for k in cands:
                if k["id"] == prefer:
                    return k
            return sorted(cands, key=lambda k: (k.get("priority", 0), k.get("fails", 0)))[0]

    def next_ready_in(self, kind: str = "llm") -> float | None:
        """Seconds until a cooling key comes back; None when nothing is cooling (all parked or none)."""
        now = time.time()
        ts = [k.get("cooldown_until", 0) - now for k in self.keys if k["status"] == "cooldown"
              and ((PROVIDERS[k["provider"]]["kind"] == "fal") == (kind == "fal"))]
        return max(0.0, min(ts)) if ts else None

    def wait(self, timeout: float):
        """Block until keys change (added, revived) or the timeout passes."""
        with self.cond:
            self.cond.wait(timeout)

    # ------------------------------------------------------------------ outcomes
    def success(self, k: dict, usage: dict):
        with self.cond:
            self._roll(k)
            u = k["usage"]
            spent = int(usage.get("input", 0)) + int(usage.get("output", 0))
            u["input"] += int(usage.get("input", 0))
            u["output"] += int(usage.get("output", 0))
            u["requests"] += 1
            u["month_tokens"] = u.get("month_tokens", 0) + spent
            u["month_requests"] = u.get("month_requests", 0) + 1
            k["fails"] = 0
            self.save()

    def failure(self, k: dict, kind: str, message: str = "", retry_after: float | None = None):
        """kind: rate_limit | overloaded | network | quota | auth | model | other."""
        with self.cond:
            k["usage"]["errors"] = k["usage"].get("errors", 0) + 1
            k["fails"] = k.get("fails", 0) + 1
            k["last_error"] = f"{kind}: {message[:240]}"
            if kind in ("auth", "model"):
                k["status"] = "dead"
            elif kind == "quota":
                # out of credit or a daily quota: park it for a while and try again by itself, so a top-up on the
                # provider's site is picked up without touching the app
                # FreeLLMAPI's monthly cap answers with Retry-After = seconds to the next month: honour it
                k["status"], k["cooldown_until"] = "cooldown", time.time() + (retry_after or QUOTA_COOLDOWN)
            elif k["provider"] == "freellmapi" and kind in ("overloaded", "network"):
                # a router's 5xx is one upstream failing; the next request is routed elsewhere, so only a short pause
                k["status"], k["cooldown_until"] = "cooldown", time.time() + (retry_after or min(3 * k["fails"], 20))
            else:
                base = retry_after if retry_after else COOLDOWN_MIN * (2 ** min(k["fails"] - 1, 6))
                if kind == "network":
                    base = retry_after or min(10 * k["fails"], 120)
                k["status"], k["cooldown_until"] = "cooldown", time.time() + min(max(base, 3), COOLDOWN_MAX)
            self.save()
            self.cond.notify_all()
