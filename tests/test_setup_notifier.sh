#!/usr/bin/env bash
#
# Item 5 (Repeated setup): re-running setup-notifier.sh preserves the secret
# env file and does not re-seed or duplicate configuration.
#
# Needs root-like privileges for chmod/ownership. If not root, it re-execs
# itself under fakeroot when available; otherwise it SKIPS (exit 77). All real
# system paths are redirected to a throwaway dir and systemctl is stubbed, so
# nothing on the host is touched.

set -euo pipefail

SKIP=77
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/.." && pwd)"

# Elevate to a (possibly fake) root so chmod/stat are self-consistent.
if [[ "${EUID}" -ne 0 && -z "${TURTLE_FAKEROOT:-}" ]]; then
    if command -v fakeroot >/dev/null 2>&1; then
        exec fakeroot env TURTLE_FAKEROOT=1 "$0" "$@"
    fi
    echo "SKIP: needs root or fakeroot for ownership/permission checks." >&2
    exit "${SKIP}"
fi

TMP="$(mktemp -d)"
cleanup() { rm -rf "${TMP}"; }
trap cleanup EXIT

# Stub systemctl so daemon-reload/enable are no-ops in the test.
mkdir -p "${TMP}/bin"
cat > "${TMP}/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "${TMP}/bin/systemctl"
export PATH="${TMP}/bin:${PATH}"

# Redirect every real path into the throwaway dir.
export INSTALL_DIR="${TMP}/opt/turtlengine/notifier"
export ENV_PATH="${TMP}/etc/nas-notifier.env"
export UNIT_PATH="${TMP}/etc/systemd/system/nas-notifier.service"
mkdir -p "${TMP}/etc"

fail() { echo "FAIL: $*" >&2; exit 1; }

# --- First run: fresh install ----------------------------------------------
"${REPO}/scripts/setup-notifier.sh" testuser >/dev/null

[[ -f "${INSTALL_DIR}/notify.py" ]] || fail "notify.py was not installed"
[[ -f "${ENV_PATH}" ]]              || fail "env file was not created"
[[ -f "${UNIT_PATH}" ]]            || fail "unit file was not installed"
grep -q '^SSH_USER=testuser$' "${ENV_PATH}" || fail "SSH_USER was not pre-filled"

perms="$(stat -c '%a' "${ENV_PATH}")"
[[ "${perms}" == "600" ]] || fail "env file perms are ${perms}, expected 600"

# --- Inject a secret, then re-run (idempotency) -----------------------------
echo "TELEGRAM_BOT_TOKEN=REAL_SECRET_DO_NOT_CLOBBER" >> "${ENV_PATH}"
before="$(sha256sum "${ENV_PATH}" | awk '{print $1}')"

"${REPO}/scripts/setup-notifier.sh" testuser >/dev/null

after="$(sha256sum "${ENV_PATH}" | awk '{print $1}')"
[[ "${before}" == "${after}" ]] || fail "re-run modified the env file (secret clobbered / re-seeded)"
grep -q '^TELEGRAM_BOT_TOKEN=REAL_SECRET_DO_NOT_CLOBBER$' "${ENV_PATH}" \
    || fail "the secret did not survive the second run"

# The unit path is fixed, so it cannot be duplicated; confirm it is still one
# well-formed file after the second run.
[[ -f "${UNIT_PATH}" ]] || fail "unit file missing after re-run"

echo "PASS: setup-notifier is idempotent and preserves the secret env file."
