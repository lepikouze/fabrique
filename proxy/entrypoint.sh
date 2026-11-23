#!/bin/sh
# Jobs egress proxy: STRICT allowlist (FilterDefaultDeny), hostnames or addresses in regular expressions
# separated by commas in PROXY_ALLOW. Everything else is refused (including local network).
# Behind an enterprise proxy: PROXY_UPSTREAM=http://host:port sends authorized requests to this proxy instead of
# going out directly; PROXY_NO_UPSTREAM lists the internal domains to reach without it (e.g. ".example.org,git.internal").
set -eu
: "${PROXY_ALLOW:?PROXY_ALLOW missing}"
conf=/tmp/tinyproxy.conf; filt=/tmp/filter
printf '%s\n' "$PROXY_ALLOW" | tr ',' '\n' | sed 's/^ *//; s/ *$//' | grep -v '^$' > "$filt"
cat > "$conf" <<CONF
Port 3128
Listen 0.0.0.0
Timeout 600
MaxClients 100
LogLevel Info
Syslog Off
DisableViaHeader Yes
Allow 0.0.0.0/0
Filter "$filt"
FilterURLs Off
FilterExtended On
FilterCaseSensitive Off
FilterDefaultDeny Yes
ConnectPort 443
CONF
up=$(printf '%s' "${PROXY_UPSTREAM:-}" | sed -E 's#^[a-zA-Z]+://##; s#/+$##')
if [ -n "$up" ]; then
  printf '%s' "$up" | grep -Eq '^[A-Za-z0-9._-]+:[0-9]{1,5}$' || { echo "invalid PROXY_UPSTREAM (expected: http://host:port, without credentials)" >&2; exit 1; }
  echo "Upstream http $up" >> "$conf"
  printf '%s\n' "${PROXY_NO_UPSTREAM:-}" | tr ',' '\n' | sed 's/^ *//; s/ *$//' | grep -E '^[A-Za-z0-9._-]+$' | while read -r d; do
    echo "Upstream none \"$d\"" >> "$conf"
  done
  echo "egress proxy, upstream: $up"
fi
echo "egress proxy, allowed:"; sed 's/^/  - /' "$filt"
exec tinyproxy -d -c "$conf"
