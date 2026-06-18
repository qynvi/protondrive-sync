#!/usr/bin/env bash
# Install the pinned Proton Drive CLI binary into vendor/proton-drive-cli/.
#
# The app's sync backend is validated against this exact CLI version.
# Upgrading requires bumping PROTON_CLI_VERSION *and* the checksums below,
# then re-running the live smoke test (scripts/smoke-proton-cli.py).
#
# Usage:
#   scripts/install-proton-cli.sh             # download + verify + install
#   scripts/install-proton-cli.sh --from FILE # install from a local copy (still verified)

set -euo pipefail

PROTON_CLI_VERSION="0.4.3"
BASE_URL="https://proton.me/download/drive/cli/${PROTON_CLI_VERSION}"

# SHA-512 checksums published at https://proton.me/download/drive/cli/index.html
SHA512_LINUX_X64="6430b087477587852eb1e44c6f9eebdf04a125fa4f66521f17acdd2ed3917a0f124a84fe56caa4242ec3a6f4a2330be4d04a7bba989359e3aefeb7cf0098a15a"
SHA512_LINUX_ARM64="0d7d5ee692f645b4dd92aa27e13eab4e9eefb9dda3e80ee9cba2a4ad75c141be898bee3b4503167b6cf17cf8ba08be4adb862be46ff670b60514359712596c30"
SHA512_DARWIN_X64="657693e24d4b4894ebe905d5fadc0834af048bf3b91d6408478a413f769326697029fbb76eb2ecfaea3b08ad6e79e0abe0f2b27a7413d17c56c497a7bbca984a"
SHA512_DARWIN_ARM64="1bce055e6edd7c004d4dc0da0da0535e96f680557f0196ca5c5b41f4873cc8187612a84cfbe74ef087f0bdb0e569c38b049e7c5b1a376e1ed3c4c5e5160c12a8"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
VENDOR_DIR="$REPO_ROOT/vendor/proton-drive-cli"
TARGET="$VENDOR_DIR/proton-drive"

FROM_FILE=""
if [[ "${1:-}" == "--from" ]]; then
    FROM_FILE="${2:?--from requires a file argument}"
elif [[ -n "${1:-}" ]]; then
    echo "Unknown argument: $1" >&2
    echo "Usage: $0 [--from FILE]" >&2
    exit 2
fi

case "$(uname -s)" in
    Linux)  os="linux" ;;
    Darwin) os="darwin" ;;
    *)
        echo "Unsupported OS: $(uname -s). Download manually from" >&2
        echo "https://proton.me/download/drive/cli/index.html and set proton_cli_path in config." >&2
        exit 1
        ;;
esac

case "$(uname -m)" in
    x86_64|amd64)  arch="x64" ;;
    aarch64|arm64) arch="arm64" ;;
    *)
        echo "Unsupported architecture: $(uname -m)" >&2
        exit 1
        ;;
esac

case "$os-$arch" in
    linux-x64)    expected="$SHA512_LINUX_X64" ;;
    linux-arm64)  expected="$SHA512_LINUX_ARM64" ;;
    darwin-x64)   expected="$SHA512_DARWIN_X64" ;;
    darwin-arm64) expected="$SHA512_DARWIN_ARM64" ;;
esac

sha512_of() {
    if command -v sha512sum >/dev/null 2>&1; then
        sha512sum "$1" | awk '{print $1}'
    else
        shasum -a 512 "$1" | awk '{print $1}'
    fi
}

# Already installed and intact?
if [[ -x "$TARGET" ]] && [[ "$(sha512_of "$TARGET")" == "$expected" ]]; then
    echo "Proton Drive CLI ${PROTON_CLI_VERSION} (${os}-${arch}) already installed: $TARGET"
    exit 0
fi

mkdir -p "$VENDOR_DIR"
tmp="$(mktemp "$VENDOR_DIR/.proton-drive.XXXXXX")"
trap 'rm -f "$tmp"' EXIT

if [[ -n "$FROM_FILE" ]]; then
    echo "Installing Proton Drive CLI from local file: $FROM_FILE"
    cp "$FROM_FILE" "$tmp"
else
    url="$BASE_URL/${os}-${arch}/proton-drive"
    echo "Downloading Proton Drive CLI ${PROTON_CLI_VERSION} (${os}-${arch}) ..."
    echo "  $url"
    if command -v curl >/dev/null 2>&1; then
        curl --fail --location --progress-bar -o "$tmp" "$url"
    elif command -v wget >/dev/null 2>&1; then
        wget -O "$tmp" "$url"
    else
        echo "Neither curl nor wget found." >&2
        exit 1
    fi
fi

actual="$(sha512_of "$tmp")"
if [[ "$actual" != "$expected" ]]; then
    echo "SHA-512 mismatch for ${os}-${arch} binary!" >&2
    echo "  expected: $expected" >&2
    echo "  actual:   $actual" >&2
    echo "Refusing to install." >&2
    exit 1
fi

chmod +x "$tmp"
mv "$tmp" "$TARGET"
trap - EXIT
echo "Installed: $TARGET"
"$TARGET" --version || true
