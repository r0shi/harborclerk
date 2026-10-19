#!/bin/sh
# Entrypoint of the Compose `llama-server` service (#654).
#
# Starts llama-server in single-model mode, once the file it is given with `-m` exists and looks whole.
# Without `-m`, llama.cpp (v0.4.1) starts in router mode: `/health` answers 200 with no model loaded,
# and every chat request the app sends (none names a `model`) is refused with HTTP 400. So a missing
# `-m` is refused here, and a missing or partial file is waited for rather than handed to llama-server,
# which would exit and be restarted in a loop. Until then nothing listens on the port, so the
# healthcheck fails: an unhealthy container is how a Compose operator sees that no model is loaded.
#
# The file appears when the model is downloaded from Settings > Models (download.py renames the
# finished `.part` file into place), or when LLM_MODEL_FILE names a GGUF already in the `llama_models`
# volume. A file copied in by hand under its final name is caught partway by checking the GGUF magic
# and that the size holds still for LLAMA_STABLE_INTERVAL seconds; copy to a temporary name and rename.
#
# While it waits it traps SIGTERM and INT, and sleeps in the background so the trap interrupts the sleep
# at once: `docker compose down` stops a waiting container promptly instead of after the grace period.
# (docker-init is PID 1, from `init: true`, and forwards the signal here.)
set -eu

# What docker-compose.yml passes as -c when LLM_CONTEXT_TOKENS is unset; the app's
# COMPOSE_DEFAULT_CONTEXT_TOKENS (tests/test_docker_compose_llama.py holds the two together).
COMPOSE_DEFAULT_CONTEXT=8192

# LLM_CONTEXT_TOKENS reaches here as the value of `-c`, and Compose interpolation cannot refuse a bad one.
# llama-server (v0.4.1) reads it with std::stoi, which takes the leading digits and ignores the rest: `8k` is 8,
# padded to 256 tokens (every chat overflows, every summary falls back to extractive); `+0` and `0x0` are 0; `-1`
# wraps and crashes; `abc` and anything above INT_MAX throw. And 0 is the model's full training context (262,144
# tokens for Qwen3.5: a KV cache a container likely cannot hold, then an OOM restart loop).
#
# So the value goes through ONE rule, which the app's config.parse_context_tokens applies identically, so the
# context llama-server runs at and the one prompts are budgeted to cannot disagree: trim ASCII whitespace; then a
# valid value is 1 to 7 ASCII digits whose value is at least 1 (at most 9,999,999, far below INT_MAX). Anything
# else is replaced with the compose default, and the warning names the value. tests/test_context_tokens_rule.py
# runs one table through both sides.
#
# Sets `context` to the trimmed value, and `problem` to why it is invalid, or "" when it is valid.
context_problem() {
    context=${1#"${1%%[![:space:]]*}"}
    context=${context%"${context##*[![:space:]]}"}
    problem=""
    case "$context" in
    '' | *[!0123456789]* | ????????*) problem="is not a whole number of tokens from 1 to 9999999" ;;
    *[!0]*) ;;
    *) problem="is zero, which llama-server reads as the model's full training context" ;;
    esac
}

prev=""
rewrite=""
for arg in "$@"; do
    if [ -z "$rewrite" ]; then
        set --
        rewrite=1
    fi
    orig="$arg"
    flag=""
    case "$arg" in
    -c=* | --ctx-size=*)
        flag="${arg%%=*}="
        given="${arg#*=}"
        ;;
    *)
        if [ "$prev" = "-c" ] || [ "$prev" = "--ctx-size" ]; then
            flag=" "
            given="$arg"
        fi
        ;;
    esac
    if [ -n "$flag" ]; then
        context_problem "$given"
        if [ -n "$problem" ]; then
            echo "llama-server-entrypoint: LLM_CONTEXT_TOKENS='$given' $problem; using $COMPOSE_DEFAULT_CONTEXT." \
                "Set LLM_CONTEXT_TOKENS in .env to a whole number of tokens from 1 to 9999999 to change it." >&2
            context="$COMPOSE_DEFAULT_CONTEXT"
        fi
        if [ "$flag" = " " ]; then arg="$context"; else arg="$flag$context"; fi
    fi
    set -- "$@" "$arg"
    prev="$orig"
done

# The model as llama-server would read it: `-m PATH`, `--model PATH`, `--model=PATH`, or LLAMA_ARG_MODEL
# (llama.cpp's environment form of -m). A flag on the command line wins over the environment.
model="${LLAMA_ARG_MODEL:-}"
prev=""
for arg in "$@"; do
    case "$arg" in
    --model=*) model="${arg#--model=}" ;;
    esac
    if [ "$prev" = "-m" ] || [ "$prev" = "--model" ]; then
        model="$arg"
    fi
    prev="$arg"
done

if [ -z "$model" ]; then
    echo "llama-server-entrypoint: no model given (-m, --model or LLAMA_ARG_MODEL); refusing to start" \
        "llama-server in router mode (#654)" >&2
    exit 64
fi

sleeper=""
stop() {
    if [ -n "$sleeper" ]; then
        kill "$sleeper" 2>/dev/null || true
    fi
    exit "$1"
}
trap 'stop 143' TERM
trap 'stop 130' INT

# A sleep the traps can interrupt: a trap runs only once the foreground command returns, and `wait` is
# interrupted by a trapped signal where `sleep` is not.
pause() {
    sleep "$1" &
    sleeper=$!
    wait "$sleeper" || true
    sleeper=""
}

# The file exists, starts with the GGUF magic, and its size does not change over the interval.
whole() {
    [ -f "$model" ] || return 1
    [ "$(head -c 4 "$model" 2>/dev/null)" = "GGUF" ] || return 1
    size=$(wc -c <"$model")
    pause "${LLAMA_STABLE_INTERVAL:-2}"
    [ -f "$model" ] && [ "$(wc -c <"$model")" = "$size" ]
}

if ! whole; then
    case "$model" in
    */)
        echo "llama-server-entrypoint: no model configured: $model is a directory (LLM_MODEL_FILE is empty, or" \
            "ends in '/'). Set it in .env to a curated model's file and run 'docker compose up -d'. llama-server" \
            "is not started." >&2
        ;;
    *)
        echo "llama-server-entrypoint: waiting for $model. Download the model from Settings > Models in the" \
            "web app, or set LLM_MODEL_FILE in .env to a GGUF in the llama_models volume. llama-server" \
            "starts when the file exists; until then this container reports unhealthy." >&2
        ;;
    esac
    said_partial=""
    until whole; do
        if [ -f "$model" ] && [ -z "$said_partial" ]; then
            echo "llama-server-entrypoint: $model is there but is not a whole GGUF yet (no GGUF magic, or" \
                "still growing); waiting. Copy a model in under a temporary name and rename it." >&2
            said_partial=1
        fi
        pause "${LLAMA_WAIT_INTERVAL:-10}"
    done
    echo "llama-server-entrypoint: $model is present; starting llama-server" >&2
fi

trap - TERM INT
exec "${LLAMA_SERVER_BIN:-/app/llama-server}" "$@"
