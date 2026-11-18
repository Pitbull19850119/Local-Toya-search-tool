#!/bin/bash
# Bindet beide Freigaben ein. Pfad-Format: //server/freigabe/unter/ordner
O=/data/options.json
get() { python3 -c "import json;print(json.load(open('$O'))['$1'])"; }
CRED=/tmp/cred
printf 'username=%s\npassword=%s\n' "$(get smb_user)" "$(get smb_password)" > $CRED; chmod 600 $CRED
mount_one() {  # $1=//server/share/sub  $2=mountpoint
  local share; share=$(echo "$1" | cut -d/ -f1-4)
  mkdir -p "$2"
  for V in 3.0 2.1 2.0 1.0; do
    mount -t cifs "$share" "$2" -o "credentials=$CRED,vers=$V,uid=0,gid=0,iocharset=utf8,nobrl" 2>/dev/null && { echo "$share eingebunden (SMB $V)"; return 0; }
  done
  echo "FEHLER: $share konnte nicht eingebunden werden"; return 1
}
A=$(get ordner_a); B=$(get ordner_b)
SA=$(echo "$A" | cut -d/ -f1-4); SB=$(echo "$B" | cut -d/ -f1-4)
mount_one "$A" /mnt/a
if [ "$SA" = "$SB" ]; then rmdir /mnt/b 2>/dev/null; ln -sfn /mnt/a /mnt/b; else mount_one "$B" /mnt/b; fi
rm -f $CRED
exec python3 /app/main.py
