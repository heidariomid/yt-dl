#!/usr/bin/env bash
# Usage: ./download.sh <URL> [quality] [output_dir]
#   quality: best (default), 1080, 720, 480, audio
#   output_dir: ~/Downloads/yt-dl by default
#
# Proxy (pick one):
#   - Set PROXY env var:  PROXY=socks5://127.0.0.1:2080 ./download.sh ...
#   - Or just turn on Hiddify/Shadowrocket/etc. in system proxy mode — auto-detected

set -euo pipefail

URL="${1:-}"
QUALITY="${2:-best}"
OUTPUT_DIR="${3:-$HOME/Downloads/yt-dl}"

if [ -z "$URL" ]; then
  echo "Usage: $0 <YouTube URL> [quality: best|1080|720|480|audio] [output_dir]"
  exit 1
fi

mkdir -p "$OUTPUT_DIR"

# Auto-detect system SOCKS5/HTTP proxy (set by Hiddify, Shadowrocket, v2RayTun, etc.)
detect_system_proxy() {
  local INFO
  INFO=$(scutil --proxy 2>/dev/null || echo "")

  if echo "$INFO" | grep -q "SOCKSEnable : 1"; then
    local HOST PORT
    HOST=$(echo "$INFO" | awk '/SOCKSProxy /{print $3}')
    PORT=$(echo "$INFO" | awk '/SOCKSPort/{print $3}')
    [ -n "$HOST" ] && [ -n "$PORT" ] && echo "socks5://${HOST}:${PORT}" && return
  fi

  if echo "$INFO" | grep -q "HTTPEnable : 1"; then
    local HOST PORT
    HOST=$(echo "$INFO" | awk '/HTTPProxy /{print $3}')
    PORT=$(echo "$INFO" | awk '/HTTPPort/{print $3}')
    [ -n "$HOST" ] && [ -n "$PORT" ] && echo "http://${HOST}:${PORT}" && return
  fi
}

PROXY="${PROXY:-$(detect_system_proxy || true)}"

if [ -n "$PROXY" ]; then
  echo "Proxy: $PROXY"
  PROXY_ARGS="--proxy $PROXY"
else
  echo "No proxy detected — routing direct."
  PROXY_ARGS=""
fi

case "$QUALITY" in
  audio) FORMAT="bestaudio" ;;
  best)  FORMAT="bestvideo+bestaudio/best" ;;
  1080)  FORMAT="bestvideo[height<=1080]+bestaudio/best[height<=1080]/best" ;;
  720)   FORMAT="bestvideo[height<=720]+bestaudio[ext=m4a]/best[height<=720]/best" ;;
  480)   FORMAT="bestvideo[height<=480]+bestaudio[ext=m4a]/best[height<=480]/best" ;;
  *)     FORMAT="bestvideo+bestaudio/best" ;;
esac

echo "Downloading: $URL"
echo "Quality:     $QUALITY"
echo "Output:      $OUTPUT_DIR"
echo ""

if [ "$QUALITY" = "audio" ]; then
  # shellcheck disable=SC2086
  yt-dlp \
    $PROXY_ARGS \
    -f bestaudio \
    --extract-audio \
    --audio-format mp3 \
    --audio-quality 0 \
    --output "$OUTPUT_DIR/%(title)s.%(ext)s" \
    --no-playlist \
    --retries 10 \
    --fragment-retries 10 \
    --no-check-certificates \
    "$URL"
  echo ""
  echo "Done! Saved to: $OUTPUT_DIR"
  exit 0
fi

download_video() {
  local METHOD=$1
  echo "Trying method $METHOD..."
  # shellcheck disable=SC2086
  case $METHOD in
    1)
      yt-dlp $PROXY_ARGS \
        --format "$FORMAT" \
        --output "$OUTPUT_DIR/%(title)s.%(ext)s" \
        --no-playlist --retries 10 --fragment-retries 10 \
        --concurrent-fragments 4 --no-check-certificates \
        --sleep-requests 2 --sleep-interval 1 --max-sleep-interval 5 \
        "$URL" ;;
    2)
      yt-dlp $PROXY_ARGS \
        --format "$FORMAT" \
        --output "$OUTPUT_DIR/%(title)s.%(ext)s" \
        --no-playlist --retries 10 --fragment-retries 10 \
        --user-agent "Mozilla/5.0 (Linux; Android 12; SM-S906N Build/QP1A.190711.020) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36" \
        --extractor-args "youtube:player_client=android" \
        "$URL" ;;
    3)
      yt-dlp $PROXY_ARGS \
        --format "$FORMAT" \
        --output "$OUTPUT_DIR/%(title)s.%(ext)s" \
        --no-playlist --retries 10 --fragment-retries 10 \
        --user-agent "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1" \
        --extractor-args "youtube:player_client=ios" \
        "$URL" ;;
    4)
      yt-dlp $PROXY_ARGS \
        --format "$FORMAT" \
        --output "$OUTPUT_DIR/%(title)s.%(ext)s" \
        --no-playlist --retries 10 --fragment-retries 10 \
        --extractor-args "youtube:player_client=tv_embedded" \
        "$URL" ;;
  esac
}

SUCCESS=false
for METHOD in 1 2 3 4; do
  if download_video $METHOD; then
    SUCCESS=true
    echo ""
    echo "Done! Saved to: $OUTPUT_DIR"
    break
  else
    echo "Method $METHOD failed, trying next..."
    sleep $((RANDOM % 5 + 3))
  fi
done

if [ "$SUCCESS" = false ]; then
  echo "All methods failed. Try: brew upgrade yt-dlp"
  exit 1
fi
