# 🐻📚 Bebaa Book Translator

Terjemahkan buku PDF (bahasa apa pun) → **EPUB Bahasa Indonesia**, dengan dashboard web mobile-friendly.

## Fitur

- **Upload PDF** → auto-detect bahasa sumber → terjemahan ke Indonesia → EPUB siap unduh
- **Buku scan** tetap diproses: OCR tesseract fallback; **Vision OCR** (model multimodal) untuk scan jelek & halaman kaya ilustrasi
- **Deteksi tabel/daftar-isi** → halaman disematkan sebagai gambar asli (struktur tabel tidak dihancurkan LLM)
- **Ilustrasi otomatis**: dideteksi, di-potong (smart-crop), diberi keterangan terjemahan di EPUB
- **Fail-safe**: multi-sweep retry, resume idempotent, tidak ada halaman hilang diam-diam
- **Multi-provider LLM**: OpenAI-compatible & Gemini-compatible, fallback berurutan (lihat Settings)

## Menjalankan

```bash
pip install -r requirements.txt
python app.py          # http://localhost:8091
# atau
docker compose up -d --build
```

### Keamanan (wajib untuk rilis)

```bash
FLASK_PASSWORD=password-anda python app.py
```

Tanpa `FLASK_PASSWORD`, dashboard berjalan **mode terbuka** (banner peringatan tampil di
UI). Dengan password: semua halaman wajib login, semua POST divalidasi **CSRF token**,
dan percobaan login di-rate-limit (5× salah → terkunci 30 detik).

### Testing

```bash
pip install pytest
pytest            # seluruh suite (config durability, auth, error handling, dst.)
```

### Deployment

**systemd** — unit siap pakai di [deploy/bebaa-translator.service](deploy/bebaa-translator.service):
sesuaikan `User`/`WorkingDirectory`, salin ke `/etc/systemd/system/`, lalu
`sudo systemctl enable --now bebaa-translator`.

**Docker** — `docker compose up -d --build`; volume `jobs/`, `uploads/`, dan `config/`
di-mount sehingga konfigurasi + API key aman saat rebuild image.

**Backup** — yang perlu dicadangkan: `config/` (API key + notifikasi) dan `jobs/`
(hasil terjemahan). Keduanya cukup untuk memulihkan layanan sepenuhnya.

Riwayat perubahan: [CHANGELOG.md](CHANGELOG.md). Lisensi: [LICENSE](LICENSE).

## Konfigurasi API (halaman ⚙️ Settings)

Config tersimpan di `config/providers.json` (dibuat otomatis). Provider default =
router bawaan `http://192.168.1.123:3005/v1` yang membaca key dari
`~/.hermes/.env`. Anda bebas menambah provider lain:

| Tipe | Cocok untuk | Contoh base URL |
|---|---|---|
| `openai` | OpenAI-compatible: LiteLLM, OpenRouter, vLLM, Ollama, Groq, router bawaan | `https://openrouter.ai/api/v1` |
| `gemini` | Google Gemini API | `https://generativelanguage.googleapis.com/v1beta` |

- **Model chain**: satu baris = satu model, dicoba berurutan sampai sukses.
- **Vision model** per provider: dipakai khusus request bergambar.
- 402/429 (kuota habis) → otomatis pindah kandidat berikutnya.
- Anti-truncation: respons terpotong (`finish_reason=length`) otomatis diulang
  dengan `max_tokens` lebih besar — teks terpotong tidak pernah disimpan ke cache.

## Kapan Vision OCR perlu dihidupkan? 🔍

Aktifkan checkbox **Vision OCR** saat upload jika:

1. **Buku hasil scan/foto** tanpa layer teks — `pdftotext` tidak bisa mengambil
   teksnya, sehingga OCR wajib. Vision jauh lebih akurat daripada tesseract
   untuk scan jadul.
2. **Scan kualitas jelek** — miring/skew, pudar, noda, perataan tidak konsisten.
   Runner sudah melakukan preprocessing (deskew + CLAHE + denoise) sebelum
   dikirim ke model vision.
3. **Halaman kaya ilustrasi** — peta, gambar teknik, lukisan. Vision mendeteksi
   posisi ilustrasi (persen Y), lalu runner memotong (smart-crop) dari render
   300 dpi dan menyematkannya kembali di EPUB dengan keterangan terjemahan.
   ≥2 ilustrasi di satu halaman → seluruh halaman disematkan sebagai gambar +
   terjemahan tetap ditulis di bawahnya.

**Tidak perlu** untuk PDF digital ber-teks (hasil Word/LaTeX) — vision hanya
menambah biaya & waktu. Tanpa vision pun buku scan tetap diproses via tesseract.

## Arsitektur

```
app.py        Flask dashboard: upload, progres realtime, cancel/retry/delete, Settings API
runner.py     Worker per job: ekstraksi teks → OCR → terjemahan → page_NNNN.html → EPUB (pandoc)
translate.py  Klien LLM multi-provider + fallback + anti-truncation + cache
config/       providers.json (API key & model chain)
jobs/<id>/    artefak per job (book.pdf, state.json, cache.sqlite, images/, terjemahan.epub)
```

Per-halaman: `pdftotext` (cepat) → jika <25 kata → render PNG 300dpi →
Vision OCR / tesseract → terjemahan (cache SQLite per job) → HTML atomik.

## Env vars penting

| Var | Default | Arti |
|---|---|---|
| `FLASK_PASSWORD` | (kosong = mode terbuka) | password login dashboard + mengaktifkan CSRF |
| `FLASK_SECRET` | acak persisten | kunci sesi cookie |
| `WORKERS` | 4 | thread LLM paralel per job |
| `API_CONCURRENCY` | 4 | batas global request API (semua job) |
| `MAX_TOKENS` | 8192 | max_tokens awal (auto digandakan jika terpotong) |
| `VISION_WIDTH` | 1280 | lebar gambar yang dikirim ke model vision |
| `USE_VISION` | 0 | 1 = vision OCR aktif (juga dari checkbox upload) |
| `SWEEPS` | 3 | maksimum pengulangan halaman gagal |
