# ---- frontend build ----
FROM node:24-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ .
RUN npm run build

# ---- backend runtime ----
# python:3.13-slim: conservative choice until all C-extension deps ship cp314 wheels
FROM python:3.13-slim AS runtime
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# libmagic + libmediainfo for python-magic / pymediainfo; ffmpeg provides ffprobe.
# P3-T6 OCR: tesseract-ocr (engine) + poppler-utils (pdftoppm, scanned-PDF raster).
# P3-T11 EXIF/GPS: libimage-exiftool-perl (the real exiftool binary; subprocess
# only, never linked in-process). These binaries are runtime-only — the OCR pass is
# per-library opt-in (default OFF) and the EXIF pass runs for images.
# P12-T5 PDF thumbnails: NO apt package needed -- the pypdfium2 wheel bundles
# libpdfium (the PDFium C library) inside the wheel, so page-1 render works with
# the pip install alone (deliberately unlike libvips/poppler, which would each add
# an apt dependency + an independent CVE surface).
RUN apt-get update && apt-get install -y --no-install-recommends \
      libmagic1 libmediainfo0v5 ffmpeg \
      tesseract-ocr poppler-utils libimage-exiftool-perl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/pyproject.toml ./
RUN uv pip install --system -r pyproject.toml
COPY backend/ .
COPY --from=frontend /build/dist ./static

ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app
EXPOSE 8000
# app:   uvicorn filearr.main:app
# worker: procrastinate --app=filearr.worker.proc_app worker
CMD ["uvicorn", "filearr.main:app", "--host", "0.0.0.0", "--port", "8000"]
