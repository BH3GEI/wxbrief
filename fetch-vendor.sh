#!/bin/sh
# Put the SILK decoder (silk-python, BSD) into content/vendor for the lzcapp image (Python 3.12, musl, x86_64).
set -eu
cd "$(dirname "$0")"
WHEEL=silk_python-0.2.9-cp312-cp312-musllinux_1_2_x86_64.whl
SHA256=c3fadf85bf67ce78e373a4088def9a80d455d2eb4d9909dc8909ee5f1d83eafb
[ -d content/vendor/pysilk ] && exit 0
mkdir -p vendor-src
if [ ! -f "vendor-src/$WHEEL" ]; then
  URL=$(curl -fsSL https://pypi.org/pypi/silk-python/0.2.9/json |
        python3 -c "import json,sys; print(next(u['url'] for u in json.load(sys.stdin)['urls'] if u['filename']=='$WHEEL'))")
  curl -fsSL -o "vendor-src/$WHEEL" "$URL"
fi
echo "$SHA256  vendor-src/$WHEEL" | shasum -a 256 -c -
rm -rf content/vendor && mkdir -p content/vendor
unzip -q "vendor-src/$WHEEL" -d content/vendor
