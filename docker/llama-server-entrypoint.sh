#!/bin/sh
# Entrypoint of the Compose `llama-server` service (#654).
#
# Starts llama-server in single-model mode, once the file it is given with `-m` exists. Without `-m`,
# llama.cpp (v0.4.1) starts in router mode: `/health` answers 200 with no model loaded, and every chat
# request the app sends (none names a `model`) is refused with HTTP 400. So a missing `-m` is refused
# here, and a missing file is waited for rather than handed to llama-server, which would exit and be
# restarted in a loop. Until the file exists nothing listens on the port, so the healthcheck fails: an
# unhealthy container is how a Compose operator sees that no model is loaded.
#
# The file appears when the model is downloaded from Settings > Models (download.py renames the
# finished `.part` file into place, so a partial download is never seen here), or when LLM_MODEL_FILE
# names a GGUF already in the `llama_models` volume.
set -eu

model=""
prev=""
for arg in "$@"; do
    if [ "$prev" = "-m" ] || [ "$prev" = "--model" ]; then
        model="$arg"
    fi
    prev="$arg"
done

if [ -z "$model" ]; then
    echo "llama-server-entrypoint: no -m given; refusing to start llama-server in router mode (#654)" >&2
    exit 64
fi

if [ ! -f "$model" ]; then
    case "$model" in
    */)
        echo "llama-server-entrypoint: no model configured (LLM_MODEL_FILE is empty). Set it in .env to a" \
            "curated model's file and run 'docker compose up -d'. llama-server is not started." >&2
        ;;
    *)
        echo "llama-server-entrypoint: waiting for $model. Download the model from Settings > Models in the" \
            "web app, or set LLM_MODEL_FILE in .env to a GGUF in the llama_models volume. llama-server" \
            "starts when the file exists; until then this container reports unhealthy." >&2
        ;;
    esac
    while [ ! -f "$model" ]; do
        sleep "${LLAMA_WAIT_INTERVAL:-10}"
    done
    echo "llama-server-entrypoint: $model is present; starting llama-server" >&2
fi

exec "${LLAMA_SERVER_BIN:-/app/llama-server}" "$@"
