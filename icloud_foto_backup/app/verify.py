"""Prüfung vor dem Leeren des Papierkorbs.

1. Jedes iCloud-Album: Liegt jedes Foto/Video mit genau der Größe aus iCloud
   im Ordner iCloud/<Album>/? (Fehlend oder abweichende Größe = Problem.)
2. Fotos ohne Album: dasselbe für iCloud/_Ohne Album/<Jahr>/.
3. Papierkorb: Jede Datei in _Papierkorb/ muss als vollständig identische Kopie
   (SHA-256 über die ganze Datei) unter iCloud/ liegen. Dateien ohne identische
   Kopie werden an ihren alten Platz ZURÜCKGELEGT. Es wird nichts gelöscht.

Ergebnis: Fotos/Pruefbericht.txt und Zeilen "PRUEF ..." für das Log.
Läuft als eigener Prozess, gestartet von main.py.
"""
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime

from icloudpd.authentication import authenticator
from icloudpd.mfa_provider import MFAProvider
from icloudpd.status import StatusExchange
from pyicloud_ipd.services.photos import PhotoLibrary
from pyicloud_ipd.version_size import AssetVersionSize, LivePhotoVersionSize

OPT = json.load(open("/data/options.json"))
WD_ROOT = sys.argv[1]
ICLOUD_ROOT = os.path.join(WD_ROOT, "iCloud")
TRASH_ROOT = os.path.join(WD_ROOT, "_Papierkorb")
OHNE = os.path.join(ICLOUD_ROOT, "_Ohne Album")
REPORT = os.path.join(WD_ROOT, "Pruefbericht.txt")

logging.basicConfig(level=logging.WARNING, format="%(message)s", stream=sys.stdout)
quiet = logging.getLogger("verify")
quiet.setLevel(logging.WARNING)


def say(msg):
    print(f"PRUEF {msg}", flush=True)


def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip().strip(".")
    return name or "Unbenannt"


def ascii_stem(filename):
    return re.sub(r"[^\x00-\x7f]", "", os.path.splitext(filename)[0]).lower()


def folder_index(folder, cache):
    """size -> [stem] für alle Dateien eines Ordners (zwischengespeichert)."""
    if folder not in cache:
        idx = {}
        if os.path.isdir(folder):
            for f in os.listdir(folder):
                full = os.path.join(folder, f)
                if os.path.isfile(full):
                    idx.setdefault(os.path.getsize(full), []).append(
                        os.path.splitext(f)[0].lower())
        cache[folder] = idx
    return cache[folder]


def present(folder, filename, size, cache):
    """Datei mit genau dieser Größe und passendem Namen im Ordner?"""
    want = ascii_stem(filename)
    return any(s.startswith(want) for s in folder_index(folder, cache).get(size, []))


def expected(photo):
    """[(Dateiname, Größe)] für Original und ggf. Live-Photo-Video."""
    out = []
    name = photo.filename
    v = photo.versions.get(AssetVersionSize.ORIGINAL)
    if v:
        out.append((name, v.size))
    lv = photo.versions.get(LivePhotoVersionSize.ORIGINAL)
    if lv:
        out.append((os.path.splitext(name)[0] + ".MOV", lv.size))
    return out


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    lines = [f"Prüfbericht iCloud-Foto-Backup – {datetime.now():%d.%m.%Y %H:%M}", ""]
    pw = OPT["apple_password"]
    icloud = authenticator(
        quiet, "com",
        {"parameter": (lambda _u: pw, lambda _u, _p: None)},
        MFAProvider.CONSOLE, StatusExchange(), OPT["apple_id"], lambda: None,
        None, "/data/cookies", os.environ.get("CLIENT_ID"), False)
    photos = icloud.photos
    smart = set(PhotoLibrary.SMART_FOLDERS)
    excl = {a.lower() for a in OPT.get("exclude_albums", [])}
    albums = {n: a for n, a in photos.albums.items() if n not in smart and n.lower() not in excl}
    say(f"1/3 Alben: prüfe {len(albums)} Alben gegen iCloud …")

    cache = {}
    in_album = set()
    total = ok = 0
    problems = []
    for n, (name, album) in enumerate(sorted(albums.items()), 1):
        folder = os.path.join(ICLOUD_ROOT, safe_name(name))
        missing = []
        for p in album:
            in_album.add(p.id)
            for fname, size in expected(p):
                total += 1
                if present(folder, fname, size, cache):
                    ok += 1
                else:
                    missing.append(f"{fname} ({size} Bytes)")
        if missing:
            problems.append((name, missing))
            say(f"   „{name}“: {len(missing)} fehlen oder unvollständig")
        if n % 50 == 0:
            say(f"   {n}/{len(albums)} Alben geprüft …")
    lines.append(f"ALBEN: {len(albums)} Alben, {total} Dateien erwartet, {ok} vollständig vorhanden.")
    for name, missing in problems:
        lines.append(f"  ✗ {name}: {len(missing)} fehlen/unvollständig")
        lines += [f"      {m}" for m in missing[:200]]
    if not problems:
        lines.append("  ✓ Alle Alben vollständig.")
    say(f"   Alben: {ok}/{total} Dateien vollständig, {len(problems)} Alben mit Lücken.")

    say("2/3 Fotos ohne Album …")
    o_total = o_ok = 0
    o_missing = []
    for p in photos.all:
        if p.id in in_album:
            continue
        folder = os.path.join(OHNE, str(p.created.year))
        for fname, size in expected(p):
            o_total += 1
            if present(folder, fname, size, cache):
                o_ok += 1
            else:
                o_missing.append(f"{p.created.year}/{fname} ({size} Bytes)")
    lines += ["", f"OHNE ALBUM: {o_total} Dateien erwartet, {o_ok} vollständig vorhanden."]
    lines += [f"  ✗ {m}" for m in o_missing[:500]]
    if len(o_missing) > 500:
        lines.append(f"  … und {len(o_missing) - 500} weitere")
    say(f"   Ohne Album: {o_ok}/{o_total} vollständig.")

    say("3/3 Papierkorb: jede Datei mit identischer Kopie (SHA-256) in iCloud/ abgleichen …")
    new_idx = {}
    for root, _d, files in os.walk(ICLOUD_ROOT):
        for f in files:
            full = os.path.join(root, f)
            try:
                new_idx.setdefault((f.lower(), os.path.getsize(full)), []).append(full)
            except OSError:
                pass
    t_total = t_ok = 0
    restored, kept = [], []
    hashes = {}
    for root, _d, files in os.walk(TRASH_ROOT):
        for f in files:
            old = os.path.join(root, f)
            t_total += 1
            size = os.path.getsize(old)
            clean = re.sub(r"\.\d{10}$", "", f)  # Zeitstempel-Endung beim Verschieben
            match = False
            h_old = None
            for cand in new_idx.get((clean.lower(), size), []):
                h_old = h_old or sha256(old)
                if cand not in hashes:
                    hashes[cand] = sha256(cand)
                if hashes[cand] == h_old:
                    match = True
                    break
            if match:
                t_ok += 1
                continue
            rel = os.path.relpath(os.path.join(root, clean), TRASH_ROOT)
            back = os.path.join(WD_ROOT, rel)
            if os.path.exists(back):
                kept.append(f"{rel} (alter Platz belegt – bleibt im Papierkorb)")
            else:
                os.makedirs(os.path.dirname(back), exist_ok=True)
                os.rename(old, back)  # zurücklegen, nicht löschen
                restored.append(rel)
            if t_total % 500 == 0:
                say(f"   {t_total} Dateien geprüft …")
    lines += ["", f"PAPIERKORB: {t_total} Dateien, {t_ok} haben eine identische Kopie in iCloud/."]
    if restored:
        lines.append(f"  ↩ {len(restored)} ohne identische Kopie an den alten Platz zurückgelegt:")
        lines += [f"      {r}" for r in restored[:500]]
    if kept:
        lines.append(f"  ! {len(kept)} ohne Kopie, alter Platz belegt (im Papierkorb gelassen):")
        lines += [f"      {k}" for k in kept[:500]]
    if not t_total:
        verdict = "Papierkorb ist leer."
    elif kept:
        verdict = ("Papierkorb NICHT leeren – er enthält Dateien ohne identische Kopie "
                   "(siehe Liste oben).")
    elif restored:
        verdict = ("Dateien ohne Kopie wurden zurückgelegt. Der Papierkorb enthält jetzt nur "
                   "noch Dateien mit identischer Kopie in iCloud/ und kann geleert werden.")
    else:
        verdict = "Papierkorb kann gefahrlos geleert werden – jede Datei liegt identisch in iCloud/."
    lines += ["", "ERGEBNIS: " + verdict]
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    say(f"   Papierkorb: {t_ok}/{t_total} identisch in iCloud/, "
        f"{len(restored)} zurückgelegt, {len(kept)} im Papierkorb gelassen.")
    say(f"FERTIG: Bericht unter {os.path.relpath(REPORT, os.path.dirname(WD_ROOT))}")


if __name__ == "__main__":
    try:
        main()
    except BaseException as ex:  # noqa: BLE001
        say(f"FEHLER: {type(ex).__name__}: {ex}")
        sys.exit(1)
