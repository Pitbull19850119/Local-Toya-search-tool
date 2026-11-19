#!/bin/bash
# Startet Tailscale (ohne TUN, userspace) und gibt Jellyfin per Funnel frei.
O=/data/options.json
ZIEL=$(jq -r .ziel $O); NAME=$(jq -r .geraetename $O); KEY=$(jq -r '.authkey // ""' $O)
SOCK=/tmp/ts.sock
TS="tailscale --socket=$SOCK"
STATUS=/tmp/status.txt
say() { echo "$(date '+%H:%M:%S')  $*" | tee -a $STATUS; }

python3 /status.py &

tailscaled --tun=userspace-networking --state=/data/tailscaled.state \
           --socket=$SOCK --statedir=/data >/tmp/tailscaled.log 2>&1 &
sleep 3

if [ -n "$KEY" ]; then
  $TS up --hostname="$NAME" --authkey="$KEY" --accept-dns=false
else
  $TS up --hostname="$NAME" --accept-dns=false --timeout=5s >/tmp/up.log 2>&1 || true
  if ! $TS status >/dev/null 2>&1; then
    ( $TS up --hostname="$NAME" --accept-dns=false >/tmp/up.log 2>&1 & )
    for i in $(seq 1 30); do
      URL=$(grep -o 'https://login.tailscale.com/[^ ]*' /tmp/up.log | head -1)
      [ -n "$URL" ] && break; sleep 1
    done
    [ -n "$URL" ] && echo "$URL" > /tmp/login_url && say "ANMELDUNG NÖTIG: $URL"
  fi
fi

until $TS status >/dev/null 2>&1; do sleep 5; done
rm -f /tmp/login_url
DNS=$($TS status --json | jq -r '.Self.DNSName' | sed 's/\.$//')
say "Bei Tailscale angemeldet als $DNS"

until $TS funnel --bg --https=443 "$ZIEL" >/tmp/funnel.log 2>&1; do
  say "Funnel noch nicht erlaubt: $(tail -n 3 /tmp/funnel.log | tr '\n' ' ')"
  say "→ In der Tailscale-Verwaltung HTTPS-Zertifikate und Funnel erlauben (siehe Anleitung). Neuer Versuch in 30 s."
  sleep 30
done
echo "https://$DNS" > /tmp/public_url
say "FERTIG – Jellyfin öffentlich erreichbar unter: https://$DNS"
wait
