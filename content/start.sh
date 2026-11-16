#!/bin/sh
# usage: start.sh worker|capture
# Everything the app needs ships inside this package (content/apk: signed Alpine packages, fetched at build
# time by fetch-apk.sh). Nothing is downloaded at run time; apk checks every file's signature against the
# keys that come with the image and refuses anything that does not match.
ROLE=${1:-worker}
if ! command -v python3 >/dev/null; then
  apk add -q --no-network --no-cache /lzcapp/pkg/content/apk/*.apk || { echo "cannot install the bundled packages" >&2; exit 1; }
fi
export VAR=/lzcapp/var WORK=/work WECHAT_DIR=/wechat/xwechat_files PORT=3000
if [ "$ROLE" = capture ]; then exec python3 -u /lzcapp/pkg/content/capturer.py; fi
export CAPTURE_REMOTE=1
exec python3 -u /lzcapp/pkg/content/app.py
