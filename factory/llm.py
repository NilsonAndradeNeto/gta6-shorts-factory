"""Cliente LLM com múltiplos provedores gratuitos (endpoints compatíveis com OpenAI).

Ordem padrão: GitHub Models (gratuito com rate limit, autenticado pelo GITHUB_TOKEN
do Actions) -> Gemini (camada gratuita do AI Studio) -> Groq (camada gratuita).
Somente provedores com chave configurada são tentados.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import requests

from factory.config import settings
from factory.log import get_logger

log = get_logger("llm")


class LLMError(RuntimeError):
    pass


def _extract_json(text: str) -> Any:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        s, e = text.find(open_c), text.rfind(close_c)
        if s != -1 and e > s:
            try:
                return json.loads(text[s:e + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError("Resposta do LLM não contém JSON válido")


class LLM:
    def __init__(self, db=None):
        self.db = db
        self.providers = []
        for p in settings().get("llm.providers", []):
            key = os.environ.get(p["api_key_env"], "").strip()
            if key:
                models = p["model"] if isinstance(p["model"], list) else [p["model"]]
                self.providers.append({**p, "key": key, "candidates": models, "model": None})
        self.max_chars = int(settings().get("llm.max_input_chars", 22000))
        self._disabled: set[str] = set()

    def _resolve_model(self, p: dict[str, Any]) -> str:
        """Escolhe o primeiro modelo configurado que a conta realmente oferece (nomes mudam com o tempo)."""
        if p["model"]:
            return p["model"]
        try:
            r = requests.get(f"{p['base_url'].rstrip('/')}/models", timeout=30,
                             headers={"Authorization": f"Bearer {p['key']}"})
            ids = {m["id"].split("/", 1)[-1] if m["id"].startswith("models/") else m["id"] for m in r.json().get("data", [])}
            p["model"] = next((m for m in p["candidates"] if m in ids), p["candidates"][0])
        except (requests.RequestException, ValueError, KeyError, TypeError):
            p["model"] = p["candidates"][0]
        log.info("LLM %s usando modelo %s", p["name"], p["model"])
        return p["model"]

    @property
    def available(self) -> bool:
        return bool(self.providers)

    def _log_usage(self, prov: str, model: str, purpose: str, ok: bool, cin: int, cout: int, err: str = "") -> None:
        if self.db is None:
            return
        from factory.database.db import now_iso
        try:
            self.db.x("INSERT INTO llm_usage(ts,provider,model,purpose,ok,chars_in,chars_out,error) VALUES (?,?,?,?,?,?,?,?)",
                      now_iso(), prov, model, purpose, int(ok), cin, cout, err[:500])
        except Exception:  # uso é só telemetria
            pass

    def chat(self, system: str, user: str, *, purpose: str, temperature: float | None = None,
             json_mode: bool = True, max_tokens: int = 3000, images: list[bytes] | None = None) -> str:
        if not self.providers:
            raise LLMError("Nenhum provedor LLM configurado (defina GEMINI_API_KEY e/ou GROQ_API_KEY)")
        if len(system) + len(user) > self.max_chars:
            user = user[: max(2000, self.max_chars - len(system))]
            log.warning("Prompt truncado para %d caracteres (%s)", self.max_chars, purpose)
        temp = settings().get("llm.temperature", 0.8) if temperature is None else temperature
        errors = []
        for p in self.providers:
            if p["name"] in self._disabled:
                continue
            user_content: Any = user
            if images:
                import base64
                user_content = [{"type": "text", "text": user}] + [
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(b).decode(),
                                                        "detail": "low"}} for b in images]
            body: dict[str, Any] = {
                "model": self._resolve_model(p),
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_content}],
                "temperature": temp,
                "max_tokens": max_tokens,
            }
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            for attempt in range(3):
                try:
                    r = requests.post(f"{p['base_url'].rstrip('/')}/chat/completions", json=body, timeout=180,
                                      headers={"Authorization": f"Bearer {p['key']}", "Content-Type": "application/json"})
                    if r.status_code == 429 or r.status_code >= 500:
                        wait = min(60, int(r.headers.get("retry-after", "0") or 0) or 2 ** (attempt + 2))
                        if r.status_code == 429 and attempt == 2:
                            self._disabled.add(p["name"])  # cota diária esgotada: pula este provedor
                        log.warning("%s HTTP %s; aguardando %ss", p["name"], r.status_code, wait)
                        time.sleep(wait)
                        continue
                    if r.status_code >= 400:
                        raise LLMError(f"{p['name']} HTTP {r.status_code}: {r.text[:300]}")
                    try:
                        content = r.json()["choices"][0]["message"]["content"] or ""
                    except ValueError:
                        raise LLMError(f"{p['name']}: resposta não é JSON da API ({r.headers.get('content-type')}: "
                                       f"{r.text[:80]!r}); endpoint pode ter mudado") from None
                    self._log_usage(p["name"], p["model"], purpose, True, len(system) + len(user), len(content))
                    return content
                except (requests.RequestException, KeyError, LLMError) as e:
                    errors.append(f"{p['name']}: {e}")
                    self._log_usage(p["name"], p["model"], purpose, False, len(system) + len(user), 0, str(e))
                    if isinstance(e, LLMError):
                        break
                    time.sleep(2 ** attempt)
        raise LLMError("Todos os provedores falharam: " + " | ".join(errors[-4:]))

    def json(self, system: str, user: str, *, purpose: str, **kw: Any) -> Any:
        last: Exception | None = None
        for _ in range(2):
            raw = self.chat(system, user, purpose=purpose, **kw)
            try:
                return _extract_json(raw)
            except LLMError as e:
                last = e
                user = user + "\n\nIMPORTANTE: responda SOMENTE com um objeto JSON válido."
        raise LLMError(str(last))
