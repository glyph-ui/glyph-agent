#!/bin/sh
# Baut den Vision-OCR-Helfer fuer Scan-PDFs (macOS: PDFKit + Vision, kein Fremdpaket).
# Aufruf: sh scripts/build-ocr.sh [zielpfad]   (Default: ~/.glyph/bin/ocr_pdf)
set -e
SRC="$(cd "$(dirname "$0")" && pwd)/ocr_pdf.swift"
OUT="${1:-$HOME/.glyph/bin/ocr_pdf}"
mkdir -p "$(dirname "$OUT")"
swiftc -O -module-cache-path "${TMPDIR:-/tmp}/swift-modcache-ocr" -o "$OUT" "$SRC"
echo "gebaut: $OUT"
