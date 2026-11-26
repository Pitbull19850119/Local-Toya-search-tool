#!/bin/bash
# Platten-Werkzeug: sicher formatieren (nur Platte mit passender Seriennummer, >= 4 TB,
# nicht eingebunden, nicht die Systemplatte) und Medien 1:1 kopieren (Quelle nur lesend).
set -u
O=/data/options.json
opt() { sed -n "s/.*\"$1\": *\"\([^\"]*\)\".*/\1/p" "$O"; }
AKTION=$(opt aktion); SERIAL=$(opt ziel_seriennummer); ZNAME=$(opt ziel_name); QNAME=$(opt quelle_name)
log() { echo "[$(date '+%d.%m. %H:%M:%S')] $*"; }

log "Platten-Werkzeug – Aktion: $AKTION"
log "Gefundene Platten:"
lsblk -o NAME,SIZE,FSTYPE,LABEL,SERIAL,MODEL,MOUNTPOINT | sed 's/^/    /'

find_target() {
  [ -n "$SERIAL" ] || { log "FEHLER: ziel_seriennummer ist leer – breche ab."; return 1; }
  local dev
  dev=$(lsblk -dn -o NAME,SERIAL | awk -v s="$SERIAL" '$2==s{print $1}')
  [ -n "$dev" ] || { log "FEHLER: keine Platte mit Seriennummer $SERIAL gefunden."; return 1; }
  [ "$(echo "$dev" | wc -l)" = "1" ] || { log "FEHLER: Seriennummer nicht eindeutig."; return 1; }
  local bytes; bytes=$(lsblk -dbn -o SIZE "/dev/$dev")
  if [ "$bytes" -lt 4000000000000 ]; then log "FEHLER: /dev/$dev ist kleiner als 4 TB – breche ab."; return 1; fi
  if lsblk -n -o MOUNTPOINT "/dev/$dev" | grep -q .; then log "FEHLER: /dev/$dev ist eingebunden – breche ab."; return 1; fi
  if lsblk -n -o LABEL "/dev/$dev" | grep -qE '^hassos-|^hassos'; then log "FEHLER: /dev/$dev ist die Systemplatte – breche ab."; return 1; fi
  TARGET=/dev/$dev
}

do_format() {
  find_target || return 1
  log "Ziel: $TARGET ($(lsblk -dn -o SIZE,MODEL,SERIAL $TARGET)) – Inhalt vorher:"
  lsblk -o NAME,SIZE,FSTYPE,LABEL $TARGET | sed 's/^/    /'
  log "Formatiere $TARGET als ext4, Name \"$ZNAME\" …"
  wipefs -a "$TARGET" >/dev/null
  parted -s "$TARGET" mklabel gpt mkpart "$ZNAME" ext4 1MiB 100% || { log "FEHLER: Partitionieren fehlgeschlagen"; return 1; }
  partprobe "$TARGET" 2>/dev/null; sleep 3
  PART=$(lsblk -ln -o NAME,TYPE "$TARGET" | awk '$2=="part"{print "/dev/"$1; exit}')
  [ -b "$PART" ] || { log "FEHLER: Partition nicht gefunden (Neustart der App hilft)"; return 1; }
  mkfs.ext4 -F -L "$ZNAME" -m 0 -T largefile -E lazy_itable_init=1,lazy_journal_init=1 "$PART" >/dev/null \
    || { log "FEHLER: mkfs.ext4 fehlgeschlagen"; return 1; }
  log "FERTIG: $PART ist ext4 mit Namen \"$ZNAME\"."
  lsblk -o NAME,SIZE,FSTYPE,LABEL $TARGET | sed 's/^/    /'
}

do_copy() {
  [ -n "$QNAME" ] || { log "FEHLER: quelle_name (Laufwerksname der Quelle) ist leer."; return 1; }
  local src dst
  src=$(lsblk -ln -o NAME,LABEL | awk -v l="$QNAME" '{n=$1; $1=""; sub(/^ /,""); if($0==l) print "/dev/"n}' | head -1)
  dst=$(lsblk -ln -o NAME,LABEL | awk -v l="$ZNAME" '{n=$1; $1=""; sub(/^ /,""); if($0==l) print "/dev/"n}' | head -1)
  [ -b "$src" ] || { log "FEHLER: Quelle \"$QNAME\" nicht gefunden."; return 1; }
  [ -b "$dst" ] || { log "FEHLER: Ziel \"$ZNAME\" nicht gefunden."; return 1; }
  mkdir -p /mnt/quelle /mnt/ziel
  mount -t ntfs3 -o ro "$src" /mnt/quelle 2>/dev/null || ntfs-3g -o ro "$src" /mnt/quelle 2>/dev/null \
    || mount -o ro "$src" /mnt/quelle || { log "FEHLER: Quelle konnte nicht (nur lesend) eingebunden werden."; return 1; }
  mount "$dst" /mnt/ziel || { log "FEHLER: Ziel konnte nicht eingebunden werden."; umount /mnt/quelle; return 1; }
  log "Quelle $src (\"$QNAME\", nur lesend) → Ziel $dst (\"$ZNAME\")"
  log "Belegt Quelle: $(du -sh /mnt/quelle 2>/dev/null | cut -f1) – frei Ziel: $(df -h --output=avail /mnt/ziel | tail -1)"
  rsync -rltD --times --no-perms --no-owner --no-group --info=progress2,stats2 --exclude='$RECYCLE.BIN' \
    --exclude='System Volume Information' /mnt/quelle/ /mnt/ziel/ 2>&1 | tr '\r' '\n' | awk 'NR%200==1 || /total|Number|bytes/'
  RC=${PIPESTATUS[0]}
  log "Kopie beendet (rsync-Code $RC). Kontrolle: Dateien und Größen beider Seiten vergleichen …"
  QN=$(find /mnt/quelle -type f ! -path '*/$RECYCLE.BIN/*' ! -path '*/System Volume Information/*' | wc -l)
  ZN=$(find /mnt/ziel -type f ! -path '/mnt/ziel/lost+found/*' | wc -l)
  QB=$(find /mnt/quelle -type f ! -path '*/$RECYCLE.BIN/*' ! -path '*/System Volume Information/*' -printf '%s\n' | awk '{s+=$1}END{print s+0}')
  ZB=$(find /mnt/ziel -type f ! -path '/mnt/ziel/lost+found/*' -printf '%s\n' | awk '{s+=$1}END{print s+0}')
  DIFF=$(rsync -rn --size-only --out-format='%n' --exclude='$RECYCLE.BIN' --exclude='System Volume Information' /mnt/quelle/ /mnt/ziel/ | grep -v '/$' | wc -l)
  log "Quelle: $QN Dateien, $QB Bytes | Ziel: $ZN Dateien, $ZB Bytes | fehlend/abweichend: $DIFF"
  if [ "$RC" = "0" ] && [ "$DIFF" = "0" ]; then log "FERTIG: Kopie vollständig und identisch (Anzahl + Größen)."; else log "WARNUNG: Kopie nicht vollständig – App erneut starten setzt fort."; fi
  sync; umount /mnt/ziel; umount /mnt/quelle
}

case "$AKTION" in
  pruefen) find_target && log "Ziel gefunden: $TARGET – nichts verändert (Aktion \"pruefen\")." ;;
  formatieren) do_format ;;
  kopieren) do_copy ;;
  formatieren_und_kopieren) do_format && do_copy ;;
esac
log "Ende."
