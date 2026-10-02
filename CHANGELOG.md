# Changelog

Format mengikuti [Keep a Changelog](https://keepachangelog.com/id-ID/1.1.0/).

## [5.1] — Rilis final

### Keamanan
- **Autentikasi dashboard**: set env `FLASK_PASSWORD` → semua halaman/API wajib login (sesi cookie); API fetch mendapat 401 JSON, halaman redirect ke `/login`.
- **CSRF token** diaktifkan bersama autentikasi: semua POST (upload, cancel, retry, delete, simpan Settings) divalidasi via header `X-CSRF-Token` / form.
- **Rate-limit login**: 5× salah → terkunci 30 detik per IP; verifikasi password memakai `hmac.compare_digest`.
- **Secret key sesi persisten**: env `FLASK_SECRET` → `config/.secret_key` (chmod 600) — sesi login tidak hangus saat restart.
- **Mode terbuka tanpa password** tetap bisa, dengan banner peringatan kuning di UI.

### Ditambah
- **Notifikasi Telegram/webhook** saat job selesai/gagal — dikonfigurasi di Settings dengan tombol "Kirim tes"; kegagalan notifikasi tidak pernah memengaruhi job.
- **Re-attach job setelah restart**: status job dibaca ulang dari `.runner.pid` (`_runner_alive`) sehingga job yang masih hidup tampil "running", bukan "error".
- **Suite pytest** (`tests/`, `pytest.ini`) — durability config, error handling, deteksi tabel, merge paragraf, auth/CSRF, dan alur `__KEEP__`.

### Diperbaiki
- Error handling menyeluruh: handler global (413/404/500 — JSON untuk `/api/*`), thread `start_job` gagal → state `error` + `error.log` (tidak ada lagi UI stuck "running"), pesan jelas untuk `pdftoppm`/`pandoc`/`tesseract` yang tidak terpasang, guard opencv, `MAX_PAGE` 0/invalid → clamp ke halaman riil, `save_state` tahan disk penuh, top-level catch runner.
- `translate.py`: pesan error manusiawi (provider kosong, koneksi gagal, key ditolak 401/403, filter keamanan SAFETY), `fetch_remote_models` dengan hint per kode HTTP, filter model gemini non-`generateContent`.
- Judul buku dibersihkan total: prefix UUID + token hex hash dibuang, `--` → em dash.

## [5.0] — Optimasi besar

### Diperbaiki (dari audit v4)
- Cancel job tidak lagi error 500 (`import time`).
- **Phantom pages** hilang: `MAX_PAGE` di-clamp ke jumlah halaman PDF riil (`_repair_state`), state lama dibersihkan, job false-error (mis. Sriwijaya 324/324) otomatis pulih.
- Docker: `pandoc` + tesseract nld/eng/ind + requirements lengkap; runner via `sys.executable`.
- `vision_ocr_page` tidak lagi NameError (`urllib` di-import).
- Judul buku & nama EPUB tidak lagi memuat prefix UUID.
- Terjemahan terpotong (`finish_reason=length`) tidak pernah masuk cache — anti-truncation dengan penggandaan `max_tokens` otomatis.

### Ditambah
- **Multi-provider LLM**: OpenAI-compatible (router bawaan, OpenRouter, vLLM, Ollama…) + **Gemini-compatible**, fallback berurutan antar provider — dikonfigurasi di halaman Settings dengan tes koneksi & pengambilan model otomatis.
- **Penyimpanan config andal**: tulis atomik + fsync, backup `.bak` otomatis, pemulihan dari file korup (file korup tidak pernah ditimpa), chmod 600.
- **Vision OCR diperkuat**: 1280px, preprocessing (deskew + CLAHE + denoise), smart-crop ilustrasi, ≥2 ilustrasi → full-page image; deteksi tabel/TOC dengan word-boundary + syarat struktur (naratif tidak salah diskip).
- **Paragraf alami di EPUB**: penggabungan baris OCR + dehyphenation.
- Buku berbahasa Indonesia lolos tanpa LLM.
- Redesign dashboard (style.css bersama: Upload / Pekerjaan / Settings), penjelasan kapan Vision OCR perlu diaktifkan.
