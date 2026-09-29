#!/usr/bin/env python3
"""Bebaa Book Translator – Flask dashboard (mobile friendly).

Fitur:
- Upload PDF (buku bahasa apa saja) → selalu diterjemahkan ke Bahasa Indonesia
- Auto-detect bahasa sumber (Belanda/Inggris/Jerman/Perancis/…)
- Progres realtime, preview terjemahan per halaman, log error
- Download EPUB saat selesai
"""
VERSION = "v4.0-table-optimized"  # deteksi tabel/TOC → full-page image, skip translate
import os
import re
import json
import uuid
import threading
import subprocess
from pathlib import Path
from datetime import datetime

from flask import (Flask, request, render_template, jsonify,
                   send_from_directory, redirect, url_for, abort)
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET', 'book-translator')
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200 MB

BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / 'uploads'
JOBS_DIR = BASE_DIR / 'jobs'
UPLOAD_DIR.mkdir(exist_ok=True)
JOBS_DIR.mkdir(exist_ok=True)

# job_id -> Popen
procs = {}
# job_id -> status string
statuses = {}
# jumlah thread LLM paralel per job (dari form upload)
WORKERS_DEFAULT = int(os.getenv('WORKERS', '4'))


# ---------------------------------------------------------------- helpers
def job_dir(jid):
    return JOBS_DIR / jid


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


# ------------------------------------------------------- jalankan runner
def start_job(jid, pdf_path, batch, max_pages, workers, use_vision='0'):
    d = job_dir(jid)
    d.mkdir(parents=True, exist_ok=True)
    target = d / 'book.pdf'
    target.write_bytes(Path(pdf_path).read_bytes())

    total = count_pages(target)
    if max_pages and max_pages > 0:
        total = min(total, max_pages)

    meta = {
        'job_id': jid,
        'filename': Path(pdf_path).name,
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
        file.save(save_path)

        threading.Thread(target=start_job,
                         args=(jid, save_path, batch, maxp, workers, use_vision), daemon=True).start()
        return redirect(url_for('progress', job_id=jid))
    return render_template('index.html', VERSION=VERSION)


@app.route('/progress/<job_id>')
def progress(job_id):
    if not job_dir(job_id).exists():
        abort(404)
    return render_template('progress.html', job_id=job_id)


@app.route('/api/status/<job_id>')
def api_status(job_id):
    d = job_dir(job_id)
    if not d.exists():
        return jsonify({'error': 'job tidak ditemukan'}), 404

    meta = read_json(d / 'meta.json')
    state = read_json(d / 'state.json', {'last_page': 0})
    total = meta.get('total_pages', 0)
    last = state.get('last_page', 0)
    missing = state.get('missing_pages', [])
    done_pages = len(list(d.glob('page_*.html')))
    status = state.get('status') or statuses.get(job_id) or 'running'
    epub = d / 'terjemahan.epub'
    ready = epub.exists()
    # jika ada EPUB tetapi ada missing pages → tetap "error" agar bisa retry
    if missing and not (procs.get(job_id) or statuses.get(job_id) == 'running'):
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
            'filename': meta.get('filename', ''),
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
            'files': (d / 'terjemahan.epub').stat().st_size if ready else 0,
        })


@app.route('/download/<job_id>')
def download_epub(job_id):
    d = job_dir(job_id)
    ep = d / 'terjemahan.epub'
    if not ep.exists():
        return 'EPUB belum siap', 404
    name = read_json(d / 'meta.json').get('filename', 'buku')
    stem = re.sub(r'\.pdf$', '', name, flags=re.I)
    return send_from_directory(d, 'terjemahan.epub',
                               as_attachment=True,
                               download_name=f'{stem}_ID.epub')


@app.route('/api/jobs')
def api_jobs():
    out = []
    for d in sorted(JOBS_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        meta = read_json(d / 'meta.json')
        state = read_json(d / 'state.json', {})
        total = meta.get('total_pages', 0)
        # hitung halaman yang benar-benar selesai (sumber kebenaran)
        done_pages = len(list(d.glob('page_*.html')))
        epub_ready = (d / 'terjemahan.epub').exists()
        running = (d.name in procs) or (statuses.get(d.name) == 'running')
        missing = state.get('missing_pages') or []

        # tentukan status yang benar
        if running:
            status = 'running'
        elif missing:
            status = 'error'
        elif state.get('status') == 'cancelled':
            status = 'cancelled'
        elif total and done_pages >= total:
            status = 'done'
        elif epub_ready and not total:
            status = 'done'
        else:
            # tidak jalan, tidak error, tapi belum lengkap → anggap error/terhenti
            status = 'error'

        out.append({
            'id': d.name,
            'filename': meta.get('filename', ''),
            'status': status,
            # EPUB hanya "siap" jika job benar-benar selesai (bukan cuma pernah dibuat)
            'ready': epub_ready and status == 'done',
            'total_pages': total,
            'done_pages': done_pages,
            'last_page': done_pages,
            'started_at': meta.get('started_at', ''),
            'missing_pages': missing,
            'size_mb': round(_dir_size_mb(d), 1),
            'cache_mb': round((d / 'cache.sqlite').stat().st_size / 1048576, 2) if (d / 'cache.sqlite').exists() else 0,
        })
    return jsonify(out)


@app.route('/delete/<job_id>', methods=['POST'])
def delete_job(job_id):
    """Hapus job secara TOTAL: PDF, halaman HTML, cache sqlite, gambar, EPUB, log."""
    import shutil, threading
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
        jobs_root = Path(BASE_DIR / 'jobs').resolve()
        if jobs_root not in d.resolve().parents:
            return jsonify({'ok': False, 'msg': 'path tidak valid'}), 400
        shutil.rmtree(d)
        procs.pop(job_id, None)
        statuses.pop(job_id, None)
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
    d = job_dir(job_id)
    p = procs.get(job_id)
    # Fallback: use PID file (survives service restarts)
    if not p:
        pidfile = d / '.runner.pid'
        if pidfile.exists():
            try:
                pid = int(pidfile.read_text().strip())
                import os as _os
                p = type('obj', (object,), {'poll': lambda self: None, 'pid': pid})()
                p.pid = pid
                if _os.path.exists(f'/proc/{pid}'):
                    p.poll = lambda self: None  # still running
                else:
                    p.poll = lambda self: 0  # exited
            except (ValueError, ProcessLookupError):
                p = None

    if p:
        import os as _os
        try:
            if p.poll() is None:
                _os.kill(p.pid, 9)  # SIGKILL — runner SIGTERM handler is unreliable
                time.sleep(1)
        except (ProcessLookupError, PermissionError):
            pass
        statuses.pop(job_id, None)
        procs.pop(job_id, None)  # Hapus entry biar retry bisa jalan lagi
        (d / '.runner.pid').unlink(missing_ok=True)
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'msg': 'runner tidak ditemukan (bisa sudah selesai/matikan manual)'}), 404


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
        'BOOK_TITLE': re.sub(r'\.pdf$', '', meta.get('filename', 'Buku'), flags=re.I),
        'USE_VISION': str(meta.get('use_vision', '0')),
    })
    procs[jid] = subprocess.Popen(
        [str(BASE_DIR / '.venv/bin/python'), str(BASE_DIR / 'runner.py')],
        env=env, stdout=logf, stderr=subprocess.STDOUT, cwd=str(BASE_DIR))
    statuses[jid] = 'running'
    st = read_json(d / 'state.json', {})
    st['status'] = 'running'
    (d / 'state.json').write_text(json.dumps(st))
    return procs[jid], logf


@app.route('/retry/<job_id>', methods=['POST'])
def retry_job(job_id):
    """Lanjutkan job: runner melewati halaman yang sudah selesai (idempotent)."""
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
    # hapus error.log lama agar UI bersih
    ep = d / 'error.log'
    if ep.exists():
        ep.unlink()
    proc, logf = _spawn_runner(job_id)
    threading.Thread(target=_wait_job, args=(job_id, proc, logf), daemon=True).start()
    return jsonify({'ok': True})


def _wait_job(jid, proc, logf):
    try:
        proc.wait()
    finally:
        logf.close()
        try:
            st = read_json(job_dir(jid) / 'state.json', {})
            ready = (job_dir(jid) / 'terjemahan.epub').exists()
            missing = st.get('missing_pages') or []
            if missing:
                statuses[jid] = 'error'
            elif ready:
                statuses[jid] = 'done'
            else:
                statuses[jid] = 'error'
            st['status'] = statuses[jid]
            (job_dir(jid) / 'state.json').write_text(json.dumps(st))
        except Exception:
            pass
        procs.pop(jid, None)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8091, debug=False, threaded=True)
