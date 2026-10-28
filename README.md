# PDF Compressor

Free, open-source desktop app that shrinks PDFs, built for **scanned documents**.
Runs on Windows and macOS. Everything happens on your computer: no uploads, no accounts.

**Website & downloads:** https://yash2301.github.io/pdf-compressor/

| Download | |
|---|---|
| Windows | [PDFCompressor-windows.exe](https://github.com/Yash2301/pdf-compressor/releases/latest/download/PDFCompressor-windows.exe) |
| macOS (Apple Silicon) | [PDFCompressor-mac.zip](https://github.com/Yash2301/pdf-compressor/releases/latest/download/PDFCompressor-mac.zip) |

## Features

- **Smart mode** (default): downsamples and JPEG-recompresses page images to a target DPI. Keeps OCR text, links and bookmarks.
- **Max mode**: re-renders each page as one JPEG. Smallest file, but text is no longer selectable.
- Presets: Low 200 dpi / q80 · Medium 150 dpi / q65 · High 110 dpi / q50 · Extreme 80 dpi / q40
- Optional grayscale conversion.
- Batch: add many files or a whole folder.
- Never writes a file bigger than the original.

Example: a 74.7 MB, 3-page 300 dpi colour scan → 169 KB on the Medium preset.

## First launch

- **Windows:** SmartScreen may warn about an unsigned app. Click *More info* → *Run anyway*.
- **macOS:** unzip, then right-click `PDFCompressor.app` → *Open* → *Open*.

## Run from source

Requires Python 3.10+ with Tk.

```
pip install -r requirements.txt
python app.py
```

## Build it yourself

- **Windows:** double-click `build_exe.bat` → `dist\PDFCompressor.exe`
- **macOS / Linux:** `pyinstaller --windowed --name PDFCompressor app.py`

GitHub Actions builds both platforms on every push. Pushing a tag like `v1.0.0` publishes a release with the downloads attached.

## Contributing

Issues and pull requests are welcome.

## License

[GNU AGPL-3.0](LICENSE). Built on [PyMuPDF](https://github.com/pymupdf/PyMuPDF) (AGPL-3.0) and [Pillow](https://python-pillow.org/).
