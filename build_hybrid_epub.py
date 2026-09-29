#!/usr/bin/env python3
import pathlib, subprocess, re, tempfile
import translate as T

PDF = "/srv/data/ebook/Midden-Sumatra Vol 1-1.pdf"
FIRST, LAST = 50, 52
OUT_DIR = pathlib.Path("epub_src")
OUT_DIR.mkdir(exist_ok=True)
IMG_DIR = OUT_DIR / "images"
IMG_DIR.mkdir(exist_ok=True)

html = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<!DOCTYPE html>',
    '<html xmlns="http://www.w3.org/1999/xhtml" lang="id">',
    '<head><title>Midden-Sumatra</title></head>',
    '<body>',
    '<h1>Midden-Sumatra — Sampel Hal 50-52</h1>'
]

with tempfile.TemporaryDirectory() as td:
    for page in range(FIRST, LAST + 1):
        subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-r", "300", "-png", PDF, f"{td}/p"], check=True)
        img_path = next(pathlib.Path(td).glob("p-*.png"))
        
        ocr_out = f"{td}/ocr_{page}"
        subprocess.run(["tesseract", str(img_path), ocr_out, "-l", "nld", "--psm", "3"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        txt_path = pathlib.Path(f"{ocr_out}.txt")
        nl = txt_path.read_text().strip() if txt_path.exists() else ""
        words = len(nl.split())

        if words < 25:
            dest = IMG_DIR / f"page-{page}.png"
            img_path.replace(dest)
            html.append(f'<div><img src="images/page-{page}.png" alt="Hal {page}" style="max-width:100%;"/></div>')
        else:
            en = T.chat("Translate this 19th-century Dutch travel text to English. Keep names, dates. Output only translation.\n\n" + nl)
            id_text = T.chat("Translate this English text to natural Indonesian. Output only translation.\n\n" + en)
            
            html.append(f'<h2>Halaman {page}</h2>')
            for line in id_text.splitlines():
                if line.strip():
                    html.append(f'<p>{line.strip()}</p>')

        for f in pathlib.Path(td).glob("p-*.png"): f.unlink()
        for f in pathlib.Path(td).glob(f"ocr_{page}.*"): f.unlink()

html.extend(['</body>', '</html>'])
xhtml_path = OUT_DIR / "book.xhtml"
xhtml_path.write_text("\n".join(html))

subprocess.run(["pandoc", str(xhtml_path), "-o", "midden_hybrid.epub",
                "--metadata", "title=Midden-Sumatra (Hybrid)", "--metadata", "lang=id"], check=True)
print("SUCCESS, size:", pathlib.Path("midden_hybrid.epub").stat().st_size)
