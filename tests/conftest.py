"""Konfigurasi bersama suite pytest Bebaa Book Translator.

- Root proyek masuk sys.path agar `import app` / `import translate` jalan.
- PROVIDERS_CONFIG diarahkan ke file sementara (config asli tidak disentuh).
"""
import os
import sys
import pathlib
import tempfile
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# config terpisah per sesi pytest — config asli pengguna TIDAK disentuh
os.environ.setdefault(
    'PROVIDERS_CONFIG',
    str(pathlib.Path(tempfile.mkdtemp(prefix='bebaa-pytest-')) / 'providers.json'))


@pytest.fixture
def tmp_config(monkeypatch):
    """Arahkan translate.CONFIG_PATH ke file temporer bersih per tes."""
    import translate as T
    p = pathlib.Path(tempfile.mkdtemp(prefix='bebaa-cfg-')) / 'providers.json'
    monkeypatch.setattr(T, 'CONFIG_PATH', p)
    monkeypatch.setattr(T, '_cfg_cache', {'mtime': None, 'data': None})
    yield p


def load_runner_block(start, end, ns_extra):
    """Muat potongan runner.py (fungsi murni) tanpa import module penuh —
    runner menuntut env PDF/WORK_DIR di level import, jadi fungsi murni
    diuji lewat ekstraksi blok sumber."""
    src = (ROOT / 'runner.py').read_text()
    seg = src[src.index(start):src.index(end)]
    ns = dict(ns_extra)
    exec(compile(seg, '<runner-extract>', 'exec'), ns)
    return ns
