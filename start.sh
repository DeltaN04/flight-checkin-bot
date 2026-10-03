#!/bin/bash
# Run the flight check-in agent
cd "$(dirname "$0")"
[ -f .env ] || cp .env.example .env
./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8001} --reload
