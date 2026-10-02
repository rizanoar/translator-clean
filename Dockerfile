FROM python:3.11-slim

WORKDIR /app

# pandoc = WAJIB untuk build EPUB; poppler (pdftotext/pdftoppm/pdfinfo);
# tesseract + bahasa nld/eng/ind untuk OCR fallback
RUN apt-get update && apt-get install -y --no-install-recommends \
        pandoc \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-nld \
        tesseract-ocr-eng \
        tesseract-ocr-ind \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p jobs uploads config

EXPOSE 8091
CMD ["python", "app.py"]
