#!/usr/bin/env bash
# glyph-agent recurring To-dos — fällige Läufe nachholen (alle 15 Min).
set -uo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export HOME="${HOME:-/Users/lxndrhbrt}"

LOG="/Users/lxndrhbrt/Library/Logs/glyph-agent/jobs-catchup.log"
mkdir -p "$(dirname "$LOG")"
NOW="$(date '+%Y-%m-%d %H:%M:%S')"

echo "[$NOW] === glyph recurring catchup ===" >> "$LOG"

# Verarbeitungslog-Watchdog: Log prüfen/sichern, bei Verlust sofort wiederherstellen
# (läuft auch, wenn der Agent offline ist — deshalb vor dem Health-Check).
python3 "/Users/lxndrhbrt/glyph-agent/scripts/verarbeitungslog_watchdog.py" >>"$LOG" 2>&1 || true

if ! curl -sf -o /dev/null "http://127.0.0.1:18899/health" 2>/dev/null; then
  echo "[$NOW] glyph-agent offline" >> "$LOG"
  exit 0
fi

# Migration + fällige To-dos
curl -sf "http://127.0.0.1:18899/recurring" -o /dev/null 2>>"$LOG" || true
if curl -sf -X POST "http://127.0.0.1:18899/recurring/run-due" \
  -H "Content-Type: application/json" \
  -d '{}' >>"$LOG" 2>&1; then
  echo "[$NOW] run-due ok" >> "$LOG"
else
  echo "[$NOW] run-due fehlgeschlagen" >> "$LOG"
fi

# Vault-Index frisch halten (Stale-Erkennung + Selbst-Drosselung, s. vault_index_refresh.py)
python3 "/Users/lxndrhbrt/glyph-agent/scripts/vault_index_refresh.py" >>"$LOG" 2>&1 || true

echo "[$NOW] === fertig ===" >> "$LOG"
