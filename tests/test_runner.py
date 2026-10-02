"""Tes fungsi murni runner.py (via ekstraksi blok — lihat conftest)."""
import os
import pathlib
import subprocess

import pytest

from conftest import load_runner_block, ROOT


# ------------------------------------------------ deteksi tabel/TOC
def _detect_ns():
    return load_runner_block('TABLE_KEYWORDS', '_stop = False', {'re': __import__('re')})


def test_daftar_isi_terdeteksi():
    ns = _detect_ns()
    toc = ("Daftar Isi\nBab 1 Pendahuluan .... 5\nBab 2 Sejarah .... 12\n"
           "Bab 3 Kesultanan .... 30\nBab 4 Ekonomi .... 55\nBab 5 ... 80\n"
           "Bab 6 ... 90\nBab 7 ... 95")
    assert ns['looks_like_table_or_toc'](toc) is True


def test_tabel_statistik_terdeteksi():
    ns = _detect_ns()
    tabel = ("Tabel 1 Curah Hujan\nStasiun\nPeriode\nGemiddeld\nAantal\n"
             "Hoogte\nmm 12\nmm 13\nmm 14\nmm 15")
    assert ns['looks_like_table_or_toc'](tabel) is True


def test_halaman_naratif_tidak_terdeteksi():
    ns = _detect_ns()
    text = ("Bab tiga berisi cerita panjang tentang perjalanan kapal. " * 12
            + "\n" + "\n".join(f"Bagian {i} ada angka {i}" for i in range(8)))
    assert ns['looks_like_table_or_toc'](text) is False


def test_kata_umum_bukan_trigger():
    ns = _detect_ns()
    # 'common'/'ammer' memuat 'mm' — dengan word-boundary tidak boleh trigger
    text = "\n".join("kalimat pendek" for _ in range(6)) + "\ncommon ammer\ncommon 12"
    assert ns['looks_like_table_or_toc'](text) is False


# ------------------------------------------------ merge paragraf
def _merge():
    return load_runner_block('def merge_ocr_paragraphs',
                             'def _append_illustrations', {})


def test_dehyphenation_dan_paragraf():
    ns = _merge()
    out = ns['merge_ocr_paragraphs'](
        "Suatu hari di perjalanan menuju kota,\n"
        "kapal kami berhenti sejenak.\n"
        "\n"
        "Di pelabuhan itu kami bertemu saudagar yang menawarkan "
        "rempah dari timur yang jauh.\n"
        "Kami membeli pala dan ceng- \n"
        "kih dua karung.")
    assert 'cengkih' in out
    assert 'kota, kapal' in out
    assert out.count('\n\n') == 1


def test_dehyphenation_lintas_paragraf():
    ns = _merge()
    out = ns['merge_ocr_paragraphs'](
        "Ia melanjutkan perjalanan konti-\nnental ke arah barat daya.\n\nSelesai.")
    assert 'kontinental' in out
    assert out.count('\n\n') == 1


# ------------------------------------------------ pesan error binary hilang
def test_pdftoppm_hilang_pesan_jelas(tmp_path):
    ns = load_runner_block('def _render_page_png', 'def _cleanup_page_temp',
                           {'subprocess': subprocess, 'pathlib': pathlib,
                            'WORK_DIR': tmp_path, 'PDF': tmp_path / 'x.pdf'})
    old = os.environ.get('PATH')
    os.environ['PATH'] = '/nonexistent'
    try:
        with pytest.raises(RuntimeError, match='pdftoppm tidak ditemukan'):
            ns['_render_page_png'](1)
    finally:
        os.environ['PATH'] = old


def test_pandoc_hilang_pesan_jelas(tmp_path):
    ns = load_runner_block(
        'def update_epub',
        '# ---------------------------------------------------------- translate',
        {'subprocess': subprocess, 'pathlib': pathlib, 'WORK_DIR': tmp_path,
         'EPUB_PATH': tmp_path / 'out.epub', 'BOOK_TITLE': 'T', 'css': ''})
    old = os.environ.get('PATH')
    os.environ['PATH'] = '/nonexistent'
    try:
        with pytest.raises(RuntimeError, match='pandoc tidak ditemukan'):
            ns['update_epub']()
    finally:
        os.environ['PATH'] = old


def test_clean_book_name_ada_di_app():
    # sanity: nama bersih diuji lengkap di test_app.py (butuh import app)
    assert (ROOT / 'app.py').exists()
