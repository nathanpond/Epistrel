#!/bin/sh
# Container entrypoint. No arguments (or `serve ...`): migrate to head, then run the API.
# Anything else is executed as given, so `docker run epistrel:dev python -c ...` works.
set -eu
cd /app
if [ "$#" -eq 0 ]; then
    set -- serve
fi
if [ "$1" = "serve" ]; then
    alembic upgrade head
    exec epistrel "$@"
fi
exec "$@"
