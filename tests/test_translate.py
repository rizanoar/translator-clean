"""Tes translate.py: parser respons, kandidat vision, durability config."""
import json
import stat

import pytest

import translate as T


# ---------------------------------------------------------------- parser
def test_parse_any_sse_streaming():
    body = ('data: {"choices":[{"delta":{"content":"halo"},"finish_reason":null}]}\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n'
            'data: [DONE]')
    d = T.parse_any(body)
    assert d['choices'][0]['message']['content'] == 'halo'
    assert d['choices'][0]['finish_reason'] == 'stop'


def test_parse_any_trailing_data():
    d = T.parse_any('{"choices":[{"message":{"content":"x"},"finish_reason":"stop"}]} trailing')
    assert d['choices'][0]['message']['content'] == 'x'


def test_parse_any_plain_json():
    d = T.parse_any('{"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}]}')
    assert d['choices'][0]['message']['content'] == 'ok'


def test_parse_any_garbage_returns_none():
    assert T.parse_any('bukan json sama sekali') is None


# ---------------------------------------------------------------- kandidat
def test_vision_model_diprioritaskan(tmp_config):
    T.save_config({'providers': [{
        'name': 'R', 'base_url': 'http://r', 'api_key': 'k',
        'models': ['text-model'], 'vision_model': 'vis-model', 'enabled': True}]})
    cands = T._candidates('data:image/png;base64,x')
    assert cands[0][1] == 'vis-model'
    assert any(m == 'text-model' for _, m in cands)


def test_vision_tanpa_gambar_pakai_chain_biasa(tmp_config):
    T.save_config({'providers': [{
        'name': 'R', 'base_url': 'http://r', 'api_key': 'k',
        'models': ['text-model'], 'vision_model': 'vis-model', 'enabled': True}]})
    cands = T._candidates(None)
    assert [m for _, m in cands] == ['text-model']


# ---------------------------------------------------------------- config
def test_config_atomic_chmod600_backup(tmp_config):
    T.save_config({'providers': [
        {'name': 'A', 'base_url': 'https://a', 'api_key': 'k1', 'models': ['m']}]})
    assert tmp_config.exists()
    assert stat.S_IMODE(tmp_config.stat().st_mode) == 0o600
    T.save_config({'providers': [
        {'name': 'B', 'base_url': 'https://b', 'api_key': 'k2', 'models': ['m']}]})
    bak = tmp_config.with_suffix('.json.bak')
    assert json.loads(bak.read_text())['providers'][0]['name'] == 'A'


def test_config_recovery_dari_bak(tmp_config):
    T.save_config({'providers': [
        {'name': 'A', 'base_url': 'https://a', 'models': ['m']}]})
    T.save_config({'providers': [   # save kedua → .bak terbentuk berisi versi A
        {'name': 'B', 'base_url': 'https://b', 'models': ['m']}]})
    tmp_config.write_text('{korup')
    cfg = T.load_config(force=True)
    assert cfg['providers'][0]['name'] == 'A'


def test_config_korup_tidak_ditimpa(tmp_config):
    tmp_config.write_text('{korup-1')
    tmp_config.with_suffix('.json.bak').write_text('{korup-2')
    before = tmp_config.read_text()
    cfg = T.load_config(force=True)
    assert cfg['providers'][0]['name'].startswith('Router Bawaan')
    assert tmp_config.read_text() == before   # file asli utuh untuk diselamatkan manual


def test_config_pertahankan_kunci_notify(tmp_config):
    T.save_config({'providers': [
        {'name': 'A', 'base_url': 'https://a', 'models': ['m']}],
        'notify': {'telegram_chat': '42'}})
    T.save_config({'providers': [
        {'name': 'B', 'base_url': 'https://b', 'models': ['m']}]})
    d = json.loads(tmp_config.read_text())
    assert d['notify']['telegram_chat'] == '42'
    assert d['providers'][0]['name'] == 'B'


def test_config_chmod600_mengikuti_backup_juga(tmp_config):
    T.save_config({'providers': [{'name': 'A', 'base_url': 'https://a', 'models': ['m']}]})
    bak = tmp_config.with_suffix('.json.bak')
    T.save_config({'providers': [{'name': 'B', 'base_url': 'https://b', 'models': ['m']}]})
    assert stat.S_IMODE(bak.stat().st_mode) == 0o600


@pytest.mark.parametrize('bad', [
    {'providers': []},
    {'providers': [{'name': 'X', 'base_url': '', 'models': ['m']}]},
    {'providers': [{'name': '', 'base_url': 'http://x', 'models': ['m']}]},
])
def test_config_validasi_menolak(tmp_config, bad):
    with pytest.raises(ValueError):
        T.save_config(bad)


# ---------------------------------------------------------------- chat
def test_chat_tanpa_provider_aktif_pesan_jelas(tmp_config):
    T.save_config({'providers': [{
        'name': 'X', 'base_url': 'http://127.0.0.1:1/v1',
        'models': ['m'], 'enabled': False}]})
    with pytest.raises(RuntimeError, match='tidak ada provider/model aktif'):
        T.chat('hai', retries=1)


def test_fetch_models_endpoint_mati_pesan_jelas(tmp_config):
    with pytest.raises(RuntimeError, match='tidak bisa terhubung'):
        T.fetch_remote_models({'name': 'X', 'base_url': 'http://127.0.0.1:1/v1',
                               'api_key': 'k', 'type': 'openai'}, timeout=2)


def test_fetch_models_base_url_kosong(tmp_config):
    with pytest.raises(RuntimeError, match='base_url'):
        T.fetch_remote_models({'name': 'X', 'base_url': '', 'type': 'openai'})


def test_resolve_key_dari_config_menang(tmp_config, monkeypatch):
    monkeypatch.setenv('HERMES_CUSTOM_192_168_1_123_3005_API_KEY', 'env-key')
    p = {'name': 'X', 'base_url': 'https://x/v1', 'api_key': 'cfg-key'}
    assert T.resolve_key(p) == 'cfg-key'
    p2 = {'name': 'X', 'base_url': 'https://x/v1', 'api_key': ''}
    assert T.resolve_key(p2) == ''   # bukan router bawaan → tidak ambil env hermes
