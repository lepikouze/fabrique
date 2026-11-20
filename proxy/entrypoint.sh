#!/bin/sh
# Proxy de sortie des jobs : liste blanche STRICTE (FilterDefaultDeny), noms d'hôtes ou adresses en expressions régulières
# séparées par des virgules dans PROXY_ALLOW. Tout le reste est refusé (réseau local compris).
# Derrière un proxy d'entreprise : PROXY_UPSTREAM=http://hôte:port envoie les requêtes autorisées à ce proxy au lieu de
# sortir en direct ; PROXY_NO_UPSTREAM liste les domaines internes à joindre sans lui (ex. « .exemple.org,depot.interne »).
set -eu
: "${PROXY_ALLOW:?PROXY_ALLOW manquant}"
conf=/tmp/tinyproxy.conf; filt=/tmp/filtre
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
  printf '%s' "$up" | grep -Eq '^[A-Za-z0-9._-]+:[0-9]{1,5}$' || { echo "PROXY_UPSTREAM invalide (attendu : http://hôte:port, sans identifiant)" >&2; exit 1; }
  echo "Upstream http $up" >> "$conf"
  printf '%s\n' "${PROXY_NO_UPSTREAM:-}" | tr ',' '\n' | sed 's/^ *//; s/ *$//' | grep -E '^[A-Za-z0-9._-]+$' | while read -r d; do
    echo "Upstream none \"$d\"" >> "$conf"
  done
  echo "proxy de sortie, en amont : $up"
fi
echo "proxy de sortie, autorisé :"; sed 's/^/  - /' "$filt"
exec tinyproxy -d -c "$conf"
