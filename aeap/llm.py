"""Externer LLM-Provider (OpenAI-kompatibel: Kimi/Moonshot, OpenAI, DeepSeek, OpenRouter …).

Der Schlüssel wird nur zur Laufzeit gelesen, nie gespeichert oder ausgegeben. Quellen in dieser Reihenfolge:
  1. Umgebungsvariablen (AEAP_LLM_API_KEY, MOONSHOT_API_KEY, KIMI_API_KEY, OPENAI_API_KEY, DEEPSEEK_API_KEY, OPENROUTER_API_KEY)
  2. .env im Projektordner (gleiche Variablennamen)
  3. Verweis auf eine fremde Datei ohne Kopie:  AEAP_LLM_API_KEY_FILE=<Pfad zur .env>  +  AEAP_LLM_API_KEY_VAR=<Variablenname>
Optionale Überschreibungen: AEAP_LLM_BASE_URL, AEAP_LLM_MODEL, AEAP_LLM_PROVIDER.
"""

import json
import os
import threading
import urllib.error
import urllib.request

PROVIDERS = {
    "moonshot": {"label": "Kimi (Moonshot AI)", "base": "https://api.moonshot.ai/v1", "alt_base": "https://api.moonshot.cn/v1",
                 "prefer": ["kimi-k2", "kimi-latest", "moonshot-v1-32k", "moonshot-v1-8k"]},
    "openai": {"label": "OpenAI", "base": "https://api.openai.com/v1", "prefer": ["gpt-4.1", "gpt-4o", "gpt-4o-mini"]},
    "deepseek": {"label": "DeepSeek", "base": "https://api.deepseek.com/v1", "prefer": ["deepseek-chat"]},
    "openrouter": {"label": "OpenRouter", "base": "https://openrouter.ai/api/v1", "prefer": ["moonshotai/kimi-k2"]},
    "custom": {"label": "OpenAI-kompatibel", "base": "", "prefer": []},
}
KEY_VARS = [("AEAP_LLM_API_KEY", None), ("MOONSHOT_API_KEY", "moonshot"), ("KIMI_API_KEY", "moonshot"),
            ("OPENAI_API_KEY", "openai"), ("DEEPSEEK_API_KEY", "deepseek"), ("OPENROUTER_API_KEY", "openrouter")]


class LLMError(Exception):
    pass


def parse_env_file(path):
    out = {}
    try:
        with open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip().removeprefix("export ").strip()
                v = v.strip().strip('"').strip("'")
                out[k] = v
    except OSError:
        pass
    return out


def _guess_provider(var, key):
    for name, prov in KEY_VARS:
        if name == var and prov:
            return prov
    if key.startswith("sk-or-"):
        return "openrouter"
    return "moonshot"


class LLMClient:
    def __init__(self, root):
        self.root = root
        self._lock = threading.Lock()
        self._model_cache = None
        self._base_override = None

    # ---- Konfiguration ------------------------------------------------------
    def _settings(self):
        env = parse_env_file(os.path.join(self.root, ".env"))
        # Leere Umgebungsvariablen dürfen Werte aus der .env nicht überschreiben
        merged = {**env, **{k: v for k, v in os.environ.items() if v and (k.startswith("AEAP_LLM_") or k in dict(KEY_VARS))}}
        key, var, source = None, None, None
        for name, _ in KEY_VARS:
            if os.environ.get(name):
                key, var, source = os.environ[name], name, f"Umgebungsvariable {name}"
                break
        if not key:
            for name, _ in KEY_VARS:
                if env.get(name):
                    key, var, source = env[name], name, f".env im Projektordner ({name})"
                    break
        if not key and merged.get("AEAP_LLM_API_KEY_FILE"):
            ref_file = os.path.expandvars(os.path.expanduser(merged["AEAP_LLM_API_KEY_FILE"]))
            ref_var = merged.get("AEAP_LLM_API_KEY_VAR", "")
            ref = parse_env_file(ref_file)
            candidates = [ref_var] if ref_var else [n for n, _ in KEY_VARS]
            for name in candidates:
                if ref.get(name):
                    key, var, source = ref[name], name, f"Verweis auf {ref_file} ({name})"
                    break
            if not key:
                source = f"Verweis auf {ref_file}: Variable {ref_var or '(Standardnamen)'} nicht gefunden"
        provider = merged.get("AEAP_LLM_PROVIDER") or (_guess_provider(var, key) if key else None)
        base = merged.get("AEAP_LLM_BASE_URL") or self._base_override or (PROVIDERS.get(provider, {}).get("base") if provider else None)
        return {"key": key, "var": var, "source": source, "provider": provider, "base": (base or "").rstrip("/"),
                "model": merged.get("AEAP_LLM_MODEL"), "baseExplicit": bool(merged.get("AEAP_LLM_BASE_URL"))}

    def status(self):
        s = self._settings()
        return {
            "configured": bool(s["key"] and s["base"]),
            "provider": PROVIDERS.get(s["provider"], {}).get("label") if s["provider"] else None,
            "baseUrl": s["base"] or None,
            "model": s["model"] or self._model_cache,
            "keySource": s["source"],
        }

    # ---- HTTP ---------------------------------------------------------------
    def _request(self, s, method, path, body=None, timeout=90):
        req = urllib.request.Request(s["base"] + path, method=method,
                                     data=json.dumps(body).encode("utf-8") if body is not None else None,
                                     headers={"Authorization": f"Bearer {s['key']}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
            except Exception:  # noqa: BLE001
                pass
            raise LLMError(f"HTTP {e.code} vom Provider{': ' + detail if detail else ''}") from None
        except urllib.error.URLError as e:
            raise LLMError(f"Provider nicht erreichbar: {e.reason}") from None
        except TimeoutError:
            raise LLMError("Zeitüberschreitung beim Provider") from None

    def _pick_model(self, s):
        if s["model"]:
            return s["model"]
        if self._model_cache:
            return self._model_cache
        data = self._request(s, "GET", "/models", timeout=20)
        ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
        if not ids:
            raise LLMError("Provider liefert keine Modelle. Bitte AEAP_LLM_MODEL setzen")
        prefer = PROVIDERS.get(s["provider"], {}).get("prefer", [])
        chosen = next((i for p in prefer for i in ids if i.startswith(p)), ids[0])
        self._model_cache = chosen
        return chosen

    def chat(self, messages, temperature=0.3, max_tokens=1800):
        with self._lock:
            s = self._settings()
            if not s["key"] or not s["base"]:
                raise LLMError("Kein API-Schlüssel konfiguriert")
            try:
                model = self._pick_model(s)
            except LLMError as e:
                # Moonshot: Schlüssel der chinesischen Plattform funktionieren nur auf api.moonshot.cn
                alt = PROVIDERS.get(s["provider"], {}).get("alt_base")
                if "401" in str(e) and alt and not s["baseExplicit"] and s["base"] != alt:
                    self._base_override = alt
                    s = self._settings()
                    model = self._pick_model(s)
                else:
                    raise
        data = self._request(s, "POST", "/chat/completions",
                             {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens})
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMError("Unerwartete Antwort des Providers") from None
        return {"text": text, "model": data.get("model", model), "usage": data.get("usage") or {}}
