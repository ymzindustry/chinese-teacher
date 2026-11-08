#!/usr/bin/env bash
# Démarre l'app sur le LAN : http://192.168.1.51:8099/
set -e
cd "$(dirname "$0")"
exec python3 teacher_server.py --port 8099 --bind 0.0.0.0
