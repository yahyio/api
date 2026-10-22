#!/bin/sh
set -eu

exec waitress-serve --host=0.0.0.0 --port="${PORT:-8080}" webapp:app
