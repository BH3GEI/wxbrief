#!/bin/sh
# Optional proxy for reaching the Telegram Bot API, so the box does not depend on a router's proxy.
# Off while $VAR/mihomo/sub_url is missing or empty; the settings page writes it (one line, a subscription URL,
# private: it never goes into this repo). With a URL, mihomo runs with one group that picks the node with the
# lowest delay to api.telegram.org (re-tested every 60 s) and listens on :7890 (SOCKS5 and HTTP) inside this
# app's network; its API on :9090 (secret in $D/secret) is only for the settings page's status.
# A changed or emptied sub_url is picked up within 5 s; mihomo is restarted if it exits.
D=/lzcapp/var/mihomo
BIN=/lzcapp/pkg/content/bin/mihomo
TEST=https://api.telegram.org/
mkdir -p "$D/providers"
chmod 700 "$D"
if [ ! -s "$D/secret" ]; then
  head -c 24 /dev/urandom | base64 | tr -d '/+=\n' > "$D/secret"
  chmod 600 "$D/secret"
fi

write_config() {
  cat > "$D/config.yaml" <<CFG
mixed-port: 7890
allow-lan: true
bind-address: '*'
mode: rule
log-level: warning
ipv6: false
external-controller: ':9090'
secret: '$(cat "$D/secret")'
dns:
  enable: true
  ipv6: false
  nameserver: [223.5.5.5, 119.29.29.29]
proxy-providers:
  sub:
    type: http
    url: "$1"
    path: ./providers/sub.txt
    interval: 21600
    health-check: {enable: true, url: "$TEST", interval: 60, timeout: 5000, lazy: false}
proxy-groups:
  - {name: fastest, type: url-test, use: [sub], url: "$TEST", interval: 60, tolerance: 50, timeout: 5000, lazy: false}
rules:
  - MATCH,fastest
CFG
  chmod 600 "$D/config.yaml"
}

PID=
start() { "$BIN" -d "$D" & PID=$!; }
stop() { if [ -n "$PID" ]; then kill "$PID" 2>/dev/null; wait "$PID" 2>/dev/null; PID=; fi; }

last=
while :; do
  url=$(head -n 1 "$D/sub_url" 2>/dev/null | tr -d '\r')
  if [ "$url" != "$last" ]; then
    stop
    # another subscription: keep the old node list aside instead of mixing it in
    [ -n "$last" ] && [ -f "$D/providers/sub.txt" ] && mv "$D/providers/sub.txt" "$D/providers/sub.txt.old"
    last=$url
    if [ -n "$url" ]; then write_config "$url"; start; echo "mihomo started" >&2; else echo "mihomo off" >&2; fi
  elif [ -n "$url" ] && ! kill -0 "$PID" 2>/dev/null; then
    echo "mihomo exited, restarting" >&2
    start
  fi
  sleep 5
done
