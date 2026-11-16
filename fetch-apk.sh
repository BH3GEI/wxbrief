#!/bin/sh
# Bundle the Alpine packages the app needs (python3, ffmpeg, zstd-libs, tzdata, gdb and everything they depend on)
# into content/apk, so the container installs them from the package itself and never downloads anything at run time.
# apk verifies each file's signature against the keys that ship with the image.
#
# The first run needs docker (any machine; it only downloads). The files are kept in vendor-src/apk, so later
# builds do not need docker. To pick up newer packages, delete vendor-src/apk and build again.
set -eu
cd "$(dirname "$0")"
ALPINE=3.20.3        # the release of the Lazycat base image
PKGS="python3 ffmpeg zstd-libs tzdata gdb"
if ! ls vendor-src/apk/*.apk >/dev/null 2>&1; then
  command -v docker >/dev/null || { echo "docker is needed once to download the Alpine packages (vendor-src/apk is empty)" >&2; exit 1; }
  mkdir -p vendor-src/apk
  docker run --rm --platform linux/amd64 -v "$PWD/vendor-src/apk:/out" alpine:$ALPINE sh -c \
    "apk update -q; apk fetch -q -o /out \$(apk add --simulate $PKGS | sed -n 's/.*Installing \([^ ]*\) .*/\1/p')"
fi
rm -rf content/apk && mkdir -p content/apk && cp vendor-src/apk/*.apk content/apk/
