#!/usr/bin/env bash
# Explicit human-only audio capture. Never called by the installer or doctor.
set -eu
umask 077

usage() {
    printf '%s\n' \
        'Usage: scripts/record-sample.sh [--help]' \
        'With no arguments, requires an interactive terminal and Enter confirmation.' \
        'Records the default microphone as 16 kHz mono PCM WAV until Ctrl+C.' \
        "Audio stays in a new private directory beneath this repository's tmp/samples/." \
        'Nothing is played, transcribed, uploaded, or copied to the clipboard.'
}

if (( $# )); then
    if (( $# == 1 )) && [[ $1 == --help || $1 == -h ]]; then
        usage
        exit 0
    fi
    usage >&2
    exit 2
fi
if [[ ! -t 0 || ! -t 1 ]]; then
    printf '%s\n' 'Refusing noninteractive recording: use a terminal, then confirm with Enter.' >&2
    exit 1
fi
if ! command -v arecord >/dev/null 2>&1; then
    printf '%s\n' 'arecord is missing. Install ALSA utilities yourself before recording.' >&2
    exit 1
fi
repo=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
for directory in "$repo/tmp" "$repo/tmp/samples"; do
    if [[ -L $directory || ( -e $directory && ! -d $directory ) ]]; then
        printf '%s\n' 'Refusing unsafe tmp/samples path (symlink or non-directory).' >&2
        exit 1
    fi
done
printf '%s\n' 'This will open your default microphone. Press Enter to begin, then Ctrl+C to finish.'
printf '%s' 'Any other input cancels: '
IFS= read -r confirmation || exit 1
if [[ -n $confirmation ]]; then
    printf '%s\n' 'Cancelled; microphone was not opened.'
    exit 0
fi
mkdir -p -- "$repo/tmp/samples"
output_dir=$(mktemp -d "$repo/tmp/samples/sample-XXXXXXXX")
chmod 700 "$output_dir"
audio="$output_dir/sample.wav"
printf '%s\n' 'Recording now. Press Ctrl+C to stop; memory and disk are not bottomless.'
interrupted=0
trap 'interrupted=1' INT
status=0
arecord -D default -t wav -f S16_LE -r 16000 -c 1 "$audio" || status=$?
trap - INT
if (( status != 0 && status != 130 && interrupted == 0 )); then
    printf 'Recording failed (exit %s). Any partial audio remains private at: %s\n' "$status" "$audio" >&2
    exit "$status"
fi
printf '\nAudio file: %s\n' "$audio"
printf '%s\n' 'Nothing else was run. Optional file-only transcription (run it yourself with your installed model/backend):'
printf '  voxtype --config %q transcribe %q > %q\n' "$repo/config/voxtype.toml" "$audio" "$output_dir/transcript.txt"
