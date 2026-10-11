#!/bin/sh
# Container entrypoint. No arguments: `serve` (in EPISTREL_MODE, default `all`: the API and the
# background workers in one process). `serve ...` and `worker ...` run the Engine; `all` and `api`
# modes migrate to head first, `worker` mode never migrates (the API process owns the schema).
# Anything else is executed as given, so `docker run epistrel:dev python -c ...` works.
set -eu
cd /app
if [ "$#" -eq 0 ]; then
    set -- serve
fi
case "$1" in
    serve)
        mode="${EPISTREL_MODE:-all}"
        previous=""
        for arg in "$@"; do
            case "$arg" in
                --mode=*) mode="${arg#--mode=}" ;;
            esac
            if [ "$previous" = "--mode" ]; then
                mode="$arg"
            fi
            previous="$arg"
        done
        if [ "$mode" != "worker" ]; then
            alembic upgrade head
        fi
        exec epistrel "$@"
        ;;
    worker)
        exec epistrel "$@"
        ;;
esac
exec "$@"
