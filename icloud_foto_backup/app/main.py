"""iCloud Foto-Backup für Home Assistant.

Grundregel: Es wird NIE eine Datei gelöscht oder überschrieben. Die App lädt
nur hinzu. Ordner werden nur nach Bestätigung im Web-Interface umbenannt.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime

import requests
import waitress
from flask import Flask, jsonify, redirect, render_template, request

OPTIONS_FILE = "/data/options.json"
STATE_FILE = "/data/state.json"
COOKIE_DIR = "/data/cookies"
OPT = json.load(open(OPTIONS_FILE))
MOUNT = "/mnt/wd"
WD_ROOT = os.path.join(MOUNT, OPT.get("base_folder", "").strip("/")).rstrip("/")
ALBUM_ROOT = os.path.join(WD_ROOT, "Alben")

# Intelligente Alben von Apple -> keine eigenen Ordner
SMART_ALBUMS = {
    "all photos", "alle fotos", "time-lapse", "videos", "slo-mo", "bursts",
    "favorites", "panoramas", "screenshots", "live", "hidden",
    "recently deleted", "selfies", "portrait", "raw", "long exposure",
    "animated", "unknown", "library",
}
PHOTO_EXT = {
    ".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".mov", ".mp4",
    ".m4v", ".dng", ".tif", ".tiff", ".webp", ".avi", ".3gp",
}

LOCK = threading.RLock()
CODE_EVENT = threading.Event()
CODE_VALUE = {"code": None}
CURRENT = {"proc": None}
LOG = []


# ------------------------------------------------------------------ helpers
def log(msg):
    line = f"{datetime.now():%d.%m. %H:%M:%S}  {msg}"
    print(line, flush=True)
    with LOCK:
        LOG.append(line)
        del LOG[:-300]


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"suggestions": {}, "done": {}, "last_run": None,
                "last_error": None, "needs_code": False, "notified": {}}


STATE = load_state()
if STATE.get("match_version") != 3:
    STATE["suggestions"] = {k: v for k, v in STATE.get("suggestions", {}).items()
                            if v.get("status") != "pending"}
    STATE["checked"] = []
    STATE["match_version"] = 3


def save_state():
    with LOCK:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(STATE, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_FILE)


def notify(message, title="Foto-Backup", key=None, every_h=20):
    """Push an alle eingestellten notify-Dienste (mit Sperrfrist pro key)."""
    if key:
        last = STATE["notified"].get(key, 0)
        if time.time() - last < every_h * 3600:
            return
        STATE["notified"][key] = time.time()
        save_state()
    token = os.environ.get("SUPERVISOR_TOKEN")
    for svc in OPT.get("notify_services", []):
        try:
            requests.post(
                f"http://supervisor/core/api/services/notify/{svc}",
                headers={"Authorization": f"Bearer {token}"},
                json={"title": title, "message": message},
                timeout=10,
            )
        except Exception as ex:  # noqa: BLE001
            log(f"Push an {svc} fehlgeschlagen: {ex}")


def in_window(now=None):
    h = (now or datetime.now()).hour
    s, e = OPT["start_hour"], OPT["end_hour"]
    return (s <= h < e) if s < e else (h >= s or h < e)


def free_gb():
    return shutil.disk_usage(MOUNT).free / 1024 ** 3


def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip().strip(".")
    return name or "Unbenannt"


def stem(filename):
    return os.path.splitext(os.path.basename(filename))[0].lower()


def base_args():
    return [
        "icloudpd",
        "--username", OPT["apple_id"],
        "--password", OPT["apple_password"],
        "--password-provider", "parameter",
        "--mfa-provider", "console",
        "--cookie-directory", COOKIE_DIR,
        "--no-progress-bar",
    ]


def run_icloudpd(extra, collect=False, stop_at_window_end=False):
    """Startet icloudpd. Beantwortet eine 2FA-Abfrage über das Web-Interface.

    Gibt (exitcode, ausgabezeilen) zurück.
    """
    os.makedirs(COOKIE_DIR, exist_ok=True)
    proc = subprocess.Popen(
        base_args() + extra,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=0, start_new_session=True,
    )
    CURRENT["proc"] = proc
    lines, buf = [], b""
    fd = proc.stdout.fileno()
    os.set_blocking(fd, False)
    try:
        while True:
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                chunk = None
            if chunk:
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    text = raw.decode("utf-8", "replace").rstrip("\r")
                    if collect:
                        lines.append(text)
                    if text.strip() and not collect:
                        log(text.replace(OPT["apple_password"], "***"))
                pending = buf.decode("utf-8", "replace")
                if "authentication code" in pending and pending.rstrip().endswith(":"):
                    buf = b""
                    answer_mfa(proc)
            elif chunk == b"" or proc.poll() is not None:
                if proc.poll() is not None:
                    break
                time.sleep(0.2)
            else:
                if stop_at_window_end and not in_window():
                    log("Zeitfenster vorbei – Download pausiert bis zur nächsten Nacht.")
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(timeout=60)
                    break
                time.sleep(0.3)
        if buf and collect:
            lines.append(buf.decode("utf-8", "replace"))
        return proc.wait(), lines
    finally:
        CURRENT["proc"] = None


def answer_mfa(proc):
    log("Apple verlangt einen Zwei-Faktor-Code. Bitte im Foto-Backup eingeben.")
    with LOCK:
        STATE["needs_code"] = True
        save_state()
    notify("Apple verlangt einen neuen Zwei-Faktor-Code. Öffne in Home Assistant "
           "die Seite „Foto-Backup“ und gib den Code von deinem iPhone ein.",
           title="Foto-Backup: Code nötig", key="mfa", every_h=6)
    CODE_EVENT.clear()
    # bis zu 8 Stunden auf den Code warten
    if CODE_EVENT.wait(timeout=8 * 3600) and CODE_VALUE["code"]:
        proc.stdin.write((CODE_VALUE["code"] + "\n").encode())
        proc.stdin.flush()
        CODE_VALUE["code"] = None
        log("Code übermittelt.")
    else:
        log("Kein Code erhalten – Abbruch.")
        os.killpg(proc.pid, signal.SIGTERM)
    with LOCK:
        STATE["needs_code"] = False
        save_state()


# ------------------------------------------------------------------ iCloud
def authenticate():
    code, _ = run_icloudpd(["--auth-only"])
    if code != 0:
        STATE["last_error"] = "Anmeldung bei iCloud fehlgeschlagen"
        save_state()
        notify("Anmeldung bei iCloud fehlgeschlagen. Apple-ID/Passwort prüfen "
               "oder neuen Code eingeben.", key="authfail")
        return False
    return True


def list_albums():
    code, lines = run_icloudpd(["--list-albums"], collect=True)
    if code != 0 or not any(l.strip() == "Albums:" for l in lines):
        log(f"Albenliste fehlgeschlagen (Exit {code}). Ausgabe:")
        keys = ("error", "exception", "denied", "not activated", "adp",
                "web access", "please", "failed", "traceback")
        hits = [l for l in lines if any(k in l.lower() for k in keys)]
        for l in (hits[-15:] or lines[:15]):
            log("  " + l.replace(OPT["apple_password"], "***")[:400])
        return []
    out, started = [], False
    for line in lines:
        if line.strip() == "Albums:":
            started = True
            continue
        if started and line.strip():
            out.append(line.strip())
    excl = {a.lower() for a in OPT.get("exclude_albums", [])}
    return [a for a in out if a.lower() not in SMART_ALBUMS and a.lower() not in excl]


def album_filenames(album):
    tmp = "/tmp/empty-listing"
    os.makedirs(tmp, exist_ok=True)
    code, lines = run_icloudpd(
        ["--album", album, "-d", tmp, "--folder-structure", "{:%Y-%m}",
         "--only-print-filenames"], collect=True)
    if code != 0:
        return None
    out = set()
    for l in lines:
        if l.startswith(tmp + "/"):
            parts = l[len(tmp) + 1:].split("/")
            if len(parts) >= 2:
                out.add(f"{parts[0]}|{stem(parts[-1])}")
    return out


def index_old_folders():
    """Alle vorhandenen Ordner auf der WD (außer Alben/) mit Dateinamen."""
    result = {}
    for root, dirs, files in os.walk(WD_ROOT):
        rel = os.path.relpath(root, WD_ROOT)
        if rel == "." :
            dirs[:] = [d for d in dirs if d != "Alben" and not d.startswith(".")]
            continue
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        keys, count = set(), 0
        for f in files:
            if os.path.splitext(f)[1].lower() not in PHOTO_EXT:
                continue
            try:
                t = datetime.fromtimestamp(os.path.getmtime(os.path.join(root, f)))
            except OSError:
                continue
            count += 1
            s_ = stem(f)
            # Monat der Datei ± 1 (Zeitzonen / Export-Ungenauigkeit)
            for d in (-1, 0, 1):
                m = t.month + d
                y = t.year + (m - 1) // 12
                m = (m - 1) % 12 + 1
                keys.add(f"{y:04d}-{m:02d}|{s_}")
        if count:
            result[rel] = (keys, count)
    return result


NAMES_FILE = "/data/album_names_v2.json"


def load_names():
    try:
        with open(NAMES_FILE) as f:
            return {k: set(v) for k, v in json.load(f).items()}
    except Exception:  # noqa: BLE001
        return {}


def save_names(cache):
    tmp = NAMES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({k: sorted(v) for k, v in cache.items()}, f)
    os.replace(tmp, NAMES_FILE)


CHECK_LOCK = threading.Lock()


def check_albums():
    if not CHECK_LOCK.acquire(blocking=False):
        log("Albenprüfung läuft schon – warte darauf.")
        with CHECK_LOCK:
            return
    try:
        _check_albums()
    finally:
        CHECK_LOCK.release()


def _check_albums():
    """Vergleicht iCloud-Alben mit vorhandenen Ordnern und erstellt Vorschläge.

    Ähnlichkeit = gemeinsame Dateinamen / alle Dateinamen beider Seiten
    (Jaccard). Jeder alte Ordner wird höchstens einem Album vorgeschlagen.
    """
    if not authenticate():
        return
    albums = list_albums()
    log(f"{len(albums)} Alben in iCloud gefunden.")
    if not albums:
        return
    with LOCK:
        STATE["albums"] = albums
    cache = load_names()
    todo = [a for a in albums if a not in cache]
    if todo:
        log(f"Lese Dateilisten von {len(todo)} Alben (einmalig, dauert) …")
    for i, album in enumerate(todo, 1):
        names = album_filenames(album)
        if names is None:
            continue
        cache[album] = names
        if i % 10 == 0:
            save_names(cache)
            log(f"  {i}/{len(todo)} Alben eingelesen")
    save_names(cache)

    old = index_old_folders()
    log(f"{len(old)} vorhandene Ordner auf der WD eingelesen.")
    thr = OPT["match_threshold"] / 100
    candidates = []
    for album in albums:
        names = cache.get(album)
        sug = STATE["suggestions"].get(album)
        if not names or (sug and sug["status"] != "pending"):
            continue
        best, best_j, best_hits = None, 0.0, 0
        for folder, (keys, count) in old.items():
            hits = len(names & keys)
            if not hits:
                continue
            j = hits / (len(names) + count - hits)
            if j > best_j:
                best, best_j, best_hits = folder, j, hits
        if best and best_j >= thr and safe_name(album) != os.path.basename(best):
            candidates.append((best_j, album, best, best_hits, len(names)))
    used = {s["folder"] for s in STATE["suggestions"].values()
            if s["status"] in ("accepted", "rejected")}
    fresh = {}
    for j, album, folder, hits, total in sorted(candidates, reverse=True):
        if folder in used:
            continue
        used.add(folder)
        fresh[album] = {"folder": folder, "hits": hits, "total": total,
                        "percent": round(j * 100), "status": "pending"}
    with LOCK:
        for a in [a for a, s in STATE["suggestions"].items() if s["status"] == "pending"]:
            del STATE["suggestions"][a]
        STATE["suggestions"].update(fresh)
        save_state()
    log(f"{len(fresh)} Umbenennungsvorschläge.")
    if fresh:
        notify(f"{len(fresh)} alte Ordner passen zu iCloud-Alben. Bitte in Home Assistant "
               "unter „Foto-Backup“ prüfen und bestätigen.", key="suggest", every_h=48)
    STATE["last_check"] = datetime.now().isoformat(timespec="minutes")
    save_state()


def same_content(a, b, size):
    """Gleicher Inhalt? Vergleicht Anfang, Mitte und Ende (je 64 KB)."""
    try:
        with open(a, "rb") as fa, open(b, "rb") as fb:
            for off in (0, max(0, size // 2 - 32768), max(0, size - 65536)):
                fa.seek(off)
                fb.seek(off)
                if fa.read(65536) != fb.read(65536):
                    return False
        return True
    except OSError:
        return False


def index_existing():
    """(dateiname, größe) -> Pfad für alle Fotos außerhalb von Alben/."""
    idx = {}
    for root, dirs, files in os.walk(WD_ROOT):
        if os.path.relpath(root, WD_ROOT) == ".":
            dirs[:] = [d for d in dirs if d != "Alben" and not d.startswith(".")]
        else:
            dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if os.path.splitext(f)[1].lower() in PHOTO_EXT:
                full = os.path.join(root, f)
                try:
                    idx.setdefault((f.lower(), os.path.getsize(full)), full)
                except OSError:
                    pass
    return idx


def remove_new_duplicates(dest, idx):
    """Entfernt NUR frisch geladene Kopien, die es schon in einem alten Ordner gibt
    (gleicher Name, gleiche Größe, gleicher Inhalt). Alte Fotos bleiben unberührt."""
    removed = 0
    if not os.path.isdir(dest):
        return 0
    known = set(STATE.setdefault("seen_files", {}).get(dest, []))
    for f in os.listdir(dest):
        full = os.path.join(dest, f)
        if f in known or not os.path.isfile(full):
            continue
        size = os.path.getsize(full)
        other = idx.get((f.lower(), size))
        if other and other != full and same_content(full, other, size):
            os.remove(full)  # nur die neue, identische Kopie
            removed += 1
        else:
            known.add(f)
    STATE["seen_files"][dest] = sorted(known)
    return removed


def download_albums():
    albums = STATE.get("albums") or []
    os.makedirs(ALBUM_ROOT, exist_ok=True)
    log("Lese vorhandene Fotos für den Doppelt-Check ein …")
    idx = index_existing()
    log(f"{len(idx)} vorhandene Fotos außerhalb von Alben/.")
    week = 7 * 24 * 3600
    for album in albums:
        if not in_window():
            return
        sug = STATE["suggestions"].get(album)
        if sug and sug["status"] == "pending":
            log(f"„{album}“ übersprungen – wartet auf deine Bestätigung.")
            continue
        done = STATE["done"].get(album)
        if done and time.time() - datetime.fromisoformat(done).timestamp() < week:
            continue  # vollständig gesichert, erst nach einer Woche erneut prüfen
        if free_gb() < OPT["min_free_gb"]:
            notify(f"Auf der WD sind nur noch {free_gb():.0f} GB frei. "
                   "Das Backup pausiert, bis wieder Platz ist.", key="space")
            log("Zu wenig Speicher – Backup pausiert.")
            return
        dest = os.path.join(ALBUM_ROOT, safe_name(album))
        log(f"Album „{album}“ wird abgeglichen …")
        # icloudpd lädt nur fehlende Dateien, überschreibt nie und löscht nichts
        code, _ = run_icloudpd(
            ["--album", album, "-d", dest, "--folder-structure", "none"],
            stop_at_window_end=True)
        dup = remove_new_duplicates(dest, idx)
        if dup:
            log(f"  {dup} Fotos gibt es schon in alten Ordnern – nicht doppelt gespeichert.")
        if os.path.isdir(dest) and not os.listdir(dest):
            os.rmdir(dest)  # leerer, eben angelegter Ordner
        if code == 0:
            with LOCK:
                STATE["done"][album] = datetime.now().isoformat(timespec="minutes")
                save_state()
        else:
            save_state()


def nightly_job():
    log("Nächtliches Backup startet.")
    STATE["last_error"] = None
    if not os.path.ismount(MOUNT) or not os.path.isdir(WD_ROOT):
        STATE["last_error"] = "WD-Freigabe nicht eingebunden"
        save_state()
        notify("Die WD-Freigabe ist nicht eingebunden – Backup nicht möglich.", key="mount")
        return
    check_albums()
    if in_window():
        download_albums()
    STATE["last_run"] = datetime.now().isoformat(timespec="minutes")
    save_state()
    log("Durchlauf beendet.")


def scheduler():
    last_night = None
    while True:
        try:
            now = datetime.now()
            # "Nacht" = Datum des Fensterbeginns
            night = now.date() if now.hour >= OPT["start_hour"] else \
                datetime.fromordinal(now.toordinal() - 1).date()
            if in_window(now) and last_night != night:
                last_night = night
                nightly_job()
        except Exception as ex:  # noqa: BLE001
            log(f"Fehler: {ex}")
            STATE["last_error"] = str(ex)
            save_state()
            notify(f"Fehler im Foto-Backup: {ex}", key="error")
        time.sleep(60)


# ------------------------------------------------------------------ Web-UI
app = Flask(__name__)


def busy():
    return CURRENT["proc"] is not None


@app.route("/")
def index():
    try:
        free = f"{free_gb():.0f} GB" if os.path.ismount(MOUNT) else "nicht eingebunden"
    except Exception:  # noqa: BLE001
        free = "?"
    with LOCK:
        return render_template(
            "index.html", state=STATE, log=LOG[-80:], free=free,
            busy=busy(), window=f"{OPT['start_hour']}–{OPT['end_hour']} Uhr")


@app.route("/code", methods=["POST"])
def code():
    CODE_VALUE["code"] = re.sub(r"\D", "", request.form.get("code", ""))
    CODE_EVENT.set()
    return redirect("./")


@app.route("/decide", methods=["POST"])
def decide():
    album = request.form["album"]
    action = request.form["action"]
    with LOCK:
        sug = STATE["suggestions"].get(album)
        if not sug or sug["status"] != "pending":
            return redirect("./")
        if action == "accept":
            src = os.path.join(WD_ROOT, sug["folder"])
            dst = os.path.join(ALBUM_ROOT, safe_name(album))
            os.makedirs(ALBUM_ROOT, exist_ok=True)
            if os.path.exists(dst):
                sug["status"] = "conflict"
                log(f"„{dst}“ existiert schon – nichts umbenannt.")
            else:
                os.rename(src, dst)  # nur verschieben/umbenennen, nichts löschen
                sug["status"] = "accepted"
                log(f"Umbenannt: „{sug['folder']}“ → „Alben/{safe_name(album)}“")
        else:
            sug["status"] = "rejected"
            log(f"Vorschlag für „{album}“ abgelehnt – Album wird neu geladen.")
        save_state()
    return redirect("./")


@app.route("/check", methods=["POST"])
def check_now():
    if not busy():
        threading.Thread(target=check_albums, daemon=True).start()
    return redirect("./")


@app.route("/api/status")
def api_status():
    return jsonify({k: STATE.get(k) for k in ("last_run", "last_error", "needs_code")})


if __name__ == "__main__":
    log("Foto-Backup gestartet. Zeitfenster: "
        f"{OPT['start_hour']}–{OPT['end_hour']} Uhr. Es wird nie etwas gelöscht.")
    if os.environ.get("WD_MOUNTED") != "1":
        notify("Die WD-Freigabe konnte nicht eingebunden werden. Bitte "
               "Freigabe, Benutzer und Passwort in der App prüfen.", key="mount")
    threading.Thread(target=scheduler, daemon=True).start()
    waitress.serve(app, host="0.0.0.0", port=8099)
