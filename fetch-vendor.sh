#!/bin/sh
# Put the pinned third-party Python packages into content/vendor for the lzcapp image (Python 3.12, musl, x86_64).
# Every file is pinned by version and SHA256; a mismatch aborts the build.
#   silk-python  voice decoding            telethon, rsa, pyasn1, pyaes, python-socks  Telegram client
#   segno        QR code for Telegram login
set -eu
cd "$(dirname "$0")"
# local test runs leave compiled caches (for another Python version) that must not ship in the package
find content -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
STAMP=content/vendor/.pinned-3
[ -f "$STAMP" ] && exit 0
mkdir -p vendor-src
rm -rf content/vendor && mkdir -p content/vendor

fetch() {  # fetch <filename> <sha256>  -> vendor-src/<filename>, verified
  f=$1; sha=$2
  if [ ! -f "vendor-src/$f" ]; then
    URL=$(curl -fsSL "https://pypi.org/pypi/$(python3 -c "import sys;n=sys.argv[1];print(n.split('-')[0].replace('_','-'))" "$f")/json" |
          python3 -c "import json,sys; print(next(u['url'] for u in json.load(sys.stdin)['releases'][sys.argv[1]] if u['filename']==sys.argv[2]))" "$V" "$f")
    curl -fsSL -o "vendor-src/$f" "$URL"
  fi
  echo "$sha  vendor-src/$f" | shasum -a 256 -c -
}

V=0.2.9; fetch silk_python-0.2.9-cp312-cp312-musllinux_1_2_x86_64.whl c3fadf85bf67ce78e373a4088def9a80d455d2eb4d9909dc8909ee5f1d83eafb
V=1.45.0; fetch telethon-1.45.0-py3-none-any.whl 339e24c83aedc9f12cbe389264627edc49779e4289c535aa90aafc4ce62f7972
V=2.8.2; fetch python_socks-2.8.2-py3-none-any.whl 7cf785d0631e0659384a773b3c402bc22cccdc23894ba1d65f8524748ace1193
V=4.9.1; fetch rsa-4.9.1-py3-none-any.whl 68635866661c6836b8d39430f97a996acbd61bfa49406748ea243539fe239762
V=0.6.1; fetch pyasn1-0.6.1-py3-none-any.whl 0d632f46f2ba09143da3a8afe9e33fb6f92fa2320ab7e886e2d0f7672af84629
V=1.6.6; fetch segno-1.6.6-py3-none-any.whl 28c7d081ed0cf935e0411293a465efd4d500704072cdb039778a2ab8736190c7
V=1.6.1; fetch pyaes-1.6.1.tar.gz 02c1b1405c38d3c370b085fb952dd8bea3fadcee6411ad99f312cc129c536d8f

unzip -q -o vendor-src/silk_python-0.2.9-cp312-cp312-musllinux_1_2_x86_64.whl -d content/vendor
for w in telethon-1.45.0-py3-none-any.whl python_socks-2.8.2-py3-none-any.whl rsa-4.9.1-py3-none-any.whl \
         pyasn1-0.6.1-py3-none-any.whl segno-1.6.6-py3-none-any.whl; do
  unzip -q -o "vendor-src/$w" -d content/vendor
done
# pyaes only ships a source archive (pure Python): take the package directory
T=$(mktemp -d); tar xzf vendor-src/pyaes-1.6.1.tar.gz -C "$T"
cp -R "$T/pyaes-1.6.1/pyaes" content/vendor/pyaes; rm -rf "$T"
touch "$STAMP"
