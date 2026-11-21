#!/bin/sh
# Optional proxy for reaching the Telegram Bot API, so the box does not depend on a router's proxy.
# Off until $VAR/mihomo/sub_url exists: one line, a subscription URL (private; it never goes into this repo).
# Builds a mihomo config whose only group picks the node with the lowest delay to api.telegram.org and
# listens on :7890 (SOCKS5 and HTTP) inside this app's network. Then set the Telegram proxy on the settings
# page to socks5://proxy:7890.
D=/lzcapp/var/mihomo
while [ ! -s "$D/sub_url" ]; do sleep 60; done
mkdir -p "$D/providers"
URL=$(head -n 1 "$D/sub_url" | tr -d '\r')
TEST=https://api.telegram.org/
cat > "$D/config.yaml" <<CFG
mixed-port: 7890
allow-lan: true
bind-address: '*'
mode: rule
log-level: warning
ipv6: false
external-controller: 127.0.0.1:9090
dns:
  enable: true
  ipv6: false
  nameserver: [223.5.5.5, 119.29.29.29]
proxy-providers:
  sub:
    type: http
    url: "$URL"
    path: ./providers/sub.txt
    interval: 21600
    health-check: {enable: true, url: "$TEST", interval: 60, timeout: 5000, lazy: false}
proxy-groups:
  - {name: fastest, type: url-test, use: [sub], url: "$TEST", interval: 60, tolerance: 50, timeout: 5000, lazy: false}
rules:
  - MATCH,fastest
CFG
chmod 600 "$D/config.yaml" "$D/sub_url"
while :; do
  /lzcapp/pkg/content/bin/mihomo -d "$D"
  echo "mihomo exited ($?), restarting in 5 s" >&2
  sleep 5
done
