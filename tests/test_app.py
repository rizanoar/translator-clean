"""Tes Flask app: routes, error handler, auth/CSRF, job lifecycle."""
import json
import re
import shutil
import uuid

import pytest

import app as A
import translate as T


@pytest.fixture()
def client():
    A.app.config['TESTING'] = True
    with A.app.test_client() as c:
        yield c


# ------------------------------------------------ routes dasar
def test_halaman_utama_ok(client):
    for url in ('/', '/jobs', '/settings'):
        assert client.get(url).status_code == 200


def test_404_api_json_html_ramah(client):
    r = client.get('/api/tidak-ada')
    assert r.status_code == 404 and r.get_json()['ok'] is False
    r = client.get('/halaman-tidak-ada')
    assert r.status_code == 404 and b'Halaman tidak ditemukan' in r.data


def test_job_id_validasi_path_traversal(client):
    r = client.post('/delete/ZZZZ', content_type='application/json')
    assert r.status_code == 404


# ------------------------------------------------ job lifecycle
def test_start_job_gagal_menandai_error(client):
    jid = uuid.uuid4().hex
    try:
        A.start_job(jid, '/nonexistent/x.pdf', 5, 0, 4, '0')
        jd = A.JOBS_DIR / jid
        st = json.loads((jd / 'state.json').read_text())
        assert st['status'] == 'error'
        assert (jd / 'error.log').exists()
    finally:
        shutil.rmtree(A.JOBS_DIR / jid, ignore_errors=True)


def test_cancel_tanpa_runner_404_bersih(client):
    jid = uuid.uuid4().hex
    (A.JOBS_DIR / jid).mkdir(parents=True, exist_ok=True)
    try:
        r = client.post(f'/cancel/{jid}')
        assert r.status_code == 404 and r.get_json()['ok'] is False
    finally:
        shutil.rmtree(A.JOBS_DIR / jid, ignore_errors=True)


def test_job_dir_rusak_tidak_merusak_listing(client):
    jid = uuid.uuid4().hex
    (A.JOBS_DIR / jid).mkdir(parents=True, exist_ok=True)
    try:
        r = client.get('/api/jobs')
        assert r.status_code == 200
        entry = [x for x in r.get_json() if x['id'] == jid][0]
        assert entry['status'] == 'error'
    finally:
        shutil.rmtree(A.JOBS_DIR / jid, ignore_errors=True)


# ------------------------------------------------ providers
def test_providers_key_dimasking(client):
    d = client.get('/api/providers').get_json()
    assert 'api_key' not in d['providers'][0]
    assert 'has_config_key' in d['providers'][0]


def test_keep_key_tidak_hilang(client, tmp_config):
    T.save_config({'providers': [{'name': 'A', 'base_url': 'https://a.example',
                                  'api_key': 'sk-asli', 'models': ['m']}]})
    p = {'name': 'A', 'base_url': 'https://a.example', 'api_key': '__KEEP__',
         'models': ['m'], 'type': 'openai', 'enabled': True}
    assert client.post('/api/providers', json={'providers': [p]}).get_json()['ok']
    final = json.loads(tmp_config.read_text())
    assert final['providers'][0]['api_key'] == 'sk-asli'


def test_clean_book_name():
    raw = ('13e022f80b934a06b0f623b7db8f1cad_Sriwijaya_--_Prof__Dr__X_--_2011_'
           '--_be6aa077233e088d00faee08afa9e860_--_Annas_Archive.pdf')
    out = A.clean_book_name(raw)
    assert out == 'Sriwijaya — Prof Dr X — 2011 — Annas Archive'


# ------------------------------------------------ notifikasi
def test_notify_config_roundtrip(client, tmp_config):
    r = client.post('/api/notify', json={
        'telegram_token': '123:ABC', 'telegram_chat': '42',
        'webhook_url': ''})
    assert r.get_json()['ok']
    d = json.loads(tmp_config.read_text())
    assert d['notify']['telegram_chat'] == '42'
    got = client.get('/api/notify').get_json()
    assert got['telegram_token_set'] is True
    assert 'api' not in got   # token tidak pernah dikirim balik


def test_notify_tanpa_konfigurasi_pesan_jelas(client):
    r = client.post('/api/notify/test')
    assert r.status_code == 200 and r.get_json()['ok'] is False
    assert 'belum ada' in r.get_json()['msg']


# ------------------------------------------------ auth + CSRF
def test_auth_flow_lengkap(client, monkeypatch):
    monkeypatch.setenv('FLASK_PASSWORD', 'rahasia123')
    try:
        # belum login → redirect
        r = client.get('/')
        assert r.status_code == 302 and '/login' in r.headers['Location']
        # API fetch → 401 JSON
        r = client.get('/api/jobs')
        assert r.status_code == 401 and r.get_json()['error'] == 'belum login'
        # halaman login + token CSRF
        html = client.get('/login').get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        # password salah → 401
        r = client.post('/login', data={'password': 'salah', 'csrf_token': token})
        assert r.status_code == 401
        # CSRF salah → 403
        r = client.post('/login', data={'password': 'rahasia123', 'csrf_token': 'x'})
        assert r.status_code == 403
        # benar → redirect /
        r = client.post('/login', data={'password': 'rahasia123',
                                        'csrf_token': token, 'next': '/'})
        assert r.status_code == 302
        # halaman terbuka
        assert client.get('/').status_code == 200
        # POST tanpa CSRF → 403
        r = client.post('/api/notify', json={'telegram_chat': '1'})
        assert r.status_code == 403
        # POST dengan CSRF dari meta → 200
        html = client.get('/settings').get_data(as_text=True)
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
        r = client.post('/api/notify', json={'telegram_chat': '1'},
                        headers={'X-CSRF-Token': csrf})
        assert r.status_code == 200 and r.get_json()['ok']
        # logout → kembali terkunci
        r = client.get('/logout')
        assert r.status_code == 302
        assert client.get('/').status_code == 302
    finally:
        monkeypatch.delenv('FLASK_PASSWORD', raising=False)
        A._login_fails.clear()


def test_login_rate_limit(client, monkeypatch):
    monkeypatch.setenv('FLASK_PASSWORD', 'pw')
    A._login_fails.clear()
    try:
        html = client.get('/login').get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        for _ in range(5):
            client.post('/login', data={'password': 'x', 'csrf_token': token})
        r = client.post('/login', data={'password': 'x', 'csrf_token': token})
        assert r.status_code == 429
    finally:
        A._login_fails.clear()
        monkeypatch.delenv('FLASK_PASSWORD', raising=False)


def test_tanpa_password_mode_terbuka(client, monkeypatch):
    monkeypatch.delenv('FLASK_PASSWORD', raising=False)
    # login di-redirect ke / (tidak ada yang perlu diautentikasi)
    r = client.get('/login')
    assert r.status_code == 302
