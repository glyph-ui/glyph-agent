#!/bin/zsh
# Startet den LightOnOCR-Server (LaunchAgent-Wrapper). Log: ~/Library/Logs/glyph-agent/
export PATH="/opt/homebrew/bin:/usr/bin:/bin"
export LIGHTON_PORT="${LIGHTON_PORT:-8790}"
exec "$HOME/.glyph/ocr-env/bin/python" "$HOME/glyph-agent/scripts/lighton_server.py"