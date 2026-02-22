#!/bin/bash
set -e

# Run migrations only for the API server
if [ "$1" = "harbor-clerk-api" ]; then
    echo "Running database migrations..."
    alembic upgrade head
    echo "Migrations complete."
fi

exec "$@"
