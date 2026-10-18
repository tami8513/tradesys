#!/usr/bin/env bash
# Server entrypoint: the dashboard and the scheduler, sharing one persistent disk.
set -euo pipefail

DATA_ROOT="${TRADESYS_DISK:-/var/data}"
mkdir -p "$DATA_ROOT/var" "$DATA_ROOT/clients"

# Profiles kept in git are refreshed on every deploy. Profiles created on the
# server (approved self-registrations) exist only on the disk and are left alone.
cp clients/*.yaml "$DATA_ROOT/clients/"

export TRADESYS_DATA_DIR="$DATA_ROOT/var"
export TRADESYS_CLIENTS_DIR="$DATA_ROOT/clients"
export TRADESYS_SCHEDULER=1
export PYTHONPATH="$PWD/src:$PWD"

# The scheduler restarts itself if it ever exits, so one crash cannot stop trading.
(
  while true; do
    python -m tradesys.scheduler || true
    echo "scheduler exited; restarting in 10s"
    sleep 10
  done
) &

exec streamlit run app.py \
  --server.address 0.0.0.0 \
  --server.port "${PORT:-8501}" \
  --server.headless true
