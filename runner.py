#!/usr/bin/env python3
"""Book translator runner – job-aware, paralel, fail-safe.

Pipeline per halaman:
  1. Ambil teks: pdftotext (PDF ber-teks, CEPAT) → jika < 25 kata,
     render PNG + OCR (vision LLM jika USE_VISION=1, fallback tesseract).
  2. Halaman < 25 kata = ilustrasi → sematkan PNG.
  3. Deteksi tabel/daftar-isi → full-page image, skip terjemahan.
  4. LLM translate (auto-detect bahasa → Indonesia; sumber Indonesia
     langsung lolos tanpa LLM), cache SQLite, anti-truncation.
  5. Baris OCR digabung menjadi paragraf alami (dehyphenation).
  6. Tulis page_NNNN.html secara atomik (tmp + rename).

Paralel: ThreadPoolExecutor (env WORKERS, default 4).
Fail-safe: multi-sweep — halaman gagal diulang di sweep berikutnya.
Resume: page_NNNN.html yang sudah ada dianggap selesai (idempotent).
MAX_PAGE otomatis di-clamp ke jumlah halaman riil PDF → tidak ada lagi
"phantom pages" (missing pages di luar isi buku).
"""
import pathlib, subprocess, shutil, sys, time, json, traceback, os, re, hashlib, sqlite3, signal
import urllib.request, urllib.error
from html import escape as _esc
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

from translate import chat

_pdf_env = os.getenv('PDF', '')
if not _pdf_env:
    raise RuntimeError("PDF env var required. Set to path of the job's book.pdf.")
PDF = pathlib.Path(_pdf_env)
# WORK_DIR WAJIB - fallback ke full_book_build = bug source folder campuran!
_work_env = os.getenv('WORK_DIR', '')
if not _work_env:
    raise RuntimeError("WORK_DIR environment variable is required. Set to job directory (e.g., jobs/<jobid>/).")
WORK_DIR = pathlib.Path(_work_env)
if not PDF.exists():
    raise FileNotFoundError(f"PDF not found: {PDF}")
if not (WORK_DIR / 'book.pdf').exists():
    raise FileNotFoundError(f"book.pdf not found in WORK_DIR — not a valid job directory: {WORK_DIR}")
# Validate job context — WORK_DIR harus ada meta.json yang match BOOK_TITLE
meta_file = WORK_DIR / 'meta.json'
if not meta_file.exists():
    raise FileNotFoundError(f"meta.json not found in WORK_DIR — not a valid job directory: {WORK_DIR}")
meta = json.loads(meta_file.read_text())
expected_title = (meta.get('title') or meta.get('filename', '')).replace('.pdf', '')
book_title = os.getenv('BOOK_TITLE', '')
if book_title and expected_title and book_title.replace('/', '') != expected_title.replace('/', ''):
    raise RuntimeError(f"BOOK_TITLE mismatch! meta={expected_title!r}, env={book_title!r}")
WORK_DIR.mkdir(parents=True, exist_ok=True)
IMG_DIR = WORK_DIR / "images"
IMG_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = WORK_DIR / "state.json"
PAGES_FILE = WORK_DIR / "pages.json"      # status per halaman (fail-safe)
ERROR_LOG = WORK_DIR / "error.log"
EPUB_PATH = pathlib.Path(os.getenv('EPUB_PATH', str(WORK_DIR / 'terjemahan.epub')))
MAX_PAGE = int(os.getenv('MAX_PAGE', '480'))
BATCH = int(os.getenv('BATCH_SIZE', '10'))          # ukuran gelombang (EPUB di-rebuild tiap gelombang)
WORKERS = max(1, int(os.getenv('WORKERS', '4')))    # paralel LLM calls
SWEEPS = int(os.getenv('SWEEPS', '3'))              # maksimum sweep untuk halaman yang gagal
BOOK_TITLE = os.getenv('BOOK_TITLE', 'Terjemahan Buku')
CACHE_DB = pathlib.Path(os.getenv('CACHE_DB', str(WORK_DIR / 'cache.sqlite')))  # per-job, bukan global
TEST_FAIL_PAGES = set(int(x) for x in os.getenv('TEST_FAIL_PAGES', '').split(',') if x.strip())
# PRESERVE_IMAGES=1 → setiap halaman: PNG selalu di-embed + teks tetap diterjemahkan
PRESERVE_IMAGES = os.getenv('PRESERVE_IMAGES', '0') == '1'
# --- Vision OCR: pakai model vision (multimodal) untuk render+OCR halaman scan
# Aktifkan dengan: USE_VISION=1  (fallback ke tesseract jika vision gagal)
USE_VISION = os.getenv('USE_VISION', '0') == '1'
# Lebar gambar yang dikirim ke vision (px). 1280 = keseimbangan akurasi/biaya.
VISION_WIDTH = int(os.getenv('VISION_WIDTH', '1280'))

# ---------------------------------------------------------------- pdf utils
def pdf_page_count():
    """Jumlah halaman riil PDF: pypdf → pdfinfo. 0 jika keduanya gagal."""
    try:
        import pypdf
        with open(PDF, 'rb') as fh:
            return len(pypdf.PdfReader(fh).pages)
    except Exception:
        pass
    try:
        out = subprocess.run(['pdfinfo', str(PDF)],
                             capture_output=True, text=True, timeout=60).stdout
        for line in out.splitlines():
            if line.startswith('Pages:'):
                return int(line.split(':')[1].strip())
    except Exception:
        pass
    return 0

# ---------------------------------------------------------------- table/TOC detect
# Halaman tabel / daftar isi → full-page image + terjemahan diskip.
# Deteksi: kata kunci (word-boundary, bukan substring) + pola struktur
# (paragraf pendek banyak + baris berangka) + TANPA paragraf naratif panjang.
TABLE_KEYWORDS = [
    # Indonesian / Dutch kata kunci tabel & statistik
    'tabel', 'tabel 1', 'statistik', 'curah hujan', 'regenval', 'stasiun',
    'stationsnaam', 'observatie', 'waarneming', 'periode', 'tijdvak',
    'gemiddeld', 'aantal', 'hoogte', 'nummer', 'bladzijde',
    # TOC/index markers
    'daftar isi', 'inhoudsopgave', 'table of contents', 'indeks', 'lampiran',
]
TOC_TITLE_RE = re.compile(r'\b(daftar isi|inhoudsopgave|table of contents|register van)\b')
TABLE_MIN_SHORT_RATIO = 0.70   # >=70% paragraf <=8 kata = pola tabel
TABLE_MIN_P = 6                # minimal paragraf agar layak dinilai
TABLE_MAX_PARA_WORDS = 30      # ada 1 paragraf >30 kata → naratif, bukan tabel
TABLE_AVG_PARA_WORDS = 14      # rata-rata kata/paragraf harus pendek

def looks_like_table_or_toc(text, html=None):
    """Deteksi pola tabel/daftar-isi dari teks halaman.
    text: teks mentah halaman; html: page_*.html bila ada.
    Return True → sisipkan full-page image, terjemahan diskip.
    Konservatif: lebih baik tabel lolos ke LLM daripada halaman naratif
    salah diskip (yang hilang dari buku)."""
    src = html or text
    paras = re.findall(r'<p[^>]*>(.*?)</p>', src, flags=re.DOTALL)
    paras = [re.sub(r'<[^>]+>', '', p).strip() for p in paras]
    if not paras:
        paras = [ln.strip() for ln in text.splitlines() if ln.strip()]
    paras = [p for p in paras if p]
    if len(paras) < TABLE_MIN_P:
        return False

    word_counts = [len(p.split()) for p in paras]
    short_ratio = sum(1 for w in word_counts if w <= 8) / len(paras)
    if short_ratio < TABLE_MIN_SHORT_RATIO:
        return False
    if max(word_counts) > TABLE_MAX_PARA_WORDS:      # ada kalimat panjang → naratif
        return False
    if sum(word_counts) / len(paras) > TABLE_AVG_PARA_WORDS:
        return False

    low = src.lower()
    kw_hits = sum(1 for k in TABLE_KEYWORDS
                  if re.search(rf'\b{re.escape(k)}\b', low))
    is_toc = bool(TOC_TITLE_RE.search(low))
    number_rows = sum(1 for p in paras if re.search(r'\d', p))
    return kw_hits >= 2 or is_toc or number_rows >= max(8, len(paras) // 2)

_stop = False          # SIGTERM → selesaikan halaman aktif, lalu stop
_DETECTED_LANG = None  # bahasa sumber, di-cache per-proses dari file per-job
_state_lock = Lock()

# ---------------------------------------------------------------- state
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}

def save_state(page=None, status="running", **extra):
    with _state_lock:
        try:
            st = load_state()
            if page is not None:
                st['last_page'] = max(st.get('last_page', 0), page)
            st['status'] = status
            st['done_pages'] = len(list(WORK_DIR.glob('page_*.html')))
            st['total_pages'] = MAX_PAGE
            st['updated_at'] = time.strftime("%Y-%m-%d %H:%M:%S")
            st.update(extra)
            tmp = STATE_FILE.with_suffix('.tmp')
            tmp.write_text(json.dumps(st))
            tmp.replace(STATE_FILE)
        except OSError as e:
            # state.json best-effort — sumber kebenaran tetap page_*.html
            print(f"[warn] gagal menulis state.json: {e}", flush=True)

def load_pages_status():
    if PAGES_FILE.exists():
        try:
            return json.loads(PAGES_FILE.read_text())
        except Exception:
            pass
    return {}

def save_pages_status(ps):
    try:
        tmp = PAGES_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(ps, indent=1))
        tmp.replace(PAGES_FILE)
    except OSError as e:
        print(f"[warn] gagal menulis pages.json: {e}", flush=True)

def classify_error(exc):
    """Klasifikasikan error → kategori manusia-bisa-baca."""
    s = str(exc).lower()
    if '402' in s or 'payment' in s:
        return 'quota(402)', 'skip model lain'
    if '429' in s or 'too many' in s:
        return 'limit(429)', 'cooldown otomatis'
    if 'timeout' in s or 'timed out' in s:
        return 'timeout', 'retry model berikut'
    if 'extra data' in s or 'json' in s:
        return 'parse(json)', 'fallback tesseract'
    if 'image' in s and 'url' in s:
        return 'vision(gambar)', 'fallback tesseract'
    return type(exc).__name__, 'lihat error.log'

def log_error(page, exc):
    cat, action = classify_error(exc)
    err = f"[Hal {page}] ERROR: {cat:16s} → {action} | {type(exc).__name__}: {exc}"
    print(err, file=sys.stderr, flush=True)
    with ERROR_LOG.open("a") as f:
        f.write(err + "\n" + traceback.format_exc() + "\n")

# ---------------------------------------------------------------- cache
def cache_get(src_text):
    """Ambil terjemahan dari cache (hash teks sumber)."""
    try:
        h = hashlib.sha1(src_text.encode()).hexdigest()
        con = sqlite3.connect(CACHE_DB, timeout=10)
        row = con.execute("SELECT tgt FROM translations WHERE h=?", (h,)).fetchone()
        con.close()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    return None

def cache_put(src_text, tgt_text):
    try:
        h = hashlib.sha1(src_text.encode()).hexdigest()
        con = sqlite3.connect(CACHE_DB, timeout=10)
        con.execute("CREATE TABLE IF NOT EXISTS translations (h TEXT PRIMARY KEY, src TEXT, tgt TEXT, created REAL)")
        con.execute("INSERT OR REPLACE INTO translations VALUES (?,?,?,?)", (h, src_text[:2000], tgt_text, time.time()))
        con.commit()
        con.close()
    except Exception:
        pass

# ---------------------------------------------------------------- epub
def update_epub():
    """Bangun ulang EPUB kumulatif dari page_*.html (atomic-ish)."""
    pages = sorted(WORK_DIR.glob("page_*.html"))
    css = """
    body { font-family: Georgia, 'Times New Roman', serif; font-size: 1em;
           line-height: 1.6; margin: 0 5%; text-align: justify; hyphens: auto; }
    h2 { font-size: 1.3em; font-weight: bold; margin: 1.5em 0 0.6em 0;
         text-align: center; color: #1a1a1a; border-bottom: 1px solid #ccc;
         padding-bottom: 0.3em; }
    p { margin: 0 0 0.9em 0; text-indent: 0; }
    .illustration-wrapper { margin: 1.5em 0; text-align: center;
                            page-break-inside: avoid; }
    .illustration-wrapper figure { margin: 0; padding: 0; }
    .illustration-wrapper img { max-width: 100%; height: auto;
        display: block; margin: 0 auto; border: 1px solid #ddd;
        border-radius: 3px; padding: 4px; background: #fff; }
    .illustration-wrapper figcaption { font-size: 0.85em; font-style: italic;
        color: #555; margin-top: 0.5em; padding: 0 1em; line-height: 1.4; }
    .page-image { margin: 1em 0; text-align: center;
                  page-break-inside: avoid; }
    .page-image img { max-width: 100%; height: auto; }
    """
    xhtml = [
        "<?xml version='1.0' encoding='UTF-8'?>",
        "<!DOCTYPE html>",
        "<html xmlns='http://www.w3.org/1999/xhtml' lang='id'>",
        f"<head><meta charset='utf-8'/><title>{BOOK_TITLE}</title>",
        f"<style type='text/css'>{css}</style></head>",
        "<body>",
    ]
    for pf in pages:
        xhtml.append(pf.read_text(errors="replace"))
    xhtml.extend(["</body>", "</html>"])
    book_file = WORK_DIR / "book.xhtml"
    book_file.write_text("\n".join(xhtml))
    try:
        subprocess.run([
            "pandoc", str(book_file), "-o", str(EPUB_PATH),
            "--metadata", f"title={BOOK_TITLE}",
            "--metadata", "lang=id",
            f"--resource-path={WORK_DIR}",
        ], check=True)
    except FileNotFoundError:
        raise RuntimeError("pandoc tidak ditemukan — pasang pandoc untuk membangun EPUB")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"pandoc gagal (rc={e.returncode}) — EPUB tidak dibangun")

# ---------------------------------------------------------- translate
def detect_language(nl_text):
    """Deteksi bahasa sumber — heuristik kata kunci (tanpa asumsi, tanpa default 'Dutch').
    Hasil di-cache per-proses + file PER-JOB (tidak bocor antar job)."""
    global _DETECTED_LANG
    if _DETECTED_LANG:
        return _DETECTED_LANG
    # lanjutkan deteksi job sebelumnya (retry tidak mendeteksi ulang)
    lang_file = WORK_DIR / 'detected_lang.txt'
    if lang_file.exists():
        try:
            prev = lang_file.read_text().strip()
            if prev:
                _DETECTED_LANG = prev
                return prev
        except Exception:
            pass
    low = " " + nl_text[:2000].lower() + " "

    scores = {
        'Dutch': sum(low.count(w) for w in [
            ' het ', ' een ', ' van ', ' de ', ' en ', ' zijn ', ' wordt ',
            ' niet ', ' ook ', ' deze ', ' door ', ' werd ', ' kan ',
            'gezicht', 'haven', 'eiland', 'bewoner', 'volgens',
            'inlandsche', 'residentie', 'tijdvak', 'waarneming']),
        'English': sum(low.count(w) for w in [
            ' the ', ' and ', ' of ', ' to ', ' in ', ' was ', ' were ',
            ' this ', ' that ', ' with ', ' from ', ' his ', ' had ',
            ' which ', ' their ', ' forces ', ' soldiers ', ' unit ']),
        'Indonesian': sum(low.count(w) for w in [
            ' yang ', ' dan ', ' dengan ', ' untuk ', ' tidak ', ' adalah ',
            ' pada ', ' dari ', ' ini ', ' itu ', ' telah ', ' akan ']),
    }
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        # Tidak ada kata kunci dikenal → tanya LLM, jangan asumsikan
        try:
            resp = chat(
                "What language is this text? Reply with the language name in "
                "English only, one word, nothing else.\n\n" + nl_text[:400])
            best = (resp or "").strip().splitlines()[0][:40] or "Unknown"
        except Exception:
            best = "Unknown"

    _DETECTED_LANG = best
    try:
        lang_file.write_text(best)
    except Exception:
        pass
    print(f"[lang] Bahasa sumber terdeteksi: {best} (skor: {scores})", flush=True)
    return best

def translate_page_text(raw_text, page_num=None):
    """Terjemahkan satu teks halaman → Indonesia (cache + retry).
    Sumber sudah Bahasa Indonesia → lolos langsung tanpa panggil LLM.
    page_num: nomor halaman (dipakai TEST_FAIL_PAGES)."""
    cached = cache_get(raw_text)
    if cached:
        return cached, True
    lang = detect_language(raw_text)
    if lang.lower().startswith('indones'):
        print(f"    Hal {page_num or '?'}: sumber sudah Bahasa Indonesia → tanpa terjemahan", flush=True)
        cache_put(raw_text, raw_text)
        return raw_text, False
    prompt = (
        f"Translate this {lang} book text to natural, fluent Indonesian "
        f"(Bahasa Indonesia). Keep names, dates, and numbers unchanged. "
        f"Output ONLY the translation, no explanations.\n\n{raw_text}"
    )
    if TEST_FAIL_PAGES and page_num in TEST_FAIL_PAGES:
        raise RuntimeError(f"TEST_FAIL_PAGES aktif (halaman {page_num} gagal disengaja)")
    out = chat(prompt, retries=5)   # chat() sudah retry+backoff+anti-truncation
    if not out or not out.strip():
        raise RuntimeError("terjemahan kosong dari model")
    cache_put(raw_text, out)
    return out, False

# ---------------------------------------------------------------- preprocessing
def preprocess_image(src_png):
    """OpenCV preprocessing untuk INPUT VISION: deskew + denoise + CLAHE.
    Tetap grayscale — TIDAK binarisasi (vision lebih akurat pada gambar
    natural; binarisasi merusak detail ilustrasi).
    Output hanya dipakai sebagai input vision; crop/embed EPUB tetap
    pakai PNG asli. Return path PNG hasil preprocess (fallback: asli).
    """
    try:
        import cv2, numpy as np
    except ImportError as e:
        print(f"    [warn] preprocessing dilewati (butuh opencv-python & numpy): {e}",
              flush=True)
        return str(src_png)
    src = pathlib.Path(str(src_png))   # pastikan Path object (bukan str)
    img = cv2.imread(str(src), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return str(src)
    try:
        # 1. Denoise cepat (median 3x3 — cukup untuk grain scan)
        img = cv2.medianBlur(img, 3)
        # 2. Kontras adaptif CLAHE — teks/ilustrasi samar jadi jelas
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        img = clahe.apply(img)
        # 3. Deskew: koreksi kemiringan kecil (<5°), deteksi dari piksel gelap
        coords = np.column_stack(np.where(img < 128))
        if len(coords) > 100:
            angle = cv2.minAreaRect(coords)[-1]
            if angle < -45:
                angle = -(90 + angle)
            else:
                angle = -angle
            if 0.05 < abs(angle) < 5.0:
                (h, w) = img.shape[:2]
                M = cv2.getRotationMatrix2D((w // 2, h // 2), angle, 1.0)
                img = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_CUBIC,
                                     borderMode=cv2.BORDER_REPLICATE)
    except Exception:
        return str(src)   # aman: kembalikan asli jika preprocessing gagal
    out = src.parent / f"{src.stem}_preproc.png"
    cv2.imwrite(str(out), img)
    return str(out)


# ---------------------------------------------------------------- vision OCR
def vision_ocr_page(png_path, page_num, text_only=False):
    """Vision OCR: transkrip teks + deteksi ilustrasi oleh model multimodal.
    - text_only=True → prompt hanya minta teks (ilustrasi diurus cache caller)
    Return: (text, illustrations) atau (None, None) jika gagal —
      caller menentukan fallback (rescue/tesseract).
      illustrations: list of dict {label, description, y_start_pct, y_end_pct}
    """
    import base64, json as _json
    from PIL import Image
    png_path = pathlib.Path(str(png_path))
    # Resize ke VISION_WIDTH px (default 1280 — akurat OCR, hemat bandwidth)
    im = Image.open(str(png_path)).convert('RGB')
    w, h = im.size
    ratio = min(VISION_WIDTH / w, 1.0)
    small_path = png_path.with_suffix('.vision.jpg')
    im.resize((max(1, int(w * ratio)), max(1, int(h * ratio))),
              Image.LANCZOS).save(str(small_path), 'JPEG', quality=80)
    try:
        with open(str(small_path), 'rb') as f:
            b64 = base64.b64encode(f.read()).decode()
    finally:
        small_path.unlink(missing_ok=True)   # file sementara → segera dihapus

    if text_only:
        prompt = (
            "Transcribe ALL text visible on this page, line by line, verbatim. "
            "Detect the source language yourself and do NOT assume it. "
            "Respond in JSON format ONLY:\n"
            '{"text": "full transcription"}'
        )
    else:
        prompt = (
            "Analyze the attached page image. "
            "Transcribe ALL text visible on this page, line by line, verbatim. "
            "Detect the source language yourself and do NOT assume it. "
            "Respond in JSON format ONLY:\n"
            "{\n"
            '  "text": "full transcription of ALL text visible on this page, line by line",\n'
            '  "illustrations": [\n'
            '    {"label": "Fig. X or Plaat X or caption", "description": "what the image shows", '
            '"y_start_pct": 0, "y_end_pct": 100}\n'
            '  ]\n'
            "}\n"
            "If no illustrations, return empty array. y_start_pct/y_end_pct = vertical position "
            "as percentage (0=top, 100=bottom) of the illustration on the page."
        )

    try:
        data_uri = f"data:image/jpeg;base64,{b64}"
        resp = chat(prompt, retries=3, image_data_uri=data_uri)
        json_match = re.search(r'\{[\s\S]*\}', resp or '')
        if json_match:
            data = _json.loads(json_match.group())
            text_val = data.get('text', '')
            if isinstance(text_val, list):   # vision kadang return array
                text_val = ' '.join(str(t) for t in text_val)
            illustrations = data.get('illustrations', [])
            if not isinstance(illustrations, list):
                illustrations = []
            if text_val and len(text_val.split()) >= 5:
                return str(text_val), illustrations
    except urllib.error.HTTPError as e:
        if e.code == 429:
            print(f"    [warn] vision quota 429 (rate limit) hal {page_num} — fallback tesseract", flush=True)
        else:
            print(f"    [warn] vision HTTP error hal {page_num}: {e.code} — fallback tesseract", flush=True)
    except Exception as e:
        print(f"    [warn] vision OCR gagal hal {page_num}: {e} — fallback tesseract", flush=True)

    return None, None

def crop_illustration(src_png, y1_pct, y2_pct, page_num, fig_idx):
    """Crop region ilustrasi berdasarkan posisi persen Y (dari vision).
    Return: path ke JPEG crop, atau None jika gagal."""
    from PIL import Image
    img = Image.open(str(src_png))
    w, h = img.size
    y1 = int(h * y1_pct / 100)
    y2 = int(h * y2_pct / 100)
    M = 15
    y1 = max(0, y1 - M)
    y2 = min(h, y2 + M)
    crop = img.crop((0, y1, w, y2))
    if w > 1200:
        ratio = 1200 / w
        crop = crop.resize((1200, int((y2 - y1) * ratio)), Image.LANCZOS)
    out_path = IMG_DIR / f"crop_{page_num:04d}_{fig_idx}.jpg"
    crop.convert('RGB').save(str(out_path), 'JPEG', quality=85)
    return out_path

# ------------------------------------------------------- satu halaman
def _render_page_png(p):
    """Render halaman p ke PNG 300dpi → return path file.
    Gagal → RuntimeError dengan pesan yang bisa ditindaklanjuti."""
    try:
        subprocess.run([
            "pdftoppm", "-f", str(p), "-l", str(p), "-r", "300", "-png",
            str(PDF), str(WORK_DIR / f"p_{p}")
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"render halaman {p} gagal (pdftoppm rc={e.returncode}) — "
            f"PDF mungkin rusak atau halaman di luar rentang")
    except FileNotFoundError:
        raise RuntimeError("pdftoppm tidak ditemukan — pasang paket poppler-utils")
    files = list(WORK_DIR.glob(f"p_{p}-*.png"))
    if not files:
        raise RuntimeError(f"render halaman {p}: tidak ada file PNG output")
    return files[0]

def _cleanup_page_temp(p):
    """Hapus semua file sementara halaman p (PNG render, vision jpg, preproc, ocr)."""
    for f in WORK_DIR.glob(f"p_{p}-*"):        # p_XXXX-1.png + .vision.jpg + _preproc
        f.unlink(missing_ok=True)
    for f in WORK_DIR.glob(f"ocr_{p}.*"):
        f.unlink(missing_ok=True)

def extract_text(p):
    """Teks halaman p: pdftotext dulu (PDF ber-teks, CEPAT), fallback OCR (scan).
    Jika PRESERVE_IMAGES=1: SELALU render PNG + OCR (tanpa pakai pdftotext).
    Jika USE_VISION=1: gunakan Vision OCR dengan escalation chain berbasis bukti:
      a) Cache illustrations ada → vision text-only (preprocessed) [+illus dari cache]
      b) Cache tidak ada → vision full (PNG asli, text+illus)
      c) Vision text < 10 kata → rescue: preprocessed text-only
      d) Tetap gagal → tesseract"""
    if not PRESERVE_IMAGES:
        # 1. coba pdftotext (cepat, tanpa gambar)
        try:
            r = subprocess.run(
                ["pdftotext", "-f", str(p), "-l", str(p), str(PDF), "-"],
                capture_output=True, text=True, timeout=120)
            txt = (r.stdout or "").strip()
            if txt and len(txt.split()) >= 25:
                return txt, None   # PDF punya layer teks → skip OCR
        except Exception:
            pass
    # 2. render PNG (untuk OCR scan / PRESERVE_IMAGES / vision)
    src_png = _render_page_png(p)

    # 3. Vision OCR (jika aktif)
    if USE_VISION:
        il_cache = WORK_DIR / f"illustrations_{p}.json"
        has_ilus_cache = il_cache.exists()

        # a) Cache ilustrasi ada → text-only pada preprocessed (hemat, akurat)
        if has_ilus_cache:
            try:
                preproc = preprocess_image(src_png)
                vt, _ = vision_ocr_page(preproc, p, text_only=True)
                if vt and len(vt.split()) >= 10:
                    illus = json.loads(il_cache.read_text())
                    print(f"    Hal {p}: [vision+cache] OCR {len(vt.split())} kata, "
                          f"{len(illus)} ilus (cached)", flush=True)
                    return vt, src_png
            except Exception as ve:
                print(f"    [warn] vision+cache gagal hal {p}: {ve}", flush=True)

        # b) Vision full pada PNG asli
        try:
            vision_text, illustrations = vision_ocr_page(src_png, p, text_only=False)
            if vision_text is not None and len(vision_text.split()) >= 10:
                print(f"    Hal {p}: [vision] OCR {len(vision_text.split())} kata, "
                      f"{len(illustrations)} ilustrasi terdeteksi", flush=True)
                if illustrations:
                    il_cache.write_text(json.dumps(illustrations))
                return vision_text, src_png
        except Exception as ve:
            print(f"    [warn] vision gagal hal {p}: {ve} — rescue preprocessed", flush=True)

        # c) Rescue: preprocessed text-only
        try:
            preproc = preprocess_image(src_png)
            vt, _ = vision_ocr_page(preproc, p, text_only=True)
            if vt and len(vt.split()) >= 10:
                print(f"    Hal {p}: [vision rescue] OCR {len(vt.split())} kata", flush=True)
                return vt, src_png
        except Exception as ve2:
            print(f"    [warn] vision rescue gagal hal {p}: {ve2} — fallback tesseract", flush=True)

    # 4. Fallback: tesseract OCR (vision gagal atau tidak aktif)
    ocr_prefix = str(WORK_DIR / f"ocr_{p}")
    try:
        subprocess.run([
            "tesseract", str(src_png), ocr_prefix, "-l", "nld+eng+ind", "--psm", "3"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        raise RuntimeError(
            "tesseract tidak ditemukan — pasang tesseract-ocr "
            "(+ bahasa nld/eng/ind) untuk buku scan")
    txt_path = pathlib.Path(f"{ocr_prefix}.txt")
    if not txt_path.exists():
        raise RuntimeError(f"tesseract tidak menghasilkan output untuk halaman {p}")
    return txt_path.read_text(errors="replace").strip(), src_png

def merge_ocr_paragraphs(txt):
    """Gabungkan baris OCR menjadi paragraf alami untuk EPUB.
    - Baris kosong → batas paragraf
    - Baris sebelumnya pendek (relatif thd lebar teks) + diakhiri titik → batas
    - Dehyphenation: 'konti-' + 'nental' → 'kontinental' (selalu digabung)
    Return string paragraf dipisah baris kosong."""
    raw = [l.strip() for l in txt.splitlines()]
    max_line = max((len(l) for l in raw if l), default=0)
    thresh = max(40, int(max_line * 0.7))   # "pendek" relatif thd lebar teks
    paras, cur = [], []

    def flush():
        if cur:
            paras.append(" ".join(cur))
            cur.clear()

    for s in raw:
        if not s:
            flush()
            continue
        if cur:
            prev = cur[-1]
            if prev.endswith('-'):
                cur[-1] = prev[:-1] + s     # dehyphenate: selalu digabung
                continue
            if len(prev) < thresh and prev.endswith(('.', '!', '?', '…', '"')):
                flush()                     # akhir kalimat di baris pendek → paragraf baru
        cur.append(s)
    flush()
    return "\n\n".join(paras)

def _append_illustrations(p, lines, ilus, src_png):
    """Sisipkan ilustrasi hasil deteksi vision:
    - >=2 ilustrasi → full-page image + terjemahan di bawah
    - 1 ilustrasi → smart-crop dengan deteksi konten (numpy)."""
    from PIL import Image as _PILImage
    if len(ilus) >= 2:
        dest = IMG_DIR / f"page_{p:04d}.jpg"
        try:
            src_full = src_png or _render_page_png(p)
            _PILImage.open(str(src_full)).convert('RGB').save(
                str(dest), 'JPEG', quality=85)   # PNG 300dpi besar → JPEG hemat
            lines.append(
                f"<div class='illustration-wrapper'><figure>"
                f"<img src='images/page_{p:04d}.jpg' alt='Halaman {p}' "
                f"style='max-width:100%;'>"
                f"<figcaption>Ilustrasi asli halaman {p} (terjemahan di bawah)</figcaption>"
                f"</figure></div>"
            )
            print(f"    Hal {p}: {len(ilus)} ilustrasi -> full-page image (JPEG)", flush=True)
        except Exception as fe:
            print(f"    [warn] gagal full-page hal {p}: {fe}", flush=True)
        return

    # --- 1 ilustrasi → smart crop ---
    src_for_crop = src_png or _render_page_png(p)
    img_full = _PILImage.open(str(src_for_crop)).convert('RGB')
    w_img, h_img = img_full.size
    import numpy as np
    img_arr = np.array(img_full.convert('L'))
    has_content_mask = img_arr < 200
    rows_with_content = np.where(np.any(has_content_mask, axis=1))[0]
    cols_with_content = np.where(np.any(has_content_mask, axis=0))[0]
    if len(rows_with_content) < 10 or len(cols_with_content) < 10:
        print(f"    [warn] hal {p} tidak ada konten visual cukup", flush=True)
        return
    for idx, ill in enumerate(ilus):
        try:
            ys_pct = max(0.0, min(100.0, float(ill.get('y_start_pct', 0))))
            ye_pct = max(0.0, min(100.0, float(ill.get('y_end_pct', 100))))
            if ye_pct - ys_pct < 5:
                continue
            y1_vision = int(h_img * ys_pct / 100)
            y2_vision = int(h_img * ye_pct / 100)
            margin = int((y2_vision - y1_vision) * 0.15)
            y1 = max(0, y1_vision - margin)
            y2 = min(h_img, y2_vision + margin)
            region_mask = has_content_mask[y1:y2, :]
            region_cols = np.where(np.any(region_mask, axis=0))[0]
            if len(region_cols) < 5:
                continue
            # perluas ke bawah selama ada konten menempel (batasi 1.2x tinggi ilus)
            y2_try = y2
            while y2_try < h_img and has_content_mask[y2_try:y2_try + 20, :].any():
                y2_try += 20
            max_ext = int((y2_vision - y1_vision) * 1.2)
            if y2_try > y2 and (y2_try - y2) < max_ext:
                y2 = min(h_img, y2_try)
            region_mask = has_content_mask[y1:y2, :]
            region_cols = np.where(np.any(region_mask, axis=0))[0]
            x1, x2 = (0, w_img) if len(region_cols) < 5 else \
                (max(0, int(region_cols[0])), min(w_img, int(region_cols[-1]) + 1))
            crop = img_full.crop((x1, y1, x2, y2))
            max_dim = max(crop.width, crop.height)
            if max_dim > 1200:
                r = 1200 / max_dim
                crop = crop.resize((int(crop.width * r), int(crop.height * r)),
                                   _PILImage.LANCZOS)
            crop_path = IMG_DIR / f"crop_{p:04d}_{idx + 1}.jpg"
            crop.save(str(crop_path), 'JPEG', quality=85)
            label = str(ill.get('label', f'Gambar {idx + 1}'))
            desc = str(ill.get('description', ''))
            try:
                raw_caption = f"{label}. {desc}".strip()
                id_caption, _ = translate_page_text(raw_caption, page_num=p)
            except Exception:
                id_caption = label
            lines.append("<div class='illustration-wrapper'><figure>")
            lines.append(f"<img src='images/crop_{p:04d}_{idx + 1}.jpg' alt='{_esc(label)}'>")
            lines.append(f"<figcaption>{_esc(id_caption)}</figcaption>")
            lines.append("</figure></div>")
        except Exception as ie:
            print(f"    [warn] crop gagal hal {p} ilus {idx + 1}: {ie}", flush=True)
    print(f"    Hal {p}: {len(ilus)} ilustrasi cropped (smart)", flush=True)

def process_page(p):
    """Proses satu halaman (dipanggil paralel oleh pool). Return (p, True/False)."""
    if _stop:
        return p, False
    page_html = WORK_DIR / f"page_{p:04d}.html"
    if page_html.exists():
        save_state(p)
        return p, True
    try:
        print(f"[*] Hal {p}/{MAX_PAGE} ekstraksi teks...", flush=True)
        raw_text, src_png = extract_text(p)
        word_cnt = len(raw_text.split())
        if word_cnt < 25:
            # Ilustrasi: butuh PNG (mungkin belum dirender jika pdftotext jalan)
            if src_png is None:
                src_png = _render_page_png(p)
            dest = IMG_DIR / f"page_{p:04d}.png"
            shutil.copy(src_png, dest)
            html = (
                f"<h2>Halaman {p} (Ilustrasi)</h2>\n"
                f"<div><img src='images/page_{p:04d}.png' alt='Hal {p}' "
                f"style='max-width:100%;'/></div>"
            )
        else:
            lines = [f"<h2>Halaman {p}</h2>"]
            # --- TABEL / DAFTAR ISI: full-page image, SKIP terjemahan ---
            if looks_like_table_or_toc(raw_text):
                try:
                    dest = IMG_DIR / f"page_{p:04d}.jpg"
                    src_full = src_png or _render_page_png(p)
                    from PIL import Image as _PIL2
                    img = _PIL2.open(str(src_full)).convert('RGB')
                    if max(img.size) > 1600:
                        r = 1600 / max(img.size)
                        img = img.resize((int(img.width * r), int(img.height * r)),
                                         _PIL2.LANCZOS)
                    img.save(str(dest), 'JPEG', quality=85)
                    lines.append(
                        f"<div class='illustration-wrapper'><figure>"
                        f"<img src='images/page_{p:04d}.jpg' alt='Tabel hal {p}' "
                        f"style='max-width:100%;'>"
                        f"<figcaption>Tabel/daftar asli halaman {p} — tampilan asli "
                        f"dipertahankan (terjemahan diabaikan karena struktur tabel "
                        f"tidak dapat dipertahankan).</figcaption>"
                        f"</figure></div>"
                    )
                    html = "\n".join(lines)
                    tmp = page_html.with_suffix('.tmp')
                    tmp.write_text(html)
                    tmp.replace(page_html)
                    _cleanup_page_temp(p)
                    save_state(p)
                    print(f"[OK] Hal {p} selesai (tabel - image only).", flush=True)
                    return p, True
                except Exception as te:
                    print(f"    [warn] gagal sisip tabel hal {p}: {te}", flush=True)

            print(f"    Hal {p}: terjemahan ({word_cnt} kata)...", flush=True)
            id_txt, from_cache = translate_page_text(raw_text, page_num=p)
            if from_cache:
                print(f"    Hal {p}: [cache] terjemahan ditemukan di cache", flush=True)

            # --- VISION: sisipkan ilustrasi hasil deteksi ---
            ilus_path = WORK_DIR / f"illustrations_{p}.json"
            if ilus_path.exists():
                try:
                    ilus = json.loads(ilus_path.read_text())
                    if isinstance(ilus, list) and ilus:
                        _append_illustrations(p, lines, ilus, src_png)
                except Exception as iee:
                    print(f"    [warn] ilustrasi hal {p} gagal diproses: {iee}", flush=True)

            # PRESERVE_IMAGES: sisipkan gambar halaman utuh di atas teks
            if PRESERVE_IMAGES and src_png is not None:
                dest = IMG_DIR / f"page_{p:04d}.png"
                try:
                    shutil.copy(src_png, dest)
                    lines.append(
                        f"<div class='page-image'><img src='images/page_{p:04d}.png' "
                        f"alt='Halaman {p}' style='max-width:100%;'/></div>"
                    )
                except Exception as ie:
                    print(f"    [warn] gagal copy gambar halaman {p}: {ie}", flush=True)

            # --- paragraf alami: gabungkan baris OCR/terjemahan ---
            for para in merge_ocr_paragraphs(id_txt).split("\n\n"):
                if para.strip():
                    lines.append(f"<p>{_esc(para.strip())}</p>")
            html = "\n".join(lines)
        # tulis atomik: tmp → rename (tidak ada setengah-file)
        tmp = page_html.with_suffix('.tmp')
        tmp.write_text(html)
        tmp.replace(page_html)
        _cleanup_page_temp(p)
        save_state(p)
        print(f"[OK] Hal {p} selesai.", flush=True)
        return p, True
    except Exception as e:
        log_error(p, e)
        _cleanup_page_temp(p)   # bersihkan sisa sementara agar sweep berikutnya bersih
        return p, False

# ---------------------------------------------------------------- main
def run_wave(pages):
    """Proses satu gelombang halaman secara paralel. Return jumlah sukses."""
    ok = 0
    done = 0
    total = len(pages)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(process_page, p): p for p in pages}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                _, success = fut.result()
                if success:
                    ok += 1
            except Exception as e:
                log_error(p, e)
            done += 1
            if done % 50 == 0 or done == total:
                pct = int(done / total * 100)
                print(f"  Progress gelombang: {done}/{total} ({pct}%) — {ok} ok, "
                      f"{done - ok} error", flush=True)
    return ok

def _repair_state():
    """Clamp MAX_PAGE ke jumlah halaman PDF riil + perbaiki state phantom.
    Phantom pages = missing_pages/total_pages di luar isi PDF sebenarnya
    (mis. MAX_PAGE env 480 padahal PDF 324 halaman) → job false-error."""
    global MAX_PAGE
    real = pdf_page_count()
    if real:
        if MAX_PAGE <= 0:
            # count_pages gagal di app saat upload → jangan biarkan job selesai 0 halaman
            print(f"[fix] MAX_PAGE {MAX_PAGE} tidak valid → set ke halaman riil PDF {real}",
                  flush=True)
            MAX_PAGE = real
        elif MAX_PAGE > real:
            print(f"[fix] MAX_PAGE {MAX_PAGE} > halaman riil PDF ({real}) → clamp ke {real}",
                  flush=True)
            MAX_PAGE = real
    elif MAX_PAGE <= 0:
        print("[warn] jumlah halaman PDF tidak bisa dibaca & MAX_PAGE 0 — "
              "pakai 480; halaman di luar rentang akan gagal terlihat", flush=True)
        MAX_PAGE = 480
    st = load_state()
    missing = [p for p in range(1, MAX_PAGE + 1)
               if not (WORK_DIR / f"page_{p:04d}.html").exists()]
    stale_missing = st.get('missing_pages') or []
    if stale_missing != missing or st.get('total_pages') != MAX_PAGE:
        if any(x > MAX_PAGE for x in stale_missing):
            print(f"[fix] state phantom dibersihkan: "
                  f"{len([x for x in stale_missing if x > MAX_PAGE])} halaman di luar PDF",
                  flush=True)
        st['missing_pages'] = missing
        st['total_pages'] = MAX_PAGE
        if not missing:
            st['status'] = 'done'
            print("[fix] Semua halaman riil sudah ada → status done", flush=True)
        tmp = STATE_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(st))
        tmp.replace(STATE_FILE)
    return missing

def main():
    # Tulis PID file — buat cancel bekerja setelah service restart
    try:
        (WORK_DIR / '.runner.pid').write_text(str(os.getpid()))
    except Exception as e:
        print(f"[warn] gagal tulis PID file: {e}", flush=True)

    missing = _repair_state()
    ps = load_pages_status()   # per-page status: attempts, last_error
    pending = missing
    print(f"Total {MAX_PAGE} halaman, pending {len(pending)}, "
          f"workers {WORKERS}, gelombang {BATCH}, sweep maks {SWEEPS}", flush=True)
    if not pending:
        print("Semua halaman sudah ada — cukup rebuild EPUB.", flush=True)

    sweep = 0
    while pending and sweep < SWEEPS and not _stop:
        sweep += 1
        if sweep > 1:
            wait = 30
            print(f"=== Sweep {sweep}/{SWEEPS}: retry {len(pending)} halaman "
                  f"gagal (tunggu {wait}s dulu) ===", flush=True)
            for _ in range(wait):
                if _stop:
                    break
                time.sleep(1)
        remaining = list(pending)
        # proses dalam gelombang BATCH → EPUB diperbarui berkala
        for i in range(0, len(remaining), BATCH):
            if _stop:
                break
            wave = remaining[i:i + BATCH]
            ok = run_wave(wave)
            try:
                update_epub()
                print(f"=== Gelombang selesai ({ok}/{len(wave)} sukses) → EPUB diperbarui ===",
                      flush=True)
            except Exception as e:
                log_error(0, e)
        # hitung ulang pending setelah sweep
        pending = [p for p in pending if not (WORK_DIR / f"page_{p:04d}.html").exists()]
        # catat percobaan per halaman
        for p in pending:
            ent = ps.get(str(p), {})
            ent['attempts'] = ent.get('attempts', 0) + 1
            ps[str(p)] = ent
        save_pages_status(ps)
        if pending and sweep < SWEEPS:
            print(f"[!] {len(pending)} halaman masih gagal, akan diulang.", flush=True)

    # ---- status akhir (fail-safe: tidak ada halaman hilang diam-diam)
    missing = [p for p in range(1, MAX_PAGE + 1)
               if not (WORK_DIR / f"page_{p:04d}.html").exists()]
    try:
        update_epub()
    except Exception as e:
        log_error(0, e)
    if _stop:
        save_state(status="cancelled", missing_pages=missing)
        print(f"DIHENTIKAN: {len(missing)} halaman belum selesai (bisa dilanjutkan).", flush=True)
    elif missing:
        save_state(status="error", missing_pages=missing)
        print(f"ERROR: {len(missing)} halaman gagal setelah {SWEEPS} sweep: {missing}", flush=True)
        print("Job ditandai error — gunakan tombol 'Coba lagi' di dashboard.", flush=True)
        sys.exit(1)
    elif not EPUB_PATH.exists():
        # semua halaman ada tetapi EPUB tidak terbangun → JANGAN bilang done
        save_state(MAX_PAGE, status="error",
                   error_msg="EPUB gagal dibangun — pastikan pandoc terpasang")
        print("ERROR: halaman lengkap tetapi EPUB gagal dibangun (pandoc?).", flush=True)
        sys.exit(1)
    else:
        save_state(MAX_PAGE, status="done", missing_pages=[])
        print(f"SELESAI 100%: semua {MAX_PAGE} halaman → {EPUB_PATH}", flush=True)

def _sigterm(signum, frame):
    global _stop
    _stop = True
    print("[SIGTERM] berhenti setelah halaman aktif selesai...", flush=True)

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)
    try:
        main()
    except SystemExit:
        raise                       # exit path yang disengaja (error/cancel) biarkan lewat
    except BaseException:
        # kegagalan tak terduga: catat ke error.log + state error, bukan mati diam-diam
        import traceback as _tb
        err = f"[FATAL] runner berhenti tak terduga: {sys.exc_info()[1]}\n{_tb.format_exc()}"
        print(err, file=sys.stderr, flush=True)
        try:
            with open(ERROR_LOG, "a") as f:
                f.write(err + "\n")
            save_state(status="error")
        except Exception:
            pass
        sys.exit(1)
    finally:
        # hapus PID file saat keluar (selesai / dihentikan)
        try:
            (WORK_DIR / '.runner.pid').unlink(missing_ok=True)
        except Exception:
            pass
