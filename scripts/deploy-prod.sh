#!/usr/bin/env bash
# Выкатка сервиса в prod. Обёртка над scripts/deploy-env.sh.
#
# Использование: GH_TOKEN=... scripts/deploy-prod.sh <service> <version>
set -euo pipefail
exec "$(dirname "$0")/deploy-env.sh" prod "$@"
