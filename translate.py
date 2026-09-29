import urllib.request, json, os, pathlib, time, threading, re

BASE = "http://192.168.1.123:3005/v1"
KEY = [l.split("=",1)[1].strip() for l in pathlib.Path(os.path.expanduser("~/.hermes/.env")).read_text().splitlines() if l.startswith("HERMES_CUSTOM_192_168_1_123_3005_API_KEY=")][0]
# Model chain: urutan fallback otomatis
# Setiap model dicoba sampai dapat respons valid (bukan 402/429/timeout)
MODEL_CHAIN = [
    os.getenv("LLM_MODEL", "Atruzz/nex-agi/nex-n2.5-mini:free"),  # primary (gratis)
    "cf/@cf/deepseek-ai/deepseek-r1-distill-qwen-32b",           # fallback 1 (cf worker)
    "cf/@cf/qwen/qwen2.5-coder-32b-instruct",                      # fallback 2
    "Atruzz/tencent/hy-mt2-30b-a3b",                               # fallback 3 (terjan gratis)
    # Vision model khusus - hanya untuk OCR vision:
    # "Hermesbebaa", "gemini/gemini-3.7-flash"
]

# Status model: "ok" / "dead" (402/429 sementara)
_MODEL_HEALTH = {}

# Semaphore global: maksimal 4 request API bersamaan (semua thread berbagi)
_API_SEM = threading.BoundedSemaphore(4)


def _parse_response(body):
    """Parse API response body → konten teks.
    Handles: streaming (data: lines), single JSON, JSON with trailing data."""
    body = body.strip()

    # 1. Streaming SSE
    if body.startswith("data:"):
        out = []
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]"):
                try:
                    chunk = json.loads(line[5:].strip())
                    delta = chunk["choices"][0].get("delta", {})
                    out.append(delta.get("content") or "")
                except Exception:
                    pass
        res = "".join(out).strip()
        return res if res else None

    # 2. Coba parse langsung
    try:
        data = json.loads(body)
        if "error" in data:
            raise RuntimeError(f"API error: {data['error']}")
        if "choices" in data:
            content = data["choices"][0]["message"]["content"]
            return content.strip() if content else None
    except json.JSONDecodeError as e:
        # 3. Streaming SSE (bukan data: prefix, tapi multi-JSON atau trailing)
        #    Strip whitespace + trailing non-JSON (mis: "data: [DONE]")
        if "Extra Data" in str(e):
            decoder = json.JSONDecoder()
            try:
                obj, _ = decoder.raw_decode(body.strip())
                if "choices" in obj:
                    content = obj["choices"][0]["message"]["content"]
                    return content.strip() if content else None
                if "error" in obj:
                    raise RuntimeError(f"API error: {obj['error']}")
            except json.JSONDecodeError:
                pass
        # 4. Coba find-first-JSON: cari { pertama dan } terakhir
        m = re.search(r'\{[\s\S]*"choices"[\s\S]*\}', body)
        if m:
            try:
                obj = json.loads(m.group())
                if "choices" in obj:
                    content = obj["choices"][0]["message"]["content"]
                    return content.strip() if content else None
            except json.JSONDecodeError:
                pass
        raise
    return None


def chat(prompt, retries=5, image_data_uri=None, skip_models=None):
    """Chat completion → teks via MODEL_CHAIN (auto-fallback).
    image_data_uri: kirim sebagai content block multimodal.
    skip_models: set model names to skip (eg: vision models for text-only).
    Raises RuntimeError jika semua model gagal."""
    skip = skip_models or set()
    last_err = "unknown error"

    for model in MODEL_CHAIN:
        if model in skip:
            continue
        # Skip jika tandanya model quota-dead baru saja
        if _MODEL_HEALTH.get(model) == "dead":
            # reset status setelah 20 request berikutnya
            _MODEL_HEALTH[model] = "retry"
            continue

        for attempt in range(retries):
            try:
                with _API_SEM:
                    if image_data_uri:
                        content = [
                            {"type": "text", "text": prompt},
                            {"type": "image_url",
                             "image_url": {"url": image_data_uri}},
                        ]
                    else:
                        content = prompt
                    payload = json.dumps({
                        "model": model,
                        "messages": [{"role": "user", "content": content}],
                        "temperature": 0.2,
                        "max_tokens": 4096,
                        "stream": False,
                    }).encode()
                    req = urllib.request.Request(
                        f"{BASE}/chat/completions",
                        data=payload,
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {KEY}",
                        },
                    )
                    with urllib.request.urlopen(req, timeout=120) as r:
                        body = r.read().decode()
                    result = _parse_response(body)
                    if result:
                        _MODEL_HEALTH[model] = "ok"
                        return result
                    last_err = "respons valid tapi content kosong"
            except urllib.error.HTTPError as e:
                msg = e.read().decode(errors='replace')[:200]
                last_err = f"HTTP {e.code}: {msg}"
                if e.code in (402, 429):
                    _MODEL_HEALTH[model] = "dead"  # quota habis
                    break  # lanjut ke model berikutnya
            except RuntimeError as e:
                last_err = str(e)
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"

            if attempt < retries - 1:
                wait = min(2 ** (attempt + 1), 60)
                print(f"    [chat] model={model} percobaan {attempt+1} gagal ({last_err}), ulangi {wait}s...", flush=True)
                time.sleep(wait)

    raise RuntimeError(f"LLM gagal: semua model habis ({last_err})")