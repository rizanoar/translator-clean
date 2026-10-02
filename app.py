#!/usr/bin/env python3
"""Bebaa Book Translator – Flask dashboard (mobile friendly).

Fitur:
- Upload PDF (buku bahasa apa saja) → selalu diterjemahkan ke Bahasa Indonesia
- Auto-detect bahasa sumber (Belanda/Inggris/Jerman/Perancis/…)
- Progres realtime, preview terjemahan per halaman, log error
- Download EPUB saat selesai
- Settings: kelola provider API (OpenAI-compatible / Gemini-compatible)
"""
VERSION = "v5.1"
import os
import re
import sys
import json
import uuid
import time
import hmac
import secrets
import threading
import subprocess
import traceback
import urllib.request
from pathlib import Path
from datetime import datetime

from flask import (Flask, request, render_template, jsonify, session,
                   send_from_directory, redirect, url_for, abort)
from werkzeug.utils import secure_filename

from translate import (load_config, save_config, get_providers,
                       resolve_key, fetch_remote_models)

def _secret_key():
    """Kunci sesi: env FLASK_SECRET → file config/.secret_key (0600).
    Kunci persisten agar sesi login tidak hangus tiap restart."""
    env = os.getenv('FLASK_SECRET')
    if env:
        return env
    f = BASE_DIR / 'config' / '.secret_key'
    try:
        if f.exists():
            key = f.read_text().strip()
            if key:
                return key
        f.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_hex(32)
        f.write_text(key)
        try:
            os.chmod(f, 0o600)
        except OSError:
            pass
        return key
    except OSError:
        return secrets.token_hex(32)   # fallback: acak per proses


BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / 'uploads'
JOBS_DIR = BASE_DIR / 'jobs'
UPLOAD_DIR.mkdir(exist_ok=True)
JOBS_DIR.mkdir(exist_ok=True)

app = Flask(__name__, static_folder='static', static_url_path='/static')
app.config['SECRET_KEY'] = _secret_key()
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200 MB

# job_id -> Popen
procs = {}
# job_id -> status string
statuses = {}
# jumlah thread LLM paralel per job (dari form upload)
WORKERS_DEFAULT = int(os.getenv('WORKERS', '4'))

_UUID_PREFIX = re.compile(r'^[0-9a-f]{32}_')


# ---------------------------------------------------------------- helpers
def job_dir(jid):
    return JOBS_DIR / jid


def valid_job_id(jid):
    """job_id hanya hex 32 (hasil uuid4) → cegah path traversal."""
    return bool(re.fullmatch(r'[0-9a-f]{32}', str(jid)))


def _runner_alive(jid):
    """Runner job masih hidup? (dict procs ATAU PID file — tahan restart)."""
    p = procs.get(jid)
    if p and p.poll() is None:
        return True
    try:
        pid = int((job_dir(jid) / '.runner.pid').read_text().strip())
        return Path(f'/proc/{pid}').exists()
    except Exception:
        return False


def clean_book_name(fname):
    """Nama buku bersih: tanpa .pdf, tanpa prefix UUID/hash, separator rapi.
    'Sriwijaya_--_Prof__Dr__X_--_be6aa077..._--_Annas_Archive'
      → 'Sriwijaya — Prof Dr X — Annas Archive'"""
    stem = _UUID_PREFIX.sub('', re.sub(r'\.pdf$', '', fname or '', flags=re.I))
    # token hex panjang (hash Annas Archive / checksum) dibuang
    stem = re.sub(r'(?<![0-9a-fA-F])[0-9a-fA-F]{32,}(?![0-9a-fA-F])', '', stem)
    stem = stem.replace('_', ' ')
    stem = re.sub(r'\s*--+\s*', ' — ', stem)      # '--' → em dash
    stem = re.sub(r'(?:—\s*){2,}', '— ', stem)    # dash ganda → satu
    stem = re.sub(r'\s{2,}', ' ', stem).strip(' —-')
    return stem or 'Buku'


def read_json(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default if default is not None else {}


def tail_text(p, limit=200):
    """Ambil sebagian teks (untuk preview / log)."""
    try:
        t = Path(p).read_text(errors='replace')
    except Exception:
        return ''
    return t[:limit]


def page_preview(jid, page):
    """Ambil teks terjemahan untuk satu halaman (dari page_NNNN.html)."""
    d = job_dir(jid)
    for cand in (d / f'page_{page:04d}.html', d / f'page_{page}.html'):
        if cand.exists():
            html = cand.read_text(errors='replace')
            html = re.sub(r'<img[^>]*>', '[ILUSTRASI]', html)
            txt = re.sub(r'<[^>]+>', '\n', html)
            txt = re.sub(r'\n{2,}', '\n', txt).strip()
            return txt
    return ''


def count_pages(pdf_path):
    """Hitung halaman PDF: pypdf dulu, fallback pdfinfo."""
    try:
        import pypdf
        with open(pdf_path, 'rb') as fh:
            return len(pypdf.PdfReader(fh).pages)
    except Exception:
        try:
            out = subprocess.run(['pdfinfo', str(pdf_path)],
                                 capture_output=True, text=True, timeout=60).stdout
            for line in out.splitlines():
                if line.startswith('Pages:'):
                    return int(line.split(':')[1].strip())
        except Exception:
            pass
    return 0


def _file_size_mb(p):
    """Ukuran file (MB) — 0 jika file hilang/rusak (tahan race)."""
    try:
        return p.stat().st_size
    except OSError:
        return 0


# ------------------------------------------------------- jalankan runner
def _mark_job_error(jid, msg):
    """Tandai job error secara eksplisit — thread tidak boleh mati diam-diam
    (sebelumnya: exception di thread → UI stuck 'running' selamanya)."""
    d = job_dir(jid)
    try:
        d.mkdir(parents=True, exist_ok=True)
        with open(d / 'error.log', 'a') as f:
            f.write(f'{msg}\n' + traceback.format_exc() + '\n')
        st = read_json(d / 'state.json', {})
        st['status'] = 'error'
        st['updated_at'] = datetime.now().isoformat(timespec='seconds')
        (d / 'state.json').write_text(json.dumps(st))
    except Exception:
        pass
    statuses[jid] = 'error'
    procs.pop(jid, None)
    print(f'[job {jid[:8]}] {msg}', file=sys.stderr, flush=True)


def start_job(jid, pdf_path, batch, max_pages, workers, use_vision='0'):
    d = job_dir(jid)
    try:
        d.mkdir(parents=True, exist_ok=True)
        target = d / 'book.pdf'
        try:
            target.write_bytes(Path(pdf_path).read_bytes())
        except OSError as e:
            raise RuntimeError(f'PDF upload tidak bisa dibaca/dipindah: {e}')
        # upload sementara sudah tersalin ke job dir → hapus agar tidak bocor disk
        try:
            Path(pdf_path).unlink(missing_ok=True)
        except OSError:
            pass

        total = count_pages(target)
        if max_pages and max_pages > 0:
            total = min(total, max_pages)

        meta = {
            'job_id': jid,
            'filename': Path(pdf_path).name,
            'title': clean_book_name(Path(pdf_path).name),   # nama bersih tanpa UUID
            'total_pages': total,
            'batch': batch,
            'workers': workers,
            'use_vision': use_vision,
            'started_at': datetime.now().isoformat(timespec='seconds'),
        }
        (d / 'meta.json').write_text(json.dumps(meta))
        (d / 'state.json').write_text(json.dumps({'last_page': 0, 'status': 'running'}))

        # pakai helper bersama agar start & retry identik (idempotent)
        proc, logf = _spawn_runner(jid, batch)
        threading.Thread(target=_wait_job, args=(jid, proc, logf), daemon=True).start()
    except Exception as e:
        _mark_job_error(jid, f'Gagal memulai job: {e}')


# ------------------------------------------------------------------ routes
@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        file = request.files.get('pdf')
        if not file or not file.filename:
            return render_template('index.html', error='Pilih file PDF dulu.', VERSION=VERSION)
        if not file.filename.lower().endswith('.pdf'):
            return render_template('index.html', error='File harus berformat PDF.', VERSION=VERSION)

        batch = request.form.get('batch', type=int) or 5
        batch = max(1, min(batch, 20))
        maxp = request.form.get('max_pages', type=int) or 0
        workers = request.form.get('workers', type=int) or WORKERS_DEFAULT
        workers = max(1, min(workers, 8))
        use_vision = '1' if request.form.get('use_vision') else '0'

        jid = uuid.uuid4().hex
        fname = secure_filename(file.filename) or 'book.pdf'
        save_path = UPLOAD_DIR / f'{jid}_{fname}'
        try:
            file.save(save_path)
        except OSError as e:
            # disk penuh / izin — jangan biarkan 500 HTML mentah
            return render_template('index.html',
                                   error=f'Gagal menyimpan file upload: {e}',
                                   VERSION=VERSION), 500

        threading.Thread(target=start_job,
                         args=(jid, save_path, batch, maxp, workers, use_vision), daemon=True).start()
        return redirect(url_for('progress', job_id=jid))
    return render_template('index.html', VERSION=VERSION)


@app.route('/progress/<job_id>')
def progress(job_id):
    if not valid_job_id(job_id) or not job_dir(job_id).exists():
        abort(404)
    return render_template('progress.html', job_id=job_id)


@app.route('/api/status/<job_id>')
def api_status(job_id):
    if not valid_job_id(job_id):
        return jsonify({'error': 'job tidak ditemukan'}), 404
    d = job_dir(job_id)
    if not d.exists():
        return jsonify({'error': 'job tidak ditemukan'}), 404

    meta = read_json(d / 'meta.json')
    state = read_json(d / 'state.json', {'last_page': 0})
    total = meta.get('total_pages', 0)
    last = state.get('last_page', 0)
    # missing hanya dihitung dalam rentang halaman riil (buang phantom lama)
    missing = [p for p in (state.get('missing_pages') or []) if not total or p <= total]
    done_pages = len(list(d.glob('page_*.html')))
    status = state.get('status') or statuses.get(job_id) or 'running'
    epub = d / 'terjemahan.epub'
    ready = epub.exists()
    # jika ada EPUB tetapi ada missing pages → tetap "error" agar bisa retry
    if missing and not (_runner_alive(job_id) or statuses.get(job_id) == 'running'):
        status = 'error'

    # log aktivitas terakhir
    log_lines = []
    logp = d / 'runner.log'
    if logp.exists():
        try:
            log_lines = logp.read_text(errors='replace').splitlines()[-8:]
        except Exception:
            pass

    # error
    err = ''
    errp = d / 'error.log'
    if errp.exists():
        err = tail_text(errp, 1500)

    # preview: 3 halaman terakhir yang sudah diterjemahkan
    previews = []
    for p in range(max(last - 2, 1), last + 1):
        t = page_preview(job_id, p)
        if t:
            previews.append({'page': p, 'text': t[:1200]})

    return jsonify({
        'job_id': job_id,
        'filename': meta.get('title') or meta.get('filename', ''),
        'total_pages': total,
        'done_pages': done_pages,
        'last_page': last,
        'percent': round(done_pages / total * 100, 1) if total else 0,
        'status': status,
        'ready': ready,
        'missing_pages': missing,
        'epub_url': url_for('download_epub', job_id=job_id) if ready else None,
        'log': log_lines,
        'error': err,
        'previews': previews,
        'files': _file_size_mb(epub) if ready else 0,
    })


@app.route('/download/<job_id>')
def download_epub(job_id):
    if not valid_job_id(job_id):
        return 'job tidak ditemukan', 404
    d = job_dir(job_id)
    ep = d / 'terjemahan.epub'
    if not ep.exists():
        return 'EPUB belum siap', 404
    meta = read_json(d / 'meta.json')
    stem = clean_book_name(meta.get('title') or meta.get('filename', 'buku'))
    try:
        return send_from_directory(d, 'terjemahan.epub',
                                   as_attachment=True,
                                   download_name=f'{stem}_ID.epub')
    except OSError as e:
        # EPUB hilang di antara cek exists() dan kirim (race/disk)
        return f'Gagal mengirim EPUB: {e}', 500


@app.route('/api/jobs')
def api_jobs():
    out = []
    try:
        dirs = sorted(JOBS_DIR.iterdir(),
                      key=lambda x: x.stat().st_mtime, reverse=True)
    except OSError:
        dirs = list(JOBS_DIR.iterdir())
    for d in dirs:
        if not d.is_dir():
            continue
        meta = read_json(d / 'meta.json')
        state = read_json(d / 'state.json', {})
        total = meta.get('total_pages', 0)
        # hitung halaman yang benar-benar selesai (sumber kebenaran)
        done_pages = len(list(d.glob('page_*.html')))
        epub_ready = (d / 'terjemahan.epub').exists()
        running = _runner_alive(d.name) or (statuses.get(d.name) == 'running')
        # phantom pages (di luar total halaman riil) diabaikan
        missing = [p for p in (state.get('missing_pages') or [])
                   if not total or p <= total]

        # tentukan status yang benar
        if running:
            status = 'running'
        elif state.get('status') == 'cancelled':
            status = 'cancelled'
        elif total and done_pages >= total:
            status = 'done'
        elif epub_ready and not total:
            status = 'done'
        elif missing:
            status = 'error'
        else:
            # tidak jalan, tidak error, tapi belum lengkap → anggap error/terhenti
            status = 'error'

        out.append({
            'id': d.name,
            'filename': meta.get('title') or meta.get('filename', ''),
            'status': status,
            # EPUB hanya "siap" jika job benar-benar selesai (bukan cuma pernah dibuat)
            'ready': epub_ready and status == 'done',
            'total_pages': total,
            'done_pages': done_pages,
            'last_page': done_pages,
            'started_at': (meta.get('started_at', '') or '').replace('T', ' '),
            'missing_pages': missing,
            'size_mb': round(_dir_size_mb(d), 1),
            'cache_mb': round(_file_size_mb(d / 'cache.sqlite') / 1048576, 2),
        })
    return jsonify(out)


@app.route('/delete/<job_id>', methods=['POST'])
def delete_job(job_id):
    """Hapus job secara TOTAL: PDF, halaman HTML, cache sqlite, gambar, EPUB, log."""
    import shutil
    if not valid_job_id(job_id):
        return jsonify({'ok': False, 'msg': 'job tidak ditemukan'}), 404
    d = job_dir(job_id)
    if not d.exists():
        return jsonify({'ok': False, 'msg': 'job tidak ditemukan'}), 404
    # Jangan hapus job yang sedang berjalan — harus di-cancel dulu
    p = procs.get(job_id)
    if p and p.poll() is None:
        return jsonify({'ok': False, 'msg': 'job masih berjalan — batalkan (cancel) dulu'}), 409
    try:
        size_before = _dir_size_mb(d)
        # pastikan tidak menghapus direktori di luar JOBS_DIR
        jobs_root = JOBS_DIR.resolve()
        if jobs_root not in d.resolve().parents:
            return jsonify({'ok': False, 'msg': 'path tidak valid'}), 400
        shutil.rmtree(d)
        procs.pop(job_id, None)
        statuses.pop(job_id, None)
        # bersihkan juga sisa upload job ini
        for f in UPLOAD_DIR.glob(f'{job_id}_*'):
            f.unlink(missing_ok=True)
        return jsonify({'ok': True, 'freed_mb': round(size_before, 1), 'deleted': d.name})
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 500


@app.route('/jobs')
def jobs_page():
    return render_template('jobs.html')


def _dir_size_mb(p):
    """Total ukuran direktori (MB)."""
    total = 0
    for f in p.rglob('*'):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total / 1048576


@app.route('/cancel/<job_id>', methods=['POST'])
def cancel_job(job_id):
    """Cancel job — check procs dict OR fallback to PID file."""
    if not valid_job_id(job_id):
        return jsonify({'ok': False, 'msg': 'job tidak ditemukan'}), 404
    d = job_dir(job_id)
    if not d.exists():
        return jsonify({'ok': False, 'msg': 'job tidak ditemukan'}), 404

    pid = None
    p = procs.get(job_id)
    if p and p.poll() is None:
        pid = p.pid
    else:
        # Fallback: PID file (bertahan setelah restart service)
        pidfile = d / '.runner.pid'
        if pidfile.exists():
            try:
                cand = int(pidfile.read_text().strip())
                if Path(f'/proc/{cand}').exists():
                    pid = cand
            except (ValueError, OSError):
                pass

    if pid:
        try:
            os.kill(pid, 9)  # SIGKILL — runner SIGTERM handler tidak reliable
            time.sleep(1)
        except (ProcessLookupError, PermissionError):
            pass
        statuses.pop(job_id, None)
        procs.pop(job_id, None)  # Hapus entry biar retry bisa jalan lagi
        (d / '.runner.pid').unlink(missing_ok=True)
        # tandai state cancelled agar UI konsisten (runner SIGKILL tak bisa menulis)
        try:
            st = read_json(d / 'state.json', {})
            if st:
                st['status'] = 'cancelled'
                (d / 'state.json').write_text(json.dumps(st))
        except Exception:
            pass
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'msg': 'runner tidak ditemukan (bisa sudah selesai)'}), 404


def _spawn_runner(jid, batch=None):
    """Jalankan ulang runner.py untuk job (dipakai pertama kali & retry).
    Runner bersifat idempotent: halaman yang sudah ada di-skip."""
    d = job_dir(jid)
    meta = read_json(d / 'meta.json', {})
    total = meta.get('total_pages', 0)
    b = batch or meta.get('batch', 10)
    logf = open(d / 'runner.log', 'a')
    env = os.environ.copy()
    env.update({
        'PDF': str(d / 'book.pdf'),
        'WORK_DIR': str(d),
        'EPUB_PATH': str(d / 'terjemahan.epub'),
        'MAX_PAGE': str(total),
        'BATCH_SIZE': str(b),
        'WORKERS': str(meta.get('workers', 4)),
        'BOOK_TITLE': clean_book_name(meta.get('title') or meta.get('filename', 'Buku')),
        'USE_VISION': str(meta.get('use_vision', '0')),
    })
    # sys.executable: jalan benar di venv host maupun di dalam container Docker
    try:
        procs[jid] = subprocess.Popen(
            [sys.executable, str(BASE_DIR / 'runner.py')],
            env=env, stdout=logf, stderr=subprocess.STDOUT, cwd=str(BASE_DIR))
    except Exception as e:
        logf.close()
        raise RuntimeError(f'gagal menjalankan runner ({sys.executable}): {e}')
    statuses[jid] = 'running'
    st = read_json(d / 'state.json', {})
    st['status'] = 'running'
    (d / 'state.json').write_text(json.dumps(st))
    return procs[jid], logf


@app.route('/retry/<job_id>', methods=['POST'])
def retry_job(job_id):
    """Lanjutkan job: runner melewati halaman yang sudah selesai (idempotent)."""
    if not valid_job_id(job_id):
        return jsonify({'ok': False, 'msg': 'job tidak ditemukan'}), 404
    d = job_dir(job_id)
    if not d.exists():
        return jsonify({'ok': False, 'msg': 'job tidak ditemukan'}), 404
    # Tolak hanya jika runner BENAR-BENAR masih hidup (dict procs ATAU PID file)
    if procs.get(job_id):
        return jsonify({'ok': False, 'msg': 'job masih berjalan'}), 409
    pidfile = d / '.runner.pid'
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text().strip())
            if Path(f'/proc/{pid}').exists():
                return jsonify({'ok': False, 'msg': 'runner masih hidup (PID file)'}), 409
        except (ValueError, OSError):
            pass
        pidfile.unlink(missing_ok=True)  # stale
    # lock anti double-retry (atomik: O_CREAT|O_EXCL)
    lock = d / '.retry.lock'
    if lock.exists():
        try:
            if time.time() - lock.stat().st_mtime < 60:
                return jsonify({'ok': False, 'msg': 'retry sedang berjalan'}), 409
        except OSError:
            pass
        lock.unlink(missing_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        return jsonify({'ok': False, 'msg': 'retry sedang berjalan'}), 409
    try:
        # hapus error.log lama agar UI bersih
        ep = d / 'error.log'
        if ep.exists():
            ep.unlink()
        proc, logf = _spawn_runner(job_id)
        threading.Thread(target=_wait_job, args=(job_id, proc, logf), daemon=True).start()
    except Exception as e:
        _mark_job_error(job_id, f'Gagal retry: {e}')
        return jsonify({'ok': False,
                        'msg': f'gagal memulai runner: {e} — cek error.log job'}), 500
    finally:
        lock.unlink(missing_ok=True)
    return jsonify({'ok': True})


def _wait_job(jid, proc, logf):
    try:
        proc.wait()
    finally:
        logf.close()
        try:
            st = read_json(job_dir(jid) / 'state.json', {})
            ready = (job_dir(jid) / 'terjemahan.epub').exists()
            total = read_json(job_dir(jid) / 'meta.json', {}).get('total_pages', 0)
            missing = [p for p in (st.get('missing_pages') or [])
                       if not total or p <= total]
            if missing:
                statuses[jid] = 'error'
            elif ready:
                statuses[jid] = 'done'
            else:
                statuses[jid] = 'error'
            st['status'] = statuses[jid]
            st['missing_pages'] = missing
            (job_dir(jid) / 'state.json').write_text(json.dumps(st))
        except Exception:
            pass
        ev = {'done': '✅ Terjemahan selesai',
              'error': '⚠️ Terjemahan gagal sebagian'}.get(statuses.get(jid))
        if ev:
            _notify(jid, ev)   # best-effort; tidak memengaruhi job
        procs.pop(jid, None)


# ---------------------------------------------------------------- settings
@app.route('/settings')
def settings_page():
    return render_template('settings.html')


@app.route('/api/providers', methods=['GET'])
def api_providers_get():
    """Daftar provider (API key dimasking demi keamanan)."""
    cfg = load_config()
    out = []
    for p in cfg.get('providers', []):
        q = {k: v for k, v in p.items() if k != 'api_key'}
        key = (p.get('api_key') or '').strip()
        q['has_config_key'] = bool(key)
        q['key_hint'] = (key[:3] + '••••' + key[-4:]) if len(key) > 8 else ('••••' if key else '')
        q['has_env_key'] = bool(resolve_key({**p, 'api_key': ''}))
        out.append(q)
    return jsonify({'providers': out})


@app.route('/api/providers', methods=['POST'])
def api_providers_set():
    """Simpan konfigurasi provider.
    api_key == '__KEEP__' → pertahankan key lama (UI tidak menampilkan secret)."""
    data = request.get_json(force=True, silent=True) or {}
    newps = data.get('providers')
    if not isinstance(newps, list) or not newps:
        return jsonify({'ok': False, 'msg': 'providers minimal 1'}), 400
    old = {(p.get('name'), p.get('base_url')): p for p in load_config().get('providers', [])}
    for p in newps:
        if isinstance(p, dict) and p.get('api_key') == '__KEEP__':
            # UI tidak menampilkan secret → pertahankan key lama
            p['api_key'] = old.get((p.get('name'), p.get('base_url')), {}).get('api_key', '')
    try:
        save_config({'providers': newps,
                     'notify': load_config().get('notify', {})})
    except ValueError as e:
        return jsonify({'ok': False, 'msg': str(e)}), 400
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 500
    return jsonify({'ok': True})


@app.route('/api/providers/test', methods=['POST'])
def api_providers_test():
    """Tes satu provider: coba fetch daftar model."""
    data = request.get_json(force=True, silent=True) or {}
    p = data.get('provider') or {}
    try:
        models = fetch_remote_models(p, timeout=8)
        return jsonify({'ok': True, 'model_count': len(models),
                        'sample': models[:8]})
    except Exception as e:
        return jsonify({'ok': False, 'msg': f'{type(e).__name__}: {e}'}), 200


@app.route('/api/providers/models', methods=['POST'])
def api_provider_models():
    """Daftar model remote untuk autocomplete di Settings."""
    data = request.get_json(force=True, silent=True) or {}
    p = data.get('provider') or {}
    try:
        return jsonify({'ok': True, 'models': fetch_remote_models(p, timeout=8)})
    except Exception as e:
        return jsonify({'ok': False, 'models': [], 'msg': str(e)})


# ------------------------------------------------------------ error handlers
# API selalu jawab JSON; halaman web dapat pesan yang ramah — bukan traceback.
@app.errorhandler(413)
def too_large(e):
    msg = 'File terlalu besar (maks 200 MB).'
    if request.path.startswith('/api/'):
        return jsonify({'ok': False, 'error': msg}), 413
    return render_template('index.html', error=msg, VERSION=VERSION), 413


@app.errorhandler(404)
def not_found(e):
    if request.path.startswith('/api/'):
        return jsonify({'ok': False, 'error': 'endpoint tidak ditemukan'}), 404
    return render_template('index.html',
                           error='Halaman tidak ditemukan.', VERSION=VERSION), 404


@app.errorhandler(Exception)
def unhandled_error(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e   # biarkan kode HTTP standar jalan seperti biasa
    print(f'[UNHANDLED] {request.path}: {type(e).__name__}: {e}', flush=True)
    traceback.print_exc()
    if request.path.startswith('/api/'):
        return jsonify({'ok': False,
                        'error': f'kesalahan internal: {type(e).__name__}: {e}'}), 500
    return render_template('index.html',
                           error=f'Terjadi kesalahan internal: {type(e).__name__}: {e}',
                           VERSION=VERSION), 500


# ------------------------------------------------------------ auth & CSRF
# Aktif jika env FLASK_PASSWORD diisi. Tanpa password → mode terbuka
# (banner peringatan tampil di UI). CSRF diaktifkan bersamaan dengan auth.
def _auth_password():
    return (os.getenv('FLASK_PASSWORD') or '').strip() or None


_login_fails = {}   # ip -> (jumlah gagal, terkunci sampai timestamp)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if not _auth_password():
        return redirect('/')
    if 'csrf' not in session:
        session['csrf'] = secrets.token_hex(16)
    nxt = request.values.get('next', '/')
    if not nxt.startswith('/'):
        nxt = '/'

    if request.method == 'POST':
        ip = request.remote_addr or '?'
        fails, locked_until = _login_fails.get(ip, (0, 0.0))
        now = time.time()
        if now < locked_until:
            return render_template('login.html',
                                   error=f'Terlalu banyak percobaan — coba lagi '
                                         f'{int(locked_until - now)} detik.',
                                   csrf=session['csrf'], next_url=nxt), 429
        if not hmac.compare_digest(request.form.get('csrf_token', ''),
                                   session['csrf']):
            return render_template('login.html',
                                   error='Sesi kedaluwarsa — muat ulang halaman.',
                                   csrf=session['csrf'], next_url=nxt), 403
        if hmac.compare_digest(request.form.get('password', ''),
                               _auth_password()):
            session['auth'] = True
            _login_fails.pop(ip, None)
            return redirect(nxt)
        fails += 1
        lock = 30 if fails >= 5 else 0
        _login_fails[ip] = (fails, now + lock)
        msg = 'Terlalu banyak percobaan salah — terkunci 30 detik.' if lock \
            else f'Password salah ({fails}×).'
        return render_template('login.html', error=msg,
                               csrf=session['csrf'], next_url=nxt), 401

    if session.get('auth'):
        return redirect('/')
    return render_template('login.html', error=None,
                           csrf=session['csrf'], next_url=nxt)


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


@app.before_request
def _auth_and_csrf():
    password = _auth_password()
    path = request.path
    if password and not path.startswith('/static') and path != '/login' \
            and not session.get('auth'):
        is_fetch = path.startswith('/api/') or \
            request.headers.get('X-Requested-With') == 'fetch'
        if is_fetch:
            return jsonify({'ok': False, 'error': 'belum login'}), 401
        return redirect(url_for('login', next=path))
    if password and request.method == 'POST' and path != '/login':
        token = request.headers.get('X-CSRF-Token') or \
            request.form.get('csrf_token') or ''
        good = session.get('csrf') or ''
        if not good or not hmac.compare_digest(token, good):
            return jsonify({'ok': False,
                            'error': 'CSRF token tidak valid — muat ulang halaman'}), 403
    return None


@app.context_processor
def _inject_globals():
    if 'csrf' not in session:
        session['csrf'] = secrets.token_hex(16)
    return {'CSRF': session.get('csrf', ''), 'AUTH_ON': bool(_auth_password())}


# ------------------------------------------------------------ notifikasi
# Dikonfigurasi di Settings (config/providers.json → kunci "notify").
# Kegagalan notifikasi TIDAK PERNAH memengaruhi job.
def _notify_send(cfg, text):
    """Kirim notifikasi sesuai konfigurasi; raise jika tidak ada/gagal."""
    n = cfg.get('notify') or {}
    token = (n.get('telegram_token') or '').strip()
    chat = (n.get('telegram_chat') or '').strip()
    sent = False
    if token and chat:
        req = urllib.request.Request(
            f'https://api.telegram.org/bot{token}/sendMessage',
            data=json.dumps({'chat_id': chat, 'text': text}).encode(),
            headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=8).read()
        sent = True
    hook = (n.get('webhook_url') or '').strip()
    if hook:
        req = urllib.request.Request(
            hook, data=json.dumps({'text': text, 'ts': time.time()}).encode(),
            headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=8).read()
        sent = True
    if not sent:
        raise RuntimeError('belum ada Telegram/webhook yang dikonfigurasi')
    return sent


def _notify(job_id, event):
    """Best-effort notifikasi hasil job."""
    try:
        meta = read_json(job_dir(job_id) / 'meta.json', {})
        title = meta.get('title') or meta.get('filename', 'Buku')
        _notify_send(load_config(), f'{event}\n📖 {title}')
    except Exception as e:
        print(f'[notify] gagal kirim: {e}', flush=True)


@app.route('/api/notify', methods=['GET'])
def api_notify_get():
    n = (load_config().get('notify') or {})
    tok = n.get('telegram_token') or ''
    return jsonify({
        'telegram_chat': n.get('telegram_chat', ''),
        'telegram_token_set': bool(tok),
        'telegram_token_hint': (tok[:3] + '••••' + tok[-4:]) if len(tok) > 8
                               else ('••••' if tok else ''),
        'webhook_url': n.get('webhook_url', ''),
    })


@app.route('/api/notify', methods=['POST'])
def api_notify_set():
    data = request.get_json(force=True, silent=True) or {}
    cfg = load_config()
    n = dict(cfg.get('notify') or {})
    tok = (data.get('telegram_token') or '').strip()
    if tok == '__KEEP__':
        tok = n.get('telegram_token', '')
    n['telegram_token'] = tok
    n['telegram_chat'] = (data.get('telegram_chat') or '').strip()
    n['webhook_url'] = (data.get('webhook_url') or '').strip()
    try:
        save_config({'providers': cfg.get('providers', []), 'notify': n})
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 400
    return jsonify({'ok': True})


@app.route('/api/notify/test', methods=['POST'])
def api_notify_test():
    try:
        _notify_send(load_config(), '🔔 Tes notifikasi dari Bebaa Book Translator')
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8091, debug=False, threaded=True)
