#!/bin/sh
# Bundle the mihomo binary for the optional Telegram proxy service (see content/proxy.sh).
# Pinned by version and SHA256; a mismatch aborts the build. Nothing is downloaded at run time.
set -eu
cd "$(dirname "$0")"
V=v1.19.33
F=mihomo-linux-amd64-compatible-$V.gz
SHA=386508bfb1c72c43b6a0eda73708ecdce82546581b0ebcb16366c53757c622b6
mkdir -p vendor-src content/bin
[ -f "vendor-src/$F" ] || curl -fsSL -o "vendor-src/$F" "https://github.com/MetaCubeX/mihomo/releases/download/$V/$F"
echo "$SHA  vendor-src/$F" | shasum -a 256 -c -
gunzip -c "vendor-src/$F" > content/bin/mihomo
chmod 755 content/bin/mihomo
