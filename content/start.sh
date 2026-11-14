#!/bin/sh
# Install python3, gdb, zstd and ffmpeg (for wxgf images) once; packages are cached under /lzcapp/cache so restarts work offline.
C=/lzcapp/cache/apk
mkdir -p "$C"
printf 'http://mirrors.aliyun.com/alpine/v3.20/main\nhttp://mirrors.aliyun.com/alpine/v3.20/community\n' > /etc/apk/repositories
ln -sfn "$C" /etc/apk/cache
until command -v python3 >/dev/null && command -v gdb >/dev/null && command -v ffmpeg >/dev/null; do
  apk add -q --no-network python3 gdb zstd-libs tzdata ffmpeg 2>/dev/null || { apk update -q && apk add -q python3 gdb zstd-libs tzdata ffmpeg; } || sleep 30
done
export VAR=/lzcapp/var WORK=/work WECHAT_DIR=/wechat/xwechat_files PORT=3000
exec python3 -u /lzcapp/pkg/content/app.py
