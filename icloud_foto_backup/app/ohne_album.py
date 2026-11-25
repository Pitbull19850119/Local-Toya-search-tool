"""Sichert alle iCloud-Fotos, die in KEINEM eigenen Album liegen,
nach Fotos/iCloud/_Ohne Album/<Jahr>/. Bereits vorhandene Dateien werden übersprungen.
Läuft als eigener Prozess, gestartet von main.py."""
import json
import logging
import os
import sys
from datetime import datetime

from icloudpd.authentication import authenticator
from icloudpd.download import download_media
from icloudpd.mfa_provider import MFAProvider
from icloudpd.status import StatusExchange
from pyicloud_ipd.services.photos import PhotoLibrary
from pyicloud_ipd.version_size import AssetVersionSize, LivePhotoVersionSize

OPT = json.load(open("/data/options.json"))
DEST = sys.argv[1]
END_HOUR = int(sys.argv[2]) if len(sys.argv) > 2 else -1   # -1 = ohne Zeitlimit
START_HOUR = int(sys.argv[3]) if len(sys.argv) > 3 else 0

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
log = logging.getLogger("ohne_album")


def in_window():
    if END_HOUR < 0:
        return True
    h = datetime.now().hour
    return h >= START_HOUR or h < END_HOUR if START_HOUR > END_HOUR else START_HOUR <= h < END_HOUR


def main():
    pw = OPT["apple_password"]
    icloud = authenticator(
        log, "com",
        {"parameter": (lambda _u: pw, lambda _u, _p: None)},
        MFAProvider.CONSOLE, StatusExchange(), OPT["apple_id"], lambda: None,
        None, "/data/cookies", os.environ.get("CLIENT_ID"), False)
    photos = icloud.photos
    smart = set(PhotoLibrary.SMART_FOLDERS)
    excl = {a.lower() for a in OPT.get("exclude_albums", [])}
    in_album = set()
    albums = photos.albums
    log.info(f"OHNE-ALBUM: prüfe {len(albums) - len(smart)} Alben …")
    for name, album in albums.items():
        if name in smart or name.lower() in excl:
            continue
        for p in album:
            in_album.add(p.id)
    log.info(f"OHNE-ALBUM: {len(in_album)} Fotos liegen in Alben.")
    new = skipped = 0
    for p in photos.all:
        if not in_window():
            log.info("OHNE-ALBUM: Zeitfenster vorbei – geht nächste Nacht weiter.")
            break
        if p.id in in_album:
            continue
        year = str(p.created.year)
        folder = os.path.join(DEST, year)
        name = p.filename
        targets = [(AssetVersionSize.ORIGINAL, name)]
        if LivePhotoVersionSize.ORIGINAL in p.versions:
            stem = os.path.splitext(name)[0]
            targets.append((LivePhotoVersionSize.ORIGINAL, f"{stem}_HEVC.MOV"))
        for size, fname in targets:
            version = p.versions.get(size)
            if not version:
                continue
            path = os.path.join(folder, fname)
            if os.path.exists(path):
                skipped += 1
                continue
            if download_media(log, False, icloud, p, path, version, size, lambda _p, f=fname: f):
                new += 1
                if new % 50 == 0:
                    log.info(f"OHNE-ALBUM: {new} neu gesichert …")
    log.info(f"OHNE-ALBUM FERTIG: {new} neu, {skipped} schon vorhanden.")


if __name__ == "__main__":
    try:
        main()
    except BaseException as ex:  # noqa: BLE001
        log.info(f"OHNE-ALBUM FEHLER: {type(ex).__name__}: {ex}")
        sys.exit(1)
