#!/usr/bin/env bash
# Tokenmin scrubber test runner. Pure stdlib Python; no pip install.
#
#   bash tests/run.sh
#
# This is what the GitHub Actions workflow invokes. Run it locally before
# pushing if you've touched anonymize.py / tokenmin.py.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "${here}/.." && pwd)"
cd "${repo}"

echo "tokenmin tests: starting"
echo "  repo: ${repo}"
echo "  python: $(python3 --version)"
echo

# Disable auto-update during tests so we don't hit the network.
export TOKENMIN_AUTOUPDATE="off"
export TOKENMIN_SALT_PATH="${repo}/tests/fixtures/test.salt"

python3 "${here}/test_scrubber.py"
echo
python3 "${here}/test_uninstall.py"
echo
echo "tokenmin tests: ok"
