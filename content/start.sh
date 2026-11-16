#!/bin/sh
# usage: start.sh worker|capture. Packages are cached under /lzcapp/cache so restarts work offline.
ROLE=${1:-worker}
C=/lzcapp/cache/apk-$ROLE
mkdir -p "$C"
printf 'http://mirrors.aliyun.com/alpine/v3.20/main\nhttp://mirrors.aliyun.com/alpine/v3.20/community\n' > /etc/apk/repositories
ln -sfn "$C" /etc/apk/cache
if [ "$ROLE" = capture ]; then PKGS="python3 gdb tzdata"; NEED="python3 gdb"; else PKGS="python3 zstd-libs tzdata ffmpeg"; NEED="python3 ffmpeg"; fi
have() { for c in $NEED; do command -v $c >/dev/null || return 1; done; }
until have; do
  apk add -q --no-network $PKGS 2>/dev/null || { apk update -q && apk add -q $PKGS; } || sleep 30
done
export VAR=/lzcapp/var WORK=/work WECHAT_DIR=/wechat/xwechat_files PORT=3000
if [ "$ROLE" = capture ]; then exec python3 -u /lzcapp/pkg/content/capturer.py; fi
export CAPTURE_REMOTE=1
exec python3 -u /lzcapp/pkg/content/app.py
