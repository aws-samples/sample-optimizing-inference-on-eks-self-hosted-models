#!/usr/bin/env bash
# compose.sh <fetch|render|upload>: side-by-side clips of the E7' takes (Qwen3.8 matrix driver v4).
#
# One phase per container of the Job bench/q38-take-compose-<run_id> (created by matrix_driver.py; this file ships
# in the ConfigMap q38-take-capture), all sharing the emptyDir $WORK_DIR:
#   fetch   (AWS CLI image)  refuse if any output key exists (append-only); download each take's webm and its
#                            <take_id>-capture.json (video start/end instants written by capture.py); a missing or
#                            empty input fails the Job before anything is uploaded
#   render  (ffmpeg image)   the full side-by-side synced on each replay start, the ~target_s deck clip, the 3-panel
#                            still, and render.json (offsets, length, speed-up)
#   upload  (AWS CLI image)  upload the three outputs, then render.json; never overwrite a key
# Input: COMPOSE_SPEC, the JSON of compose_spec(): run_id, target_s, tail_s, note, inputs[] (cell, take_id, label,
# webm_uri, replay_start_ms, sync_ms, replay_end_ms or null), outputs {full, deck, still}. Baseline (S0) left,
# candidate (S7) right. Sync: each webm is cut at its ACTUAL replay start (sync_ms = the first profiling request,
# else the planned replay_start_ms; minus video_t0_ms); a recording that started after the
# replay (negative offset) is padded at the head with its first frame. Length L = the later replay end + tail_s,
# capped by the shorter recording after its replay start. Deck: L / target_s speed-up (never slowed down). Still:
# the full clip at L/3, 2L/3 and its end, stacked. Takes are not results of record (spec section 8): the clips show
# the same traffic on both stacks; numbers on slides come from the matrix. The mp4s carry that note as their
# comment metadata, render.json as "note".
set -euo pipefail

phase="${1:?usage: compose.sh fetch|render|upload}"
work="${WORK_DIR:-/work}"
spec="${COMPOSE_SPEC:?COMPOSE_SPEC is not set}"
font=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf
fps=25
cells=(S0 S7)

say() { printf 'compose %s: %s\n' "$phase" "$*" >&2; }
die() {
  say "FATAL: $*"
  exit 1
}
field() { jq -er "$1" <<<"$spec"; }
input() { jq -er --arg c "$1" ".inputs[] | select(.cell == \$c) | $2" <<<"$spec"; }
# seconds with millisecond precision of a non-negative millisecond count
secs() { printf '%d.%03d' "$(($1 / 1000))" "$(($1 % 1000))"; }
exists() {
  local u="${1#s3://}"
  aws s3api head-object --bucket "${u%%/*}" --key "${u#*/}" >/dev/null 2>&1
}

fetch_phase() {
  local key uri cell webm meta
  mkdir -p "$work/in" "$work/out" "$HOME"
  for key in full deck still; do
    uri="$(field ".outputs.$key")"
    if exists "$uri"; then die "$uri already exists (append-only)"; fi
  done
  for cell in "${cells[@]}"; do
    webm="$(input "$cell" .webm_uri)"
    meta="${webm%.webm}-capture.json"
    for uri in "$webm" "$meta"; do
      exists "$uri" || die "input $uri is missing (take $(input "$cell" .take_id)); nothing was uploaded"
    done
    aws s3 cp --only-show-errors "$webm" "$work/in/$cell.webm"
    aws s3 cp --only-show-errors "$meta" "$work/in/$cell-capture.json"
    [ -s "$work/in/$cell.webm" ] || die "input $webm is empty; nothing was uploaded"
    jq -e '(.video_t0_ms | type == "number") and (.video_end_ms | type == "number")' "$work/in/$cell-capture.json" \
      >/dev/null || die "input $meta has no video_t0_ms/video_end_ms; nothing was uploaded"
    say "$cell $(input "$cell" .take_id): $(stat -c %s "$work/in/$cell.webm") bytes"
  done
}

render_phase() {
  local target tail_ms cell meta t0 t1 start end rel want=0 ends=1 len_ms="" off pre note
  local -A pre_of avail_of off_of
  mkdir -p "$work/out" "$HOME"
  target="$(field .target_s)"
  note="$(jq -r '.note // "TAKE - NOT A RESULT OF RECORD (spec section 8)"' <<<"$spec")"
  tail_ms=$(($(field .tail_s) * 1000))
  for cell in "${cells[@]}"; do
    meta="$work/in/$cell-capture.json"
    t0="$(jq -er .video_t0_ms "$meta")"
    t1="$(jq -er .video_end_ms "$meta")"
    start="$(input "$cell" '(.sync_ms // .replay_start_ms)')"
    end="$(jq -r --arg c "$cell" '.inputs[] | select(.cell == $c) | .replay_end_ms // empty' <<<"$spec")"
    off=$((start - t0))
    off_of[$cell]=$off
    avail_of[$cell]=$((t1 - start))
    if [ "$off" -ge 0 ]; then
      pre="setpts=PTS-STARTPTS,trim=start=$(secs "$off"),setpts=PTS-STARTPTS"
    else
      pre="setpts=PTS-STARTPTS,tpad=start_duration=$(secs $((-off))):start_mode=clone"
    fi
    pre_of[$cell]=$pre
    if [ -n "$end" ]; then
      rel=$((end - start))
      if [ "$rel" -gt "$want" ]; then want=$rel; fi
    else
      ends=0
    fi
    if [ -z "$len_ms" ] || [ "${avail_of[$cell]}" -lt "$len_ms" ]; then len_ms=${avail_of[$cell]}; fi
    say "$cell: replay start at ${off} ms of the video (negative: head padded), $((avail_of[$cell] / 1000)) s after it"
  done
  if [ "$ends" -eq 1 ] && [ $((want + tail_ms)) -lt "$len_ms" ]; then len_ms=$((want + tail_ms)); fi
  [ "$len_ms" -ge 10000 ] || die "only ${len_ms} ms of synced video"
  local len full deck still
  len="$(secs "$len_ms")"
  full="$work/out/$(basename "$(field .outputs.full)")"
  deck="$work/out/$(basename "$(field .outputs.deck)")"
  still="$work/out/$(basename "$(field .outputs.still)")"
  input S0 .label >"$work/label-S0.txt"
  input S7 .label >"$work/label-S7.txt"
  printf '%s' 'replay t+%{pts:gmtime:0:%H\:%M\:%S}' >"$work/clock.txt"
  cat >"$work/full.filter" <<EOF
[0:v]${pre_of[S0]},fps=$fps,trim=duration=$len,setpts=PTS-STARTPTS,scale=960:540:flags=lanczos,setsar=1[l];
[1:v]${pre_of[S7]},fps=$fps,trim=duration=$len,setpts=PTS-STARTPTS,scale=960:540:flags=lanczos,setsar=1[r];
[l][r]hstack=inputs=2,pad=1920:1080:0:270:color=black,
drawtext=fontfile=$font:textfile=$work/label-S0.txt:fontsize=38:fontcolor=white:x=(960-text_w)/2:y=180,
drawtext=fontfile=$font:textfile=$work/label-S7.txt:fontsize=38:fontcolor=white:x=960+(960-text_w)/2:y=180,
drawtext=fontfile=$font:textfile=$work/clock.txt:fontsize=32:fontcolor=white@0.85:x=(w-text_w)/2:y=850,
format=yuv420p[v]
EOF
  local enc=(-c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p -movflags +faststart -metadata "comment=$note" -an)
  say "full: ${len} s, offsets S0 ${off_of[S0]} ms, S7 ${off_of[S7]} ms"
  ffmpeg -hide_banner -nostats -loglevel warning -n -i "$work/in/S0.webm" -i "$work/in/S7.webm" \
    -/filter_complex "$work/full.filter" -map "[v]" -r "$fps" "${enc[@]}" "$full"
  # deck: L / target_s speed-up (integer ms ratio in the setpts expression), never slower than real time
  local speed_expr="PTS*${target}000/${len_ms}" factor=$(((len_ms + target * 500) / (target * 1000)))
  if [ "$len_ms" -le $((target * 1000)) ]; then
    speed_expr="PTS"
    factor=1
  fi
  printf '%s' "${factor}x speed" >"$work/speed.txt"
  say "deck: ${factor}x (target ${target} s)"
  ffmpeg -hide_banner -nostats -loglevel warning -n -i "$full" \
    -vf "setpts=$speed_expr,fps=$fps,drawtext=fontfile=$font:textfile=$work/speed.txt:fontsize=30:fontcolor=white@0.85:x=w-text_w-40:y=40" \
    -r "$fps" "${enc[@]}" "$deck"
  local i at frames=()
  for i in 1 2 3; do
    at=$((i * len_ms / 3))
    if [ "$i" -eq 3 ]; then at=$((len_ms - 500)); fi
    ffmpeg -hide_banner -nostats -loglevel warning -n -ss "$(secs "$at")" -i "$full" -frames:v 1 \
      -vf "crop=1920:750:0:150" -update 1 "$work/still-$i.png"
    frames+=(-i "$work/still-$i.png")
  done
  ffmpeg -hide_banner -nostats -loglevel warning -n "${frames[@]}" -filter_complex "vstack=inputs=3" -frames:v 1 -update 1 "$still"
  jq -n --argjson spec "$spec" --argjson len_ms "$len_ms" --argjson factor "$factor" --arg note "$note" \
    --argjson off0 "${off_of[S0]}" --argjson off7 "${off_of[S7]}" --arg ffmpeg "$(ffmpeg -version | head -n 1)" \
    '{run_id: $spec.run_id, length_ms: $len_ms, deck_speedup: $factor, target_s: $spec.target_s,
      offsets_ms: {S0: $off0, S7: $off7}, fps: 25, canvas: "1920x1080, panels 960x540 at y=270",
      still: "full clip at L/3, 2L/3 and L-0.5 s, rows cropped to y 150-900", ffmpeg: $ffmpeg,
      sync: "each input cut at sync_ms (first profiling request), else replay_start_ms",
      inputs: $spec.inputs, outputs: $spec.outputs, result_of_record: false, note: $note}' >"$work/out/render.json"
  say "done: $(cd "$work/out" && ls)"
}

upload_phase() {
  local key uri file base
  for key in full deck still; do
    uri="$(field ".outputs.$key")"
    file="$work/out/$(basename "$uri")"
    [ -s "$file" ] || die "missing output $file"
    if exists "$uri"; then die "$uri already exists (append-only)"; fi
    aws s3 cp --only-show-errors "$file" "$uri"
    say "uploaded $uri ($(stat -c %s "$file") bytes)"
  done
  uri="$(field .outputs.full)"
  base="${uri%/*}/render.json"
  if exists "$base"; then die "$base already exists (append-only)"; fi
  aws s3 cp --only-show-errors "$work/out/render.json" "$base"
  say "uploaded $base"
}

case "$phase" in
  fetch) fetch_phase ;;
  render) render_phase ;;
  upload) upload_phase ;;
  *) die "unknown phase '$phase' (fetch|render|upload)" ;;
esac
