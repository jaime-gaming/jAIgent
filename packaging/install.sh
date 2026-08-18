#!/usr/bin/env sh
# jaigent installer for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/jaime-gaming/jaigent/main/packaging/install.sh | sh
#
# Downloads the standalone binary for this platform from the latest GitHub
# release, verifies its checksum, and installs it somewhere on your PATH.
# No Python required.
#
# Environment:
#   JAIGENT_VERSION   install a specific tag instead of the latest
#   JAIGENT_BIN_DIR   install location (default: ~/.local/bin)

set -eu

REPO="jaime-gaming/jaigent"
BIN_DIR="${JAIGENT_BIN_DIR:-$HOME/.local/bin}"

red() { printf '\033[31m%s\033[0m\n' "$1"; }
green() { printf '\033[32m%s\033[0m\n' "$1"; }
dim() { printf '\033[2m%s\033[0m\n' "$1"; }

die() {
  red "error: $1"
  exit 1
}

need() {
  command -v "$1" >/dev/null 2>&1 || die "$1 is required but not installed."
}

need uname
if command -v curl >/dev/null 2>&1; then
  fetch() { curl -fsSL "$1" -o "$2"; }
  read_url() { curl -fsSL "$1"; }
elif command -v wget >/dev/null 2>&1; then
  fetch() { wget -qO "$2" "$1"; }
  read_url() { wget -qO- "$1"; }
else
  die "curl or wget is required."
fi

# ---------------------------------------------------------------- platform
os="$(uname -s)"
arch="$(uname -m)"

case "$os" in
  Darwin) platform="macos" ;;
  Linux) platform="linux" ;;
  *) die "unsupported operating system: $os. Install from source instead: pip install jaigent" ;;
esac

case "$arch" in
  x86_64 | amd64) arch="x64" ;;
  arm64 | aarch64) arch="arm64" ;;
  *) die "unsupported architecture: $arch. Install from source instead: pip install jaigent" ;;
esac

# ----------------------------------------------------------------- version
if [ -n "${JAIGENT_VERSION:-}" ]; then
  version="$JAIGENT_VERSION"
else
  dim "Looking up the latest release..."
  version="$(read_url "https://api.github.com/repos/$REPO/releases/latest" |
    sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n 1)"
  [ -n "$version" ] || die "could not determine the latest version. Set JAIGENT_VERSION."
fi

asset="jaigent-$platform-$arch"
url="https://github.com/$REPO/releases/download/$version/$asset.tar.gz"

printf '\n'
dim "  version   $version"
dim "  platform  $platform-$arch"
dim "  target    $BIN_DIR/jaigent"
printf '\n'

# ---------------------------------------------------------------- download
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT INT TERM

dim "Downloading $asset..."
fetch "$url" "$tmp/jaigent.tar.gz" || die "download failed: $url"

# Verify the checksum when the release publishes one. A tampered binary is a
# far worse outcome than a failed install, so a mismatch is always fatal.
if read_url "https://github.com/$REPO/releases/download/$version/checksums.txt" \
  >"$tmp/checksums.txt" 2>/dev/null && [ -s "$tmp/checksums.txt" ]; then
  expected="$(grep " $asset.tar.gz\$" "$tmp/checksums.txt" | awk '{print $1}' | head -n 1)"
  if [ -n "$expected" ]; then
    if command -v sha256sum >/dev/null 2>&1; then
      actual="$(sha256sum "$tmp/jaigent.tar.gz" | awk '{print $1}')"
    elif command -v shasum >/dev/null 2>&1; then
      actual="$(shasum -a 256 "$tmp/jaigent.tar.gz" | awk '{print $1}')"
    else
      actual=""
    fi
    if [ -n "$actual" ]; then
      [ "$actual" = "$expected" ] || die "checksum mismatch — refusing to install."
      green "  checksum verified"
    fi
  fi
fi

tar -xzf "$tmp/jaigent.tar.gz" -C "$tmp" || die "could not extract the archive."
[ -f "$tmp/jaigent" ] || die "the archive did not contain a jaigent binary."

mkdir -p "$BIN_DIR"
chmod +x "$tmp/jaigent"
mv "$tmp/jaigent" "$BIN_DIR/jaigent"

# macOS quarantines downloads; clear it so Gatekeeper does not block the binary.
if [ "$platform" = "macos" ] && command -v xattr >/dev/null 2>&1; then
  xattr -d com.apple.quarantine "$BIN_DIR/jaigent" 2>/dev/null || true
fi

green "Installed jaigent $version to $BIN_DIR/jaigent"

# ------------------------------------------------------------------- PATH
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    printf '\n'
    red "$BIN_DIR is not on your PATH."
    dim "Add this to your shell profile:"
    printf '\n    export PATH="%s:$PATH"\n\n' "$BIN_DIR"
    ;;
esac

printf '\nNext:  '
green "jaigent init"
