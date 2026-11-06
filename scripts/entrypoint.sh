#!/bin/sh
set -eu
paramfabric db-upgrade
exec "$@"
