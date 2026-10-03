#!/bin/zsh
# Nächtlicher, inkrementeller Aufbau des PDF-Vektorindex (LaunchAgent).
# Überspringt sich selbst, wenn Ollama fehlt oder schon ein Lauf aktiv ist.
set -u

export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
LOCK="$HOME/.glyph/pdf-index.lock"
STAMP="[$(date '+%Y-%m-%d %H:%M:%S')]"

if ! curl -s -m 5 http://localhost:11434/api/version >/dev/null 2>&1; then
  echo "$STAMP Ollama nicht erreichbar — Lauf uebersprungen"
  exit 0
fi

if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +360 2>/dev/null)" ]; then
    rm -rf "$LOCK"
    mkdir "$LOCK" 2>/dev/null || { echo "$STAMP Lock nicht uebernehmbar — uebersprungen"; exit 0; }
  else
    echo "$STAMP Lauf aktiv — uebersprungen"
    exit 0
  fi
fi
trap 'rm -rf "$LOCK"' EXIT

echo "$STAMP Start (inkrementell)"
/opt/homebrew/bin/python3.11 "$HOME/glyph-agent/scripts/build_pdf_index.py" --workers 4
RC=$?
echo "$STAMP Ende (exit $RC)"
exit $RC
