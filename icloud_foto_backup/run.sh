#!/bin/bash
# Bindet die WD-Freigabe ein und startet die Backup-App.
OPTS=/data/options.json
SHARE=$(python3 -c "import json;print(json.load(open('$OPTS'))['wd_share'])")
WDUSER=$(python3 -c "import json;print(json.load(open('$OPTS'))['wd_user'])")
WDPASS=$(python3 -c "import json;print(json.load(open('$OPTS'))['wd_password'])")

mkdir -p /mnt/wd
CRED=/tmp/wdcred
printf 'username=%s\npassword=%s\n' "$WDUSER" "$WDPASS" > "$CRED"
chmod 600 "$CRED"

MOUNTED=0
for V in 3.0 2.1 2.0 1.0; do
  if mount -t cifs "$SHARE" /mnt/wd -o "credentials=$CRED,vers=$V,uid=0,gid=0,iocharset=utf8,file_mode=0664,dir_mode=0775,nobrl,soft,echo_interval=10"; then
    echo "WD-Freigabe $SHARE eingebunden (SMB $V)"
    MOUNTED=1
    break
  fi
done
rm -f "$CRED"
if [ "$MOUNTED" != "1" ]; then
  echo "FEHLER: WD-Freigabe $SHARE konnte nicht eingebunden werden. Freigabe, Benutzer und Passwort prüfen."
fi
export WD_MOUNTED=$MOUNTED
exec python3 /app/main.py
