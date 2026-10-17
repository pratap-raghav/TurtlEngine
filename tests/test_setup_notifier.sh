#!/usr/bin/env bash
#
# Item 5 (Repeated setup): re-running setup-notifier.sh preserves the secret
# env file and does not re-seed or duplicate configuration. Also covers the
# interactive credential flow: skipped prompts save nothing, and the overwrite
# guard preserves working credentials when you decline to replace them.
#
# Needs root-like privileges for chmod/ownership. If not root, it re-execs
# itself under fakeroot when available; otherwise it SKIPS (exit 77). All real
# system paths are redirected to a throwaway dir and systemctl is stubbed, so
# nothing on the host is touched. Every setup-notifier.sh invocation gets its
# stdin fed explicitly, so the interactive reads never block the suite.

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

# Run setup-notifier.sh with an explicit stdin and an explicit ENV_PATH, so the
# interactive prompts are driven deterministically and never block.
run_setup() { # run_setup <env-path> <stdin-string>
    printf '%s' "$2" | ENV_PATH="$1" "${REPO}/scripts/setup-notifier.sh" testuser >/dev/null 2>&1
}

sha() { sha256sum "$1" | awk '{print $1}'; }

# --- First run: fresh install (Enter at the token prompt -> skip) -----------
run_setup "${ENV_PATH}" '' || fail "fresh install exited non-zero"

[[ -f "${INSTALL_DIR}/notify.py" ]] || fail "notify.py was not installed"
[[ -f "${ENV_PATH}" ]]              || fail "env file was not created"
[[ -f "${UNIT_PATH}" ]]            || fail "unit file was not installed"
grep -q '^SSH_USER=testuser$' "${ENV_PATH}" || fail "SSH_USER was not pre-filled"

perms="$(stat -c '%a' "${ENV_PATH}")"
[[ "${perms}" == "600" ]] || fail "env file perms are ${perms}, expected 600"

# --- Inject a secret, then re-run (idempotency) -----------------------------
echo "TELEGRAM_BOT_TOKEN=REAL_SECRET_DO_NOT_CLOBBER" >> "${ENV_PATH}"
before="$(sha "${ENV_PATH}")"

run_setup "${ENV_PATH}" '' || fail "re-run exited non-zero"

after="$(sha "${ENV_PATH}")"
[[ "${before}" == "${after}" ]] || fail "re-run modified the env file (secret clobbered / re-seeded)"
grep -q '^TELEGRAM_BOT_TOKEN=REAL_SECRET_DO_NOT_CLOBBER$' "${ENV_PATH}" \
    || fail "the secret did not survive the second run"

# The unit path is fixed, so it cannot be duplicated; confirm it is still one
# well-formed file after the second run.
[[ -f "${UNIT_PATH}" ]] || fail "unit file missing after re-run"

# --- Scenario A: skip at the token prompt saves nothing ---------------------
ENV_A="${TMP}/etc/env-a"
run_setup "${ENV_A}" $'\n' || fail "A: exited non-zero when the token prompt was skipped"
grep -q 'ExampleToken' "${ENV_A}" || fail "A: placeholder token was unexpectedly changed"
grep -q 'MyRealToken\|T0KEN' "${ENV_A}" && fail "A: credentials were written despite skipping"

# --- Scenario B: token entered but chat id skipped -> token NOT saved -------
ENV_B="${TMP}/etc/env-b"
run_setup "${ENV_B}" $'T0KEN-NOT-SAVED\n\n' || fail "B: exited non-zero when the chat id was skipped"
grep -q 'T0KEN-NOT-SAVED' "${ENV_B}" && fail "B: token was saved even though the chat id was skipped"

# --- Scenario C: decline overwrite -> working credentials preserved ---------
ENV_C="${TMP}/etc/env-c"
cat > "${ENV_C}" <<'EOF'
TELEGRAM_BOT_TOKEN=111222333:RealWorkingTokenValue
TELEGRAM_CHAT_ID=987654321
SSH_USER=testuser
TMUX_SESSION=main
EOF
c_before="$(sha "${ENV_C}")"
run_setup "${ENV_C}" $'n\n' || fail "C: exited non-zero when declining overwrite"
c_after="$(sha "${ENV_C}")"
[[ "${c_before}" == "${c_after}" ]] || fail "C: declining overwrite modified the env file"
grep -q '^TELEGRAM_BOT_TOKEN=111222333:RealWorkingTokenValue$' "${ENV_C}" \
    || fail "C: working token was not preserved"

# --- Scenario D: full entry saves; a later run then guards the overwrite ----
ENV_D="${TMP}/etc/env-d"
run_setup "${ENV_D}" $'123:MyRealToken\n5555555\nn\n' || fail "D: exited non-zero on full entry"
grep -q '^TELEGRAM_BOT_TOKEN=123:MyRealToken$' "${ENV_D}" || fail "D: token was not saved"
grep -q '^TELEGRAM_CHAT_ID=5555555$' "${ENV_D}"         || fail "D: chat id was not saved"
d_perms="$(stat -c '%a' "${ENV_D}")"
[[ "${d_perms}" == "600" ]] || fail "D: env perms are ${d_perms}, expected 600"
d_before="$(sha "${ENV_D}")"
run_setup "${ENV_D}" $'n\n' || fail "D: exited non-zero declining the later overwrite"
d_after="$(sha "${ENV_D}")"
[[ "${d_before}" == "${d_after}" ]] || fail "D: declining overwrite changed saved credentials"

echo "PASS: setup-notifier is idempotent, skips save nothing, overwrite guard holds."
