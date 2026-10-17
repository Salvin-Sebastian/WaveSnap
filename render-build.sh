#!/usr/bin/env bash
# exit on error
set -o errexit

echo "==> Installing Python dependencies..."
pip install -r requirements.txt

echo "==> Pre-caching static ffmpeg binaries for runtime..."
python -c "import static_ffmpeg; static_ffmpeg.add_paths()"
echo "==> Build completed successfully."
