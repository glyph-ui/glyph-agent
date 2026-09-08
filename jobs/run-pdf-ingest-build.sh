#!/bin/bash
set -euo pipefail
cd /Users/lxndrhbrt/glyph-agent
export PATH="/Users/lxndrhbrt/.grok/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
GROK_BIN="/Users/lxndrhbrt/.grok/downloads/grok-1.0.13-macos-aarch64"
OUT="/Users/lxndrhbrt/glyph-agent/jobs/pdf-ingest-build-out.txt"
PROMPT="/Users/lxndrhbrt/glyph-agent/jobs/PROMPT-PDF-Ingest-Build.md"
# Own session (no desk leader). --no-leader is agent-subcommand; top-level -p is already solo when no leader.sock.
exec "$GROK_BIN" \
  --prompt-file "$PROMPT" \
  --cwd /Users/lxndrhbrt/glyph-agent \
  --always-approve \
  --permission-mode bypassPermissions \
  --output-format plain \
  --max-turns 50 \
  --no-alt-screen \
  --verbatim \
  >"$OUT" 2>&1
