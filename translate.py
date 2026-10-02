#!/usr/bin/env python3
"""Klien LLM multi-provider untuk Bebaa Book Translator.

Sumber konfigurasi: config/providers.json (dibuat otomatis dari default bila
belum ada — perilaku lama tanpa konfigurasi tetap jalan).

Tipe provider yang didukung:
  - "openai" : endpoint OpenAI-compatible (/v1/chat/completions) — router
               bawaan, LiteLLM, OpenRouter, vLLM, Ollama, dsb.
  - "gemini" : Google Gemini API (generativelanguage.googleapis.com/v1beta).

API key provider default boleh dibiarkan kosong → dibaca dari env
HERMES_CUSTOM_192_168_1_123_3005_API_KEY atau file ~/.hermes/.env.

Fitur:
- Fallback otomatis antar model & antar provider (402/429/401 → kandidat berikutnya)
- Anti-truncation: finish_reason=length → max_tokens digandakan lalu diulang,
  teks yang terpotong TIDAK pernah dikembalikan (melindungi cache terjemahan)
- Vision: model vision tiap provider diprioritaskan untuk request bergambar
- Semaphore global: API_CONCURRENCY / WORKERS env (default 4)
"""
import urllib.request, urllib.error, json, os, pathlib, time, threading, re

BASE_DIR = pathlib.Path(__file__).parent
CONFIG_PATH = pathlib.Path(os.getenv(
    "PROVIDERS_CONFIG", str(BASE_DIR / "config" / "providers.json")))
HERMES_ENV = pathlib.Path(os.path.expanduser("~/.hermes/.env"))

# ---- Model chain default (identik dengan perilaku lama — router bawaan) ----
DEFAULT_CHAIN = [
    "Atruzz/nex-agi/nex-n2.5-mini:free",                       # primary (gratis)
    "cf/@cf/deepseek-ai/deepseek-r1-distill-qwen-32b",         # fallback 1
    "cf/@cf/qwen/qwen2.5-coder-32b-instruct",                  # fallback 2
    "Atruzz/tencent/hy-mt2-30b-a3b",                           # fallback 3
]
DEFAULT_VISION_MODEL = "Hermesbebaa"
DEFAULT_BASE_URL = "http://192.168.1.123:3005/v1"

_DEFAULT_PROVIDERS = [{
    "name": "Router Bawaan (192.168.1.123:3005)",
    "type": "openai",
    "base_url": DEFAULT_BASE_URL,
    "api_key": "",                 # kosong → env / ~/.hermes/.env
    "models": DEFAULT_CHAIN,
    "vision_model": DEFAULT_VISION_MODEL,
    "enabled": True,
}]

MAX_TOKENS_BASE = int(os.getenv("MAX_TOKENS", "8192"))
MAX_TOKENS_CAP = 32768
_API_CONC = max(1, int(os.getenv("API_CONCURRENCY", os.getenv("WORKERS", "4"))))
_API_SEM = threading.BoundedSemaphore(_API_CONC)
_MODEL_HEALTH = {}          # "provider:model" → "ok" | "dead"
_cfg_lock = threading.Lock()
_cfg_cache = {"mtime": None, "data": None}


# ---------------------------------------------------------------- config
def _write_durable(path, text):
    """Tulis file secara aman: tmp → flush+fsync → os.replace (atomik),
    lalu chmod 600 (file berisi API key)."""
    tmp = path.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_config(force=False):
    """Baca config/providers.json dengan pemulihan berlapis:
    - file belum ada        → tulis default (sekali)
    - file korup            → coba backup providers.json.bak
    - backup gagal juga     → pakai default DI MEMORI tanpa menimpa file korup
      (agar masih bisa diselamatkan manual)
    """
    with _cfg_lock:
        try:
            mtime = CONFIG_PATH.stat().st_mtime
        except OSError:
            mtime = None
        if (not force and _cfg_cache["data"] is not None
                and _cfg_cache["mtime"] == mtime):
            return _cfg_cache["data"]

        data = None
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text())
            except Exception:
                bak = CONFIG_PATH.with_suffix('.json.bak')
                if bak.exists():
                    try:
                        data = json.loads(bak.read_text())
                        print(f"[config] providers.json korup → dipulihkan dari "
                              f"{bak.name}", flush=True)
                    except Exception:
                        data = None
                if data is None:
                    print(f"[config] PERINGATAN: {CONFIG_PATH.name} tidak valid — "
                          f"memakai default di memori; file asli TIDAK ditimpa",
                          flush=True)
                    _cfg_cache.update(mtime=mtime,
                                      data={"providers": _DEFAULT_PROVIDERS})
                    return _cfg_cache["data"]

        if not data or not isinstance(data.get("providers"), list) \
                or not data["providers"]:
            data = {"providers": _DEFAULT_PROVIDERS}
            if not CONFIG_PATH.exists():   # hanya tulis kalau memang belum ada
                try:
                    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
                    _write_durable(CONFIG_PATH, json.dumps(data, indent=2))
                except Exception:
                    pass
        _cfg_cache.update(mtime=mtime, data=data)
        return data


def save_config(data):
    """Simpan providers.json dengan jaminan:
    validasi → backup versi lama (providers.json.bak) → tulis atomik + fsync.
    Dipakai halaman Settings."""
    if not isinstance(data.get("providers"), list) or not data["providers"]:
        raise ValueError("providers minimal 1 dan berupa list")
    clean = []
    for p in data["providers"]:
        if not isinstance(p, dict):
            raise ValueError("format provider tidak valid")
        if not (p.get("name") or "").strip():
            raise ValueError("setiap provider butuh nama")
        if not (p.get("base_url") or "").strip():
            raise ValueError(f"provider '{p.get('name')}' butuh base_url")
        q = dict(p)
        q["name"] = p["name"].strip()
        q["base_url"] = p["base_url"].strip().rstrip('/')
        q["type"] = "gemini" if p.get("type") == "gemini" else "openai"
        q["models"] = [m.strip() for m in (p.get("models") or []) if (m or "").strip()]
        q["enabled"] = bool(p.get("enabled", True))
        clean.append(q)
    with _cfg_lock:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if CONFIG_PATH.exists():   # backup versi lama sebelum menimpa
            try:
                bak = CONFIG_PATH.with_suffix('.json.bak')
                bak.write_text(CONFIG_PATH.read_text())
                try:
                    os.chmod(bak, 0o600)
                except OSError:
                    pass
            except OSError:
                pass
        out = {k: v for k, v in data.items() if k != 'providers'}
        # kunci non-provider yang tidak dikirim (mis. "notify") dipertahankan
        # dari file lama — menyimpan provider tidak boleh menghapus notify
        try:
            old = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
            if isinstance(old, dict):
                for k, v in old.items():
                    if k != 'providers' and k not in out:
                        out[k] = v
        except Exception:
            pass
        out['providers'] = clean
        _write_durable(CONFIG_PATH, json.dumps(out, indent=2))
        _cfg_cache.update(mtime=CONFIG_PATH.stat().st_mtime, data=out)


def get_providers():
    """Provider aktif (enabled=True) sesuai urutan di config."""
    return [p for p in load_config().get("providers", []) if p.get("enabled", True)]


def _hermes_key():
    """API key router bawaan dari ~/.hermes/.env (kompatibel perilaku lama)."""
    try:
        for line in HERMES_ENV.read_text().splitlines():
            if line.startswith("HERMES_CUSTOM_192_168_1_123_3005_API_KEY="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def resolve_key(p):
    """Key efektif sebuah provider: dari config → env → file hermes."""
    key = (p.get("api_key") or "").strip()
    if key:
        return key
    base = (p.get("base_url") or "").rstrip("/")
    if base.startswith(DEFAULT_BASE_URL.rstrip("/")[:21]):  # host router bawaan
        return os.getenv("HERMES_CUSTOM_192_168_1_123_3005_API_KEY") or _hermes_key()
    return os.getenv("GENERIC_API_KEY", "")


# ---------------------------------------------------------------- HTTP
def _http_json(url, payload, headers, timeout=180):
    """POST JSON → body mentah (string)."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


def parse_any(body):
    """Parse body response OpenAI-compatible → dict (atau None).
    Menangani: JSON murni, SSE streaming (data: ...), JSON + trailing data."""
    body = (body or "").strip()
    # 1. SSE streaming
    if body.startswith("data:") or "\ndata:" in body:
        out, finish = [], None
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]"):
                try:
                    chunk = json.loads(line[5:].strip())
                    delta = chunk["choices"][0].get("delta", {})
                    out.append(delta.get("content") or "")
                    fr = chunk["choices"][0].get("finish_reason")
                    if fr:
                        finish = fr
                except Exception:
                    pass
        text = "".join(out).strip()
        if text:
            return {"choices": [{"message": {"content": text},
                                 "finish_reason": finish}]}
        return None
    # 2. JSON murni
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        # 3. JSON + trailing data
        if "Extra Data" in str(e):
            try:
                obj, _ = json.JSONDecoder().raw_decode(body.strip())
                return obj
            except json.JSONDecodeError:
                pass
        # 4. Cari blok JSON berisi "choices"
        m = re.search(r'\{[\s\S]*"choices"[\s\S]*\}', body)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return None


# ---------------------------------------------------------------- openai
def _call_openai(p, model, prompt, image_data_uri, max_tokens):
    """Satu panggilan chat/completions. Return (text|None, finish_reason)."""
    if image_data_uri:
        content = [{"type": "text", "text": prompt},
                   {"type": "image_url", "image_url": {"url": image_data_uri}}]
    else:
        content = prompt
    body_text = _http_json(
        p["base_url"].rstrip("/") + "/chat/completions",
        {"model": model,
         "messages": [{"role": "user", "content": content}],
         "temperature": 0.2,
         "max_tokens": max_tokens,
         "stream": False},
        {"Authorization": f"Bearer {resolve_key(p)}"})
    data = parse_any(body_text)
    if data is None:
        raise RuntimeError(f"respons tidak bisa diparse: {body_text[:200]}")
    if "error" in data:
        raise RuntimeError(f"API error: {data['error']}")
    ch = (data.get("choices") or [{}])[0]
    msg = (ch.get("message") or {}).get("content")
    if isinstance(msg, list):  # sebagian router balas list of parts
        msg = " ".join(str(x.get("text", x)) if isinstance(x, dict) else str(x)
                       for x in msg)
    text = (msg or "").strip()
    return (text or None), ch.get("finish_reason")


# ---------------------------------------------------------------- gemini
_GEMINI_SAFETY = [
    {"category": c, "threshold": "BLOCK_NONE"}
    for c in ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
              "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")
]


def _call_gemini(p, model, prompt, image_data_uri, max_tokens):
    """Satu panggilan generateContent Gemini. Return (text|None, finishReason)."""
    parts = [{"text": prompt}]
    if image_data_uri:
        m = re.match(r"data:([^;]+);base64,(.*)", image_data_uri, re.DOTALL)
        if not m:
            raise RuntimeError("image_data_uri tidak valid untuk gemini")
        parts.append({"inline_data": {"mime_type": m.group(1),
                                      "data": m.group(2)}})
    body_text = _http_json(
        p["base_url"].rstrip("/") + f"/models/{model}:generateContent",
        {"contents": [{"parts": parts}],
         "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens},
         "safetySettings": _GEMINI_SAFETY},
        {"x-goog-api-key": resolve_key(p)})
    try:
        data = json.loads(body_text)
    except Exception:
        raise RuntimeError(f"respons gemini tidak valid: {body_text[:200]}")
    if data.get("error"):
        raise RuntimeError(f"API error: {data['error']}")
    cands = data.get("candidates") or []
    if not cands:
        fb = json.dumps(data.get("promptFeedback", data))[:200]
        raise RuntimeError(f"gemini tanpa candidates: {fb}")
    c = cands[0]
    text = "".join(part.get("text", "")
                   for part in (c.get("content") or {}).get("parts", [])).strip()
    return (text or None), c.get("finishReason")


# ---------------------------------------------------------------- kandidat
def _candidates(image_data_uri):
    """Daftar (provider, model) urutan fallback.
    Request bergambar → model vision tiap provider dicoba lebih dulu."""
    seen, regular, vision_first = set(), [], []
    for p in get_providers():
        tag = (p.get("type"), p.get("base_url"))
        vm = (p.get("vision_model") or "").strip()
        if vm and image_data_uri and (tag + (vm,)) not in seen:
            seen.add(tag + (vm,))
            vision_first.append((p, vm))
        for m in p.get("models") or []:
            m = (m or "").strip()
            if m and (tag + (m,)) not in seen:
                seen.add(tag + (m,))
                regular.append((p, m))
    return (vision_first + regular) if image_data_uri else regular


# ---------------------------------------------------------------- chat
def chat(prompt, retries=5, image_data_uri=None, skip_models=None, max_tokens=None):
    """Chat completion → teks. Menelusuri semua provider/model sampai sukses.

    image_data_uri : data URI gambar (mulai 'data:image/...;base64,') → vision.
    skip_models    : set nama model yang di-skip.
    max_tokens     : override awal (default MAX_TOKENS env / 8192).
    Raises RuntimeError jika semua kandidat gagal.
    TIDAK pernah mengembalikan teks terpotong (finish_reason=length).
    """
    skip = set(skip_models or [])
    last_err = "unknown error"
    mt = int(max_tokens or MAX_TOKENS_BASE)

    candidates = _candidates(image_data_uri)
    if not candidates:
        raise RuntimeError(
            "tidak ada provider/model aktif — buka halaman Settings "
            "dan periksa konfigurasi provider (nama, base_url, model)")

    for p, model in candidates:
        name = f"{p.get('name', '?')}:{model}"
        if name in skip or model in skip:
            continue
        if _MODEL_HEALTH.get(name) == "dead":
            _MODEL_HEALTH[name] = "retry"   # reset setelah 1 request berikutnya
            continue
        attempt_mt = mt
        for attempt in range(retries):
            try:
                with _API_SEM:
                    fn = _call_gemini if p.get("type") == "gemini" else _call_openai
                    result, finish = fn(p, model, prompt, image_data_uri, attempt_mt)
                if result:
                    if finish in ("length", "MAX_TOKENS") and attempt_mt < MAX_TOKENS_CAP:
                        attempt_mt = min(attempt_mt * 2, MAX_TOKENS_CAP)
                        print(f"    [chat] {name} terpotong (finish={finish}) "
                              f"→ ulang max_tokens={attempt_mt}", flush=True)
                        continue
                    if finish in ("length", "MAX_TOKENS"):
                        last_err = (f"respons terpotong "
                                    f"(finish_reason={finish}, max_tokens={attempt_mt})")
                    elif finish in ("SAFETY", "RECITATION", "PROHIBITED_CONTENT",
                                    "BLOCKLIST", "SPII"):
                        last_err = f"diblokir filter keamanan model ({finish})"
                    else:
                        _MODEL_HEALTH[name] = "ok"
                        return result
                else:
                    last_err = "respons valid tapi content kosong"
            except urllib.error.HTTPError as e:
                msg = e.read().decode(errors="replace")[:200]
                last_err = f"HTTP {e.code}: {msg}"
                if e.code in (401, 403):
                    last_err = (f"HTTP {e.code}: API key ditolak oleh "
                                f"{p.get('base_url')} — periksa Settings")
                    break        # key provider ini bermasalah → provider berikutnya
                if e.code in (402, 429):
                    _MODEL_HEALTH[name] = "dead"
                    break        # quota/rate-limit → kandidat berikutnya
            except urllib.error.URLError as e:
                # timeout / DNS / koneksi ditolak (HTTPError adalah subclass-nya)
                last_err = f"koneksi gagal ke {p.get('base_url')} ({e.reason})"
            except RuntimeError as e:
                last_err = str(e)
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"

            if attempt < retries - 1:
                wait = min(2 ** (attempt + 1), 60)
                print(f"    [chat] {name} percobaan {attempt + 1} gagal "
                      f"({last_err}), ulangi {wait}s...", flush=True)
                time.sleep(wait)

    raise RuntimeError(f"LLM gagal: semua provider/model habis ({last_err})")


# ---------------------------------------------------------------- util UI
def _get_json(url, headers, timeout=10):
    """GET JSON dengan pesan error yang bisa ditindaklanjuti (dipakai Settings)."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:150]
        hint = "periksa base_url & API key di Settings"
        if e.code == 401:
            hint = "API key ditolak (401) — periksa key di Settings"
        elif e.code == 404:
            hint = "endpoint /models tidak ditemukan — periksa base_url (harus sampai /v1)"
        raise RuntimeError(f"HTTP {e.code} dari {url} — {hint}. {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"tidak bisa terhubung ke {url} ({e.reason})")
    except json.JSONDecodeError:
        raise RuntimeError(f"respons dari {url} bukan JSON valid")


def fetch_remote_models(p, timeout=10):
    """Ambil daftar model dari provider (untuk picker di halaman Settings)."""
    base = (p.get("base_url") or "").rstrip("/")
    if not base:
        raise RuntimeError("base_url provider kosong — isi dulu di Settings")
    url = base + "/models"
    if p.get("type") == "gemini":
        data = _get_json(url, {"x-goog-api-key": resolve_key(p)}, timeout)
        out = []
        for m in data.get("models", []):
            name = (m.get("name") or "").replace("models/", "")
            methods = m.get("supportedGenerationMethods") or []
            # hanya model yang bisa generateContent (bukan embedding dll.)
            if not name or (methods and "generateContent" not in methods):
                continue
            out.append(name)
        return out
    data = _get_json(url, {"Authorization": f"Bearer {resolve_key(p)}"}, timeout)
    return [m.get("id") for m in data.get("data", []) if m.get("id")]
