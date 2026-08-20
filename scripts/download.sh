#!/usr/bin/env bash
# Extracted from .github/workflows/yt-dl.yml.
# Lives here because GitHub caps a run: block at 21000 chars.
# Inputs arrive as env vars: YOUTUBE_URLS QUALITY PLAYLIST PLAYLIST_ITEMS
#                            REPO_OWNER REPO_NAME
set -uo pipefail

YOUTUBE_URLS="${YOUTUBE_URLS:?YOUTUBE_URLS is required}"
QUALITY="${QUALITY:-best}"
PLAYLIST="${PLAYLIST:-false}"
PLAYLIST_ITEMS="${PLAYLIST_ITEMS:-}"
REPO_OWNER="${REPO_OWNER:?REPO_OWNER is required}"
REPO_NAME="${REPO_NAME:?REPO_NAME is required}"

# Playlist mode: --yes-playlist + index-prefixed filenames (so a USB
# stick plays them in order); --playlist-items narrows the range.
#
# A blank range defaults to 1-50 rather than "everything": a YouTube
# radio mix (RD... / RDGMEM... URLs) is an INFINITE generated station,
# so "all" expanded to 447 items in practice and ran until timeout.
#
# Rate limiting is essential here. Within one playlist run yt-dlp loops
# with no delay, so a datacenter IP gets throttled and then 403s every
# item at the data-fetch stage (extraction still succeeds, which is why
# the failure looks confusing). --sleep-requests spaces the API calls
# and --min/max-sleep-interval spaces the downloads.
if [ "$PLAYLIST" = "true" ]; then
  [ -z "$PLAYLIST_ITEMS" ] && PLAYLIST_ITEMS="1-50" && echo "No range given — defaulting to 1-50"
  PL_FLAGS=(
    --yes-playlist --ignore-errors
    --playlist-items "$PLAYLIST_ITEMS"
    --sleep-requests 1.5
    --min-sleep-interval 5 --max-sleep-interval 15
    --retry-sleep "http:exp=5:120"
  )
  OUT_TMPL="tmp_downloads/%(playlist_index)s - %(title)s.%(ext)s"
  echo "Playlist mode ON (items: $PLAYLIST_ITEMS)"
else
  # Single URLs need the same throttle resistance as playlists. A flagged
  # WARP exit IP gets 403s at the data-fetch stage even for one video —
  # extraction succeeds, then format 140 is refused. --retry-sleep rides
  # out the backoff instead of giving up after yt-dlp's short default
  # retries. No --yes-playlist here: this is rate limiting, not playlist
  # behaviour, so --no-playlist still applies.
  PL_FLAGS=(
    --no-playlist
    --sleep-requests 1.5
    --retry-sleep "http:exp=5:120"
  )
  OUT_TMPL="tmp_downloads/%(title)s.%(ext)s"
fi

SPLIT_MB=45
SPLIT_BYTES=$(( SPLIT_MB * 1024 * 1024 ))

if [ "$QUALITY" = "audio" ]; then
  BACKUP_DIR="/tmp/audio_backup"
  DEST_FOLDER="sound"
  IS_AUDIO=true
else
  BACKUP_DIR="/tmp/video_backup"
  DEST_FOLDER="videos"
  IS_AUDIO=false
fi
mkdir -p "$BACKUP_DIR"
echo "$BACKUP_DIR"  > /tmp/backup_dir_path.txt
echo "$DEST_FOLDER" > /tmp/dest_folder.txt

mkdir -p "$DEST_FOLDER" tmp_downloads
> /tmp/video_info.txt

BRANCH="${GITHUB_REF_NAME}"

urlencode() {
  python3 -c "import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$1"
}

# Format selection is SABR-aware. Under YouTube's SABR experiment some
# formats lose their direct URL inconsistently across codecs, so a plain
# "bestvideo" (which sorts by bitrate) can settle for a surviving 720p
# even when a 1080p of another codec is still downloadable. To counter
# that we:
#   * use bv* (not bestvideo) so video streams that already carry audio
#     are also eligible — widens the pool when pure video-only is stripped
#   * pair with SORT="res,fps,vcodec,br" below so selection prioritises
#     RESOLUTION first, codec/bitrate last — i.e. grab the tallest
#     surviving stream regardless of codec
#   * keep a muxed/best tail so we still get a file if all adaptive is gone
case "$QUALITY" in
  "audio") FORMAT="bestaudio"; SORT="" ;;
  "best")  FORMAT="bv*+ba/b";                                              SORT="res,fps,vcodec,br" ;;
  "1080")  FORMAT="bv*[height<=1080]+ba/b[height<=1080]";                  SORT="res:1080,fps,vcodec,br" ;;
  "720")   FORMAT="bv*[height<=720]+ba/b[height<=720]";                    SORT="res:720,fps,vcodec,br" ;;
  "480")   FORMAT="bv*[height<=480]+ba/b[height<=480]";                    SORT="res:480,fps,vcodec,br" ;;
  *)       FORMAT="bv*+ba/b";                                              SORT="res,fps,vcodec,br" ;;
esac

# Apply the resolution-first sort (SORT, set with FORMAT above) to every
# invocation so all methods pick the tallest surviving stream under SABR.
run_ytdlp() {
  if [ -n "${SORT:-}" ]; then
    yt-dlp -S "$SORT" "$@" 2> >(tee /tmp/ytdlp_last_error >&2)
  else
    yt-dlp "$@" 2> >(tee /tmp/ytdlp_last_error >&2)
  fi
}

# Car-stereo MP3 profile: car head units reject VBR LAME (yt-dlp's
# default --audio-quality 0), reject 48 kHz, and ignore ID3v2.4 tags.
# Force 320k CBR / 44.1 kHz / ID3v2.3 on the ExtractAudio ffmpeg call.
# The postprocessor key MUST be lowercase: yt-dlp lowercases keys when matching,
# so "ExtractAudio:" silently never matches and the args are dropped with no
# warning — the log still shows "[ExtractAudio] Destination: ....mp3" while
# ffmpeg falls back to its default ~128k VBR. Verified: "ExtractAudio:" -> 64k,
# "extractaudio:" -> 320k.
#
# Do NOT add --audio-quality here: it emits its own -q:a, which lands BEFORE
# these args and yields a low VBR bitrate (0 -> ~76k).
MP3_CAR=(
  --postprocessor-args "extractaudio:-c:a libmp3lame -b:a 320k -ar 44100 -map_metadata 0 -id3v2_version 3"
  --embed-metadata
)

download_audio() {
  local METHOD=$1
  echo "Trying audio method $METHOD..."
  case $METHOD in
    1)
      # multi-client + JS solver + GVS PO token (bgutil) — primary path
      run_ytdlp \
        --proxy "socks5://127.0.0.1:1080" \
        -f "bestaudio[ext=m4a]/bestaudio" \
        --extract-audio --audio-format mp3 "${MP3_CAR[@]}" \
        --output "$OUT_TMPL" \
        "${PL_FLAGS[@]}" --retries 10 --fragment-retries 20 \
        --concurrent-fragments 4 --no-check-certificates --no-warnings \
        --throttled-rate 100K \
        --extractor-args "youtube:player_client=web,mweb,android_vr" \
        "${JS_GITHUB[@]}" \
        "$URL"
      ;;
    2)
      # android_vr alone: clean audio-only formats, no PO token needed.
      # Format chain is deliberately wide: this method is the rescue path
      # when method 1 gets 403'd by throttling, so a missing m4a must fall
      # through to any audio (or a muxed best) rather than aborting with
      # "Requested format is not available" before downloading anything.
      run_ytdlp \
        --proxy "socks5://127.0.0.1:1080" \
        -f "bestaudio[ext=m4a]/bestaudio/bestaudio*/best" \
        --extract-audio --audio-format mp3 "${MP3_CAR[@]}" \
        --output "$OUT_TMPL" \
        "${PL_FLAGS[@]}" --retries 10 --fragment-retries 20 \
        --concurrent-fragments 4 --no-check-certificates --no-warnings \
        --throttled-rate 100K \
        --extractor-args "youtube:player_client=android_vr" \
        "$URL"
      ;;
    3)
      # android_vr without proxy: last-resort exit IP
      run_ytdlp \
        -f "bestaudio[ext=m4a]/bestaudio/bestaudio*/best" \
        --extract-audio --audio-format mp3 "${MP3_CAR[@]}" \
        --output "$OUT_TMPL" \
        "${PL_FLAGS[@]}" --retries 10 --fragment-retries 20 \
        --concurrent-fragments 1 --no-check-certificates --no-warnings \
        --throttled-rate 100K \
        --extractor-args "youtube:player_client=android_vr" \
        "$URL"
      ;;
  esac
}

# JS-solver flags: yt-dlp runs YouTube's BotGuard/PO-token + n-sig
# challenge in Deno via remotely-fetched EJS components. This is the
# mechanism that actually clears datacenter-IP bot detection in 2026
# (proven by the Ourtube project on the same kind of runner).
JS_GITHUB=(--js-runtimes deno --remote-components ejs:github)
DESKTOP_UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

download_video() {
  local METHOD=$1
  echo "Trying download method $METHOD..."
  case $METHOD in
    1)
      # multi-client + JS solver + GVS PO token (bgutil): web/mweb get a
      # PO token so their large/adaptive streams download without the
      # HTTP 403 that kills android_vr on big files (e.g. 4K 401+251).
      # yt-dlp picks the best client that clears the check.
      run_ytdlp \
        --proxy "socks5://127.0.0.1:1080" \
        --format "${FORMAT}/best" \
        --output "$OUT_TMPL" \
        "${PL_FLAGS[@]}" --retries 10 --fragment-retries 10 \
        --concurrent-fragments 4 --no-check-certificates --no-warnings \
        --throttled-rate 100K \
        --extractor-args "youtube:player_client=web,mweb,android_vr" \
        "${JS_GITHUB[@]}" \
        "$URL"
      ;;
    2)
      # android_vr alone: no PO token, but fast for SHORT videos whose
      # adaptive stream finishes before YouTube enforces the 403. Good
      # fallback when the token path above is throttled.
      run_ytdlp \
        --proxy "socks5://127.0.0.1:1080" \
        --format "$FORMAT" \
        --output "$OUT_TMPL" \
        "${PL_FLAGS[@]}" --retries 10 --fragment-retries 20 \
        --concurrent-fragments 4 --no-check-certificates --no-warnings \
        --throttled-rate 100K \
        --extractor-args "youtube:player_client=android_vr" \
        "$URL"
      ;;
    3)
      # web + JS solver (github) + desktop UA — clears the bot check on
      # sessions where android_vr fails (e.g. your local IP)
      run_ytdlp \
        --proxy "socks5://127.0.0.1:1080" \
        --format "$FORMAT" \
        --output "$OUT_TMPL" \
        "${PL_FLAGS[@]}" --retries 10 --fragment-retries 20 \
        --concurrent-fragments 4 --no-check-certificates --no-warnings \
        --throttled-rate 100K \
        --extractor-args "youtube:player_client=web" \
        "${JS_GITHUB[@]}" --user-agent "$DESKTOP_UA" \
        "$URL"
      ;;
    4)
      # tv_embedded: full adaptive DASH on IPs where it isn't DRM-locked
      # (works on the local Iran IP; DRM-locked on the runner). Cheap
      # extra net for sessions the clients above miss.
      run_ytdlp \
        --proxy "socks5://127.0.0.1:1080" \
        --format "$FORMAT" \
        --output "$OUT_TMPL" \
        "${PL_FLAGS[@]}" --retries 10 --fragment-retries 20 \
        --concurrent-fragments 4 --no-check-certificates --no-warnings \
        --throttled-rate 100K \
        --extractor-args "youtube:player_client=tv_embedded" \
        "$URL"
      ;;
    5)
      # android_vr WITHOUT proxy: different exit IP as last resort
      run_ytdlp \
        --format "$FORMAT" \
        --output "$OUT_TMPL" \
        "${PL_FLAGS[@]}" --retries 15 --fragment-retries 30 \
        --concurrent-fragments 4 --no-check-certificates \
        --throttled-rate 100K \
        --http-chunk-size 10M \
        --extractor-args "youtube:player_client=android_vr" \
        "$URL"
      ;;
    6)
      # web + JS solver WITHOUT proxy: different exit IP, last resort
      run_ytdlp \
        --format "${FORMAT}/best" \
        --output "$OUT_TMPL" \
        "${PL_FLAGS[@]}" --retries 10 --fragment-retries 20 \
        --concurrent-fragments 4 --no-check-certificates \
        --throttled-rate 100K \
        --extractor-args "youtube:player_client=web" \
        "${JS_GITHUB[@]}" --user-agent "$DESKTOP_UA" \
        "$URL"
      ;;
  esac
}

RANDOM_WORDS=("alpha" "beta" "gamma" "delta" "epsilon" "zeta" "theta" "kappa" "lambda" "sigma" "omega" "nova" "star" "moon" "sun" "sky" "cloud" "river" "ocean" "mountain")

get_random_word() {
  echo "${RANDOM_WORDS[$RANDOM % ${#RANDOM_WORDS[@]}]}_$RANDOM"
}

get_unique_folder() {
  local BASE_PATH="$1"
  local NAME="$2"
  if [ ! -d "$BASE_PATH/$NAME" ] && [ ! -d "$BACKUP_DIR/$NAME" ]; then
    echo "$NAME"
    return
  fi
  local RANDOM_SUFFIX
  RANDOM_SUFFIX=$(get_random_word)
  while [ -d "$BASE_PATH/${NAME}_${RANDOM_SUFFIX}" ] || [ -d "$BACKUP_DIR/${NAME}_${RANDOM_SUFFIX}" ]; do
    RANDOM_SUFFIX=$(get_random_word)
  done
  echo "${NAME}_${RANDOM_SUFFIX}"
}

# Args: $1 readme file  $2 title  $3 original url  $4 size info
#       $5 quality  $6 links markdown rows  $7 footer mode: "split"|"plain"
write_readme() {
  local readme_file="$1" title="$2" orig_url="$3" size_info="$4"
  local quality="$5" links_md="$6" footer="$7"
  {
    printf '%s\n' "# ${title}" "" "---" "" "## Video Information" ""
    printf '%s\n' "| Property | Value |" "|----------|-------|"
    printf '%s\n' "| **Video Name** | \`${title}\` |"
    printf '%s\n' "| **Original Link** | [YouTube Video](${orig_url}) |"
    printf '%s\n' "| **Total Size** | ${size_info} |"
    printf '%s\n' "| **Quality** | **${quality}** |"
    printf '%s\n' "| **Status** | **Complete (100%)** |"
    printf '%s\n' "" "---" ""
    if [ "$footer" = "split" ]; then
      printf '%s\n' "## Download Links" ""
    else
      printf '%s\n' "## Download Link" ""
    fi
    printf '%s\n' "| # | File | Link |" "|---|------|------|"
    printf '%s' "${links_md}"
    printf '%s\n' "" "---" ""
    if [ "$footer" = "split" ]; then
      printf '%s\n' "## How to Extract" ""
      printf '%s\n' "1. **Download** all \`.zip\` and \`.z01\`, \`.z02\`... files"
      printf '%s\n' "2. **Extract** using [7-Zip](https://www.7-zip.org/) or [WinRAR](https://www.rarlab.com/)"
      printf '%s\n' "3. Open the \`.zip\` file - all parts will combine automatically"
      printf '%s\n' "" "---" ""
    elif [ "$footer" = "plain" ]; then
      printf '%s\n' "Ready to use - no extraction needed!" "" "---" ""
    fi
    printf '%s\n' "*This tool created by [avasam.ir](https://avasam.ir)*"
  } > "$readme_file"
}

IFS=',' read -ra RAW_URLS <<< "$YOUTUBE_URLS"
URL_ARRAY=()
for raw in "${RAW_URLS[@]}"; do
  trimmed=$(echo "$raw" | tr -d '[:space:]')
  [[ -z "$trimmed" ]] && continue
  URL_ARRAY+=("$trimmed")
done
URL_COUNT=${#URL_ARRAY[@]}
echo "Total URLs: $URL_COUNT"

YTDLP_UPDATED=false
UPDATE_TRIGGER='extractor error|KeyError|please report|HTTP Error 410|HTTP Error 403|Unable to extract|Unable to download webpage'

maybe_update_ytdlp() {
  if [ "$YTDLP_UPDATED" = true ]; then
    return 1
  fi
  if grep -qE "$UPDATE_TRIGGER" /tmp/ytdlp_last_error 2>/dev/null; then
    echo "Updating yt-dlp..."
    pip3 install --upgrade --force-reinstall "yt-dlp @ https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.tar.gz" 2>/dev/null
    YTDLP_UPDATED=true
    return 0
  fi
  return 1
}

check_quality() {
  local FILE="$1"
  # Audio has no height to check.
  [ "$QUALITY" = "audio" ] && return 0

  local HEIGHT
  HEIGHT=$(ffprobe -v quiet -select_streams v:0 \
    -show_entries stream=height -of csv=p=0 "$FILE" 2>/dev/null | head -1)

  if [ -z "$HEIGHT" ] || [ "$HEIGHT" -eq 0 ] 2>/dev/null; then
    return 0
  fi

  # The gate demands (near) the requested tier so a lower result on an
  # early method does NOT short-circuit the search — methods 2-6 use
  # different clients (and the no-proxy ones a different exit IP), which
  # can reach the target height when method 1's session is SABR-gated.
  # The threshold sits just under the tier to tolerate encoder variance
  # (e.g. 1078 vs 1080). It does NOT skip the video — the best-effort
  # stash keeps the highest file we got, so if no method reaches the
  # target we still save the best available.
  #
  # "best" is treated like 1080: aim high, fall back through the stash.
  # MAX_HEIGHT enforces the requested ceiling: a request for 1080 must
  # NOT accept a 4K file. The capped format string above already aims
  # at the tier, but if a fallback (or a muxed-only client) overshoots,
  # this rejects it so the next method/format is tried. A small margin
  # (e.g. 1088 for 1080) tolerates non-standard encodes.
  local MIN_HEIGHT MAX_HEIGHT
  case "$QUALITY" in
    "best") MIN_HEIGHT=1000; MAX_HEIGHT=0    ;;
    "1080") MIN_HEIGHT=1000; MAX_HEIGHT=1088 ;;
    "720")  MIN_HEIGHT=700;  MAX_HEIGHT=728  ;;
    "480")  MIN_HEIGHT=460;  MAX_HEIGHT=488  ;;
    *)      MIN_HEIGHT=0;    MAX_HEIGHT=0    ;;
  esac

  if [ "$HEIGHT" -lt "$MIN_HEIGHT" ]; then
    echo "Quality check FAILED: ${HEIGHT}p < ${MIN_HEIGHT}p minimum — trying next method"
    return 1
  fi
  if [ "$MAX_HEIGHT" -ne 0 ] && [ "$HEIGHT" -gt "$MAX_HEIGHT" ]; then
    echo "Quality check FAILED: ${HEIGHT}p > ${MAX_HEIGHT}p maximum (requested ${QUALITY}) — trying next method"
    return 1
  fi
  echo "Quality check passed: ${HEIGHT}p (requested ${QUALITY})"
  return 0
}

URL_INDEX=0
for URL in "${URL_ARRAY[@]}"; do
  URL_INDEX=$((URL_INDEX + 1))
  echo "[$URL_INDEX/$URL_COUNT] $URL"

  rm -rf tmp_downloads
  mkdir -p tmp_downloads

  DOWNLOAD_SUCCESS=false

  if echo "$URL" | grep -qiE '(youtube\.com|youtu\.be)'; then
    VIDEO_METHODS="1 2 3 4 5 6"
    # Audio method 1 uses web/mweb, whose format 140 YouTube 403s from
    # datacenter IPs once it starts throttling — on a playlist that
    # means every item fails after the first few. android_vr (method 2)
    # serves audio without that restriction, so lead with it here.
    if [ "$PLAYLIST" = "true" ]; then
      AUDIO_METHODS="2 1 3"
    else
      AUDIO_METHODS="1 2 3"
    fi
  else
    VIDEO_METHODS="1 5"
    AUDIO_METHODS="1"
  fi

  if [ "$IS_AUDIO" = true ]; then
    # In playlist mode --ignore-errors makes yt-dlp exit non-zero when
    # ANY item failed, so a run that fetched 45 of 50 tracks still looks
    # like a failure and the whole playlist gets retried from scratch.
    # Treat "produced at least one file" as success instead.
    audio_run() {
      download_audio "$1" && return 0
      [ "$PLAYLIST" = "true" ] || return 1
      local n
      n=$(find tmp_downloads -maxdepth 1 -type f -name "*.mp3" 2>/dev/null | wc -l)
      [ "$n" -gt 0 ] || return 1
      echo "Playlist: $n track(s) downloaded, some items failed — keeping them"
      return 0
    }

    for METHOD in $AUDIO_METHODS; do
      if audio_run "$METHOD"; then
        DOWNLOAD_SUCCESS=true
        break
      fi
      if maybe_update_ytdlp; then
        if audio_run "$METHOD"; then
          DOWNLOAD_SUCCESS=true
          break
        fi
      fi
      sleep $((RANDOM % 15 + 20))
    done
  else
    # Best-effort stash: when a method downloads a file that fails the
    # quality gate (e.g. 360p when 1080 was requested), keep the largest
    # such file aside instead of discarding it. If no method ever clears
    # the gate, we fall back to this rather than skipping the video.
    BEST_EFFORT_DIR="/tmp/best_effort_${URL_INDEX}"
    rm -rf "$BEST_EFFORT_DIR"

    # Stash the current tmp_downloads video if it is bigger than what we
    # already have stashed; then clear tmp_downloads for the next method.
    stash_best_effort() {
      local f="$1"
      local existing new_size existing_size
      new_size=$(stat -c%s "$f" 2>/dev/null || echo 0)
      existing=$(find "$BEST_EFFORT_DIR" -maxdepth 1 -type f 2>/dev/null | head -1)
      if [ -n "$existing" ]; then
        existing_size=$(stat -c%s "$existing" 2>/dev/null || echo 0)
        [ "$new_size" -le "$existing_size" ] && return
      fi
      rm -rf "$BEST_EFFORT_DIR"; mkdir -p "$BEST_EFFORT_DIR"
      cp "$f" "$BEST_EFFORT_DIR/$(basename "$f")"
      echo "Stashed best-effort fallback: $(basename "$f")"
    }

    try_video_method() {
      local m="$1"
      download_video "$m" || return 1
      DOWNLOADED_FILE=$(find tmp_downloads -maxdepth 1 -type f \( -name "*.mp4" -o -name "*.webm" -o -name "*.mkv" -o -name "*.avi" \) | head -1)
      [ -n "$DOWNLOADED_FILE" ] || return 1

      # Playlist mode: the retry/stash path below is single-file (head -1,
      # and it deletes every video on failure), so one low-res item would
      # re-download the whole playlist and discard the good files. Accept
      # the run; --ignore-errors handles per-item failures.
      if [ "$PLAYLIST" = "true" ]; then
        echo "Playlist mode: keeping downloaded file(s), no per-item quality retry"
        return 0
      fi

      if check_quality "$DOWNLOADED_FILE"; then
        return 0
      fi
      # Failed the gate: remember it as a fallback, then clear for retry
      stash_best_effort "$DOWNLOADED_FILE"
      rm -f tmp_downloads/*.mp4 tmp_downloads/*.webm tmp_downloads/*.mkv tmp_downloads/*.avi
      return 1
    }

    for METHOD in $VIDEO_METHODS; do
      if try_video_method "$METHOD"; then
        DOWNLOAD_SUCCESS=true
        break
      fi
      if maybe_update_ytdlp && try_video_method "$METHOD"; then
        DOWNLOAD_SUCCESS=true
        break
      fi
      sleep $((RANDOM % 15 + 20))
    done

    # Nothing cleared the quality gate, but if we stashed a lower-res
    # file along the way, keep it rather than skipping the video.
    if [ "$DOWNLOAD_SUCCESS" = false ]; then
      STASHED=$(find "$BEST_EFFORT_DIR" -maxdepth 1 -type f 2>/dev/null | head -1)
      if [ -n "$STASHED" ]; then
        echo "No method met the requested quality — keeping best available: $(basename "$STASHED")"
        rm -rf tmp_downloads; mkdir -p tmp_downloads
        cp "$STASHED" "tmp_downloads/$(basename "$STASHED")"
        DOWNLOAD_SUCCESS=true
      fi
    fi
    rm -rf "$BEST_EFFORT_DIR"
  fi

  if [ "$DOWNLOAD_SUCCESS" = false ]; then
    echo "All methods failed for $URL — skipping"
    rm -rf tmp_downloads
    continue
  fi

  for FILE in tmp_downloads/*; do
    [ -f "$FILE" ] || continue
    SIZE=$(stat -c%s "$FILE")
    BASENAME=$(basename "$FILE")
    FILENAME_NO_EXT="${BASENAME%.*}"
    EXT="${BASENAME##*.}"

    FINAL_FOLDER_NAME=$(get_unique_folder "$DEST_FOLDER" "$FILENAME_NO_EXT")
    mkdir -p "$BACKUP_DIR/${FINAL_FOLDER_NAME}"

    echo "${FILENAME_NO_EXT}|${FINAL_FOLDER_NAME}" >> /tmp/video_info.txt

    TEMP_NAME="${FILENAME_NO_EXT}"
    FOLDER_ENCODED=$(urlencode "${FINAL_FOLDER_NAME}")
    README_FILE="$BACKUP_DIR/${FINAL_FOLDER_NAME}/README.md"

    if [ "$SIZE" -gt "$SPLIT_BYTES" ]; then
      if [ "$(basename "$FILE")" != "${TEMP_NAME}.${EXT}" ]; then
        cp "$FILE" "tmp_downloads/${TEMP_NAME}.${EXT}"
      fi

      pushd tmp_downloads > /dev/null
      zip -0 -s "${SPLIT_MB}m" "${TEMP_NAME}.zip" "${TEMP_NAME}.${EXT}"

      for part in "${TEMP_NAME}".z[0-9]* "${TEMP_NAME}".zip; do
        [ -f "$part" ] && mv "$part" "$BACKUP_DIR/${FINAL_FOLDER_NAME}/${FINAL_FOLDER_NAME}.${part##*.}"
      done

      rm -f "${TEMP_NAME}.${EXT}"
      popd > /dev/null

      PART_COUNT=$(ls "$BACKUP_DIR/${FINAL_FOLDER_NAME}/"*.z* 2>/dev/null | wc -l)

      TOTAL_SIZE=0
      for part_file in "$BACKUP_DIR/${FINAL_FOLDER_NAME}"/*; do
        [ -f "$part_file" ] && TOTAL_SIZE=$((TOTAL_SIZE + $(stat -c%s "$part_file")))
      done
      TOTAL_SIZE_MB=$(echo "scale=2; $TOTAL_SIZE / 1024 / 1024" | bc)

      DOWNLOAD_LINKS_MD=""
      LINK_NUM=0
      for part_file in "$BACKUP_DIR/${FINAL_FOLDER_NAME}"/*.z*; do
        if [ -f "$part_file" ]; then
          PART_BASENAME=$(basename "$part_file")
          PART_ENCODED=$(urlencode "${PART_BASENAME}")
          RAW_LINK="https://github.com/${REPO_OWNER}/${REPO_NAME}/raw/${BRANCH}/${DEST_FOLDER}/${FOLDER_ENCODED}/${PART_ENCODED}"
          LINK_NUM=$((LINK_NUM + 1))
          DOWNLOAD_LINKS_MD="${DOWNLOAD_LINKS_MD}| ${LINK_NUM} | \`${PART_BASENAME}\` | [Download](${RAW_LINK}) |"$'\n'
        fi
      done

      write_readme "$README_FILE" "$FILENAME_NO_EXT" "$URL" \
        "**${PART_COUNT} zip parts** - **${TOTAL_SIZE_MB} MB**" \
        "$QUALITY" "$DOWNLOAD_LINKS_MD" "split"

    else
      cp "$FILE" "$BACKUP_DIR/${FINAL_FOLDER_NAME}/${FINAL_FOLDER_NAME}.${EXT}"

      SIZE_MB=$(echo "scale=2; $SIZE / 1024 / 1024" | bc)
      FILE_ENCODED=$(urlencode "${FINAL_FOLDER_NAME}.${EXT}")
      RAW_LINK="https://github.com/${REPO_OWNER}/${REPO_NAME}/raw/${BRANCH}/${DEST_FOLDER}/${FOLDER_ENCODED}/${FILE_ENCODED}"
      LINKS_MD="| 1 | \`${FINAL_FOLDER_NAME}.${EXT}\` | [Download](${RAW_LINK}) |"$'\n'

      write_readme "$README_FILE" "$FILENAME_NO_EXT" "$URL" \
        "**1 file** (no split) - **${SIZE_MB} MB**" \
        "$QUALITY" "$LINKS_MD" "plain"
    fi
  done

  rm -rf tmp_downloads
done
