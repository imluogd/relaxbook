#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
install_dir="$root/.local/qwen3-tts"
release=0.11.2
archive="speech-$release-linux-x64-cpu.zip"
model=Qwen3-TTS-12Hz-1.7B-CustomVoice-Q4_K.gguf

verify() {
    printf '%s  %s\n' "$2" "$1" | sha256sum --check --status
}

download() {
    local name=$1 url=$2 checksum=$3 partial attempt
    partial="$name.part"
    if [[ -f "$name" ]]; then
        verify "$name" "$checksum" || { echo "SHA-256 mismatch: $name" >&2; return 1; }
        return
    fi
    for attempt in 1 2 3; do
        if [[ -f "$partial" ]] && verify "$partial" "$checksum"; then
            break
        fi
        if curl --fail --location --continue-at - --connect-timeout 15 \
            --speed-limit 1024 --speed-time 45 --output "$partial" "$url"; then
            break
        fi
    done
    verify "$partial" "$checksum" || { echo "Download incomplete or SHA-256 mismatch: $partial" >&2; return 1; }
    mv -- "$partial" "$name"
}

setup_assets() (
    if [[ "$(uname -s)-$(uname -m)" != Linux-x86_64 ]]; then
        echo "This installer requires x86-64 Linux or WSL." >&2
        exit 1
    fi
    mkdir -p -- "$install_dir"
    cd -- "$install_dir"
    exec 9>setup.lock
    flock 9
    download "$archive" "https://github.com/nyosegawa/speech.cpp/releases/download/v$release/$archive" \
        e04de4380d8a0f1bdd61b1c61b7075363a7cadc78d31d40f64187750b0c39a7d
    download "$model" "https://huggingface.co/sakasegawa/Qwen3-TTS-12Hz-1.7B-CustomVoice-GGUF/resolve/main/$model" \
        d643b25ffa11e7c9687aae50d21b5a47076c4f5642c5e6b957f101a98024cc74
    python3 - "$archive" <<'PY'
from pathlib import Path
from zipfile import ZipFile
import sys

with ZipFile(sys.argv[1]) as release:
    for name in ("libspeech.so.4", "speech"):
        target = Path(name)
        partial = target.with_suffix(".partial")
        partial.write_bytes(release.read(name))
        partial.chmod(0o755)
        partial.replace(target)
PY
    ./speech --version
)

serve() {
    local port=$1
    if [[ ! -x "$install_dir/speech" || ! -f "$install_dir/$model" ]]; then
        echo "Run bash scripts/qwen-local.sh setup first." >&2
        return 1
    fi
    local -a command=("$install_dir/speech" serve "$install_dir/$model" --device cpu --no-warmup
        --host 127.0.0.1 --port "$port" --voice serena --language zh)
    if [[ "${2:-}" != managed ]]; then
        exec "${command[@]}"
    fi
    # The parent RelaxBook process closes stdin when it shuts down.
    "${command[@]}" &
    local server=$!
    (IFS= read -r _ || true) <&0 &
    local watcher=$!
    trap "kill $server $watcher 2>/dev/null || true; wait $server $watcher 2>/dev/null || true" EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    wait -n "$server" "$watcher"
}

case "${1:-}" in
    setup)
        setup_assets
        ;;
    serve)
        serve "${2:-8777}" "${3:-}"
        ;;
    start)
        setup_assets
        serve "${2:-8777}"
        ;;
    *)
        echo "Usage: bash scripts/qwen-local.sh start [port] | setup | serve [port]" >&2
        exit 2
        ;;
esac
