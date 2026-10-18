#!/usr/bin/env bash
# Выкатка сервиса в окружение: записывает версию chart в ../infra
# (product/<env>/apps/<service>.yml), дальше её применяет Argo CD.
#
# Использование: GH_TOKEN=... scripts/deploy-env.sh <env> <service> <version>
# GH_TOKEN - токен GitHub App с записью в TeaTeaCode/infra.
#
# Вместо версии можно передать from-test: тогда берётся версия, которая уже
# выкачена в test (product/test/apps/<service>.yml). Так prod получает ровно то,
# что проверено на тесте.
#
# Меняются только targetRevision и, если он закреплён, тег образа той же
# версии: коммит выкатки должен читаться глазами. Сервис без файла в этом
# окружении пропускается, поэтому шаг можно держать во всех workflow.
#
# Обёртки для удобства: scripts/deploy-test.sh и scripts/deploy-prod.sh.
set -euo pipefail

env_name="${1:?env is required (test|prod)}"
service="${2:?service is required}"
version="${3:?version is required}"
infra_repo="${INFRA_REPO:-TeaTeaCode/infra}"

case "${env_name}" in
  test | prod) ;;
  *)
    echo "env must be test or prod, got ${env_name}" >&2
    exit 1
    ;;
esac

app_file="product/${env_name}/apps/${service}.yml"

if [[ "${version}" != "from-test" && ! "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "version must be X.Y.Z or from-test, got ${version}" >&2
  exit 1
fi
: "${GH_TOKEN:?GH_TOKEN is required}"

workdir="$(mktemp -d)"
trap 'rm -rf "${workdir}"' EXIT
git clone --quiet --depth 1 "https://x-access-token:${GH_TOKEN}@github.com/${infra_repo}.git" "${workdir}/infra"
cd "${workdir}/infra"

if [[ "${version}" == "from-test" ]]; then
  test_file="product/test/apps/${service}.yml"
  if [[ ! -f "${test_file}" ]]; then
    echo "${service} is not deployed to test (${test_file} not found), nothing to promote" >&2
    exit 1
  fi
  version="$(sed -n 's/^ *targetRevision: *"\{0,1\}\([0-9.]*\)"\{0,1\} *$/\1/p' "${test_file}" | head -1)"
  if [[ ! "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "cannot read chart version from ${test_file}, got '${version}'" >&2
    exit 1
  fi
  echo "version taken from test: ${version}"
fi

if [[ ! -f "${app_file}" ]]; then
  echo "${service} is not deployed to ${env_name} (${app_file} not found), skipping"
  exit 0
fi

python3 - "${version}" "${app_file}" <<'EOF'
import re
import sys

version, path = sys.argv[1], sys.argv[2]
text = open(path).read()
text, changed = re.subn(
    r'(?m)^(\s*targetRevision:\s*).*$',
    lambda m: m.group(1) + '"%s"' % version,
    text,
    count=1,
)
if changed != 1:
    raise SystemExit("targetRevision not found in " + path)
# Закреплённый тег образа равен версии chart: обновляем его вместе с ней.
text = re.sub(
    r'(?m)^(\s*tag:\s*)"?\d+\.\d+\.\d+"?\s*$',
    lambda m: m.group(1) + '"%s"' % version,
    text,
    count=1,
)
open(path, 'w').write(text)
EOF

if git diff --quiet; then
  echo "${env_name} already points ${service} to ${version}"
  exit 0
fi
git diff
git config user.name "gaba-ci"
git config user.email "gaba-ci@users.noreply.github.com"
git commit --quiet -am "deploy(${service}): ${env_name} ${version}"
# В infra пишут сборки разных сервисов: при отказе push подтягиваем и повторяем.
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if git pull --quiet --rebase && git push --quiet; then
    echo "${env_name} ${service} -> ${version}"
    exit 0
  fi
  echo "push attempt ${attempt} failed, retrying"
  sleep $((attempt * 3))
done
exit 1
