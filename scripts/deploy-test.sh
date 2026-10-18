#!/usr/bin/env bash
# Выкатка сервиса в test. Обёртка над scripts/deploy-env.sh.
#
# Использование: GH_TOKEN=... scripts/deploy-test.sh <service> <version>
set -euo pipefail
exec "$(dirname "$0")/deploy-env.sh" test "$@"
