#!/usr/bin/env bash
set -euo pipefail

chart_file="${1:?Chart.yaml path is required}"

current="$(grep '^version:' "${chart_file}" | awk '{print $2}' | tr -d '"')"
base="${current%%-*}"
IFS='.' read -r major minor patch <<< "${base}"

if ! [[ "${major}" =~ ^[0-9]+$ && "${minor}" =~ ^[0-9]+$ && "${patch}" =~ ^[0-9]+$ ]]; then
  echo "invalid chart version: ${current}" >&2
  exit 1
fi

patch=$((patch + 1))
new_version="${major}.${minor}.${patch}"

tmp="$(mktemp)"
sed "s/^version:.*/version: ${new_version}/" "${chart_file}" > "${tmp}"
mv "${tmp}" "${chart_file}"
echo "${new_version}"
