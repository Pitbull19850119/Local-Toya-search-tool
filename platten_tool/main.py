"""Platten-Werkzeug: listet Laufwerke, formatiert eine gewählte Platte als ext4.
Formatieren nur mit exakter Seriennummer als Bestätigung und nie die System-Platte."""
import json, os, subprocess, threading, time
import waitress
from flask import Flask, request

app = Flask(__name__)
LOG = []


def sh(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    LOG.append(f"$ {' '.join(cmd)}\n{r.stdout}{r.stderr}")
    return r


def disks():
    r = subprocess.run(["lsblk", "-J", "-b", "-o", "NAME,PATH,SIZE,MODEL,SERIAL,TRAN,TYPE,FSTYPE,LABEL,MOUNTPOINTS"],
                       capture_output=True, text=True)
    return [d for d in json.loads(r.stdout)["blockdevices"] if d["type"] == "disk"]


def is_system(d):
    for c in d.get("children") or []:
        if (c.get("label") or "").startswith("hassos") or c.get("label") in ("hassos-data", "hassos-boot"):
            return True
    return False


@app.route("/api/disks")
def api_disks():
    out = []
    for d in disks():
        out.append({"path": d["path"], "size_gb": round(d["size"] / 1e9), "model": d.get("model"),
                    "serial": d.get("serial"), "tran": d.get("tran"), "system": is_system(d),
                    "parts": [{"path": c["path"], "fs": c.get("fstype"), "label": c.get("label"),
                               "size_gb": round(c["size"] / 1e9)} for c in d.get("children") or []]})
    return {"disks": out}


@app.route("/api/format", methods=["POST"])
def api_format():
    data = request.get_json(silent=True) or request.form
    dev, serial, label = data.get("path"), data.get("serial"), data.get("label", "Filme")
    d = next((x for x in disks() if x["path"] == dev), None)
    if not d:
        return {"ok": False, "err": "Gerät nicht gefunden"}, 400
    if is_system(d):
        return {"ok": False, "err": "System-Platte – wird nie formatiert"}, 400
    expect = d.get("serial") or f"{(d.get('model') or '').strip()}|{round(d['size'] / 1e9)}"
    if not serial or serial != expect:
        return {"ok": False, "err": f"Bestätigung stimmt nicht (erwartet Modell|GB) – Abbruch"}, 400
    if dev == "/dev/sda" or (d.get("tran") != "usb"):
        return {"ok": False, "err": "Nur USB-Platten werden formatiert"}, 400
    if d["size"] < 1e12:
        return {"ok": False, "err": "Platte kleiner als 1 TB – Abbruch zur Sicherheit"}, 400
    LOG.clear()
    for c in d.get("children") or []:
        sh(["umount", "-l", c["path"]])
    sh(["wipefs", "-a", dev])
    sh(["parted", "-s", dev, "mklabel", "gpt", "mkpart", "primary", "ext4", "0%", "100%"])
    sh(["partprobe", dev]); time.sleep(3)
    part = dev + ("p1" if dev[-1].isdigit() else "1")
    r = sh(["mkfs.ext4", "-F", "-L", label, "-m", "0", "-T", "largefile", part])
    return {"ok": r.returncode == 0, "part": part, "log": LOG}


COPY = {"state": "idle", "lines": [], "result": None}


def by_label(label):
    for d in disks():
        for c in d.get("children") or []:
            if c.get("label") == label:
                return c["path"], c.get("fstype")
    return None, None


def cp_log(msg):
    COPY["lines"].append(f"{time.strftime('%d.%m. %H:%M:%S')}  {msg}")
    del COPY["lines"][:-200]
    print(msg, flush=True)


def tree_stats(root):
    n = b = 0
    for r, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in ("$RECYCLE.BIN", "System Volume Information", "lost+found")]
        for f in files:
            try:
                b += os.path.getsize(os.path.join(r, f)); n += 1
            except OSError:
                pass
    return n, b


def copy_job(src_label, dst_label):
    COPY.update(state="läuft", lines=[], result=None)
    src, sfs = by_label(src_label)
    dst, dfs = by_label(dst_label)
    if not src or not dst:
        cp_log(f"FEHLER: Quelle „{src_label}“ oder Ziel „{dst_label}“ nicht gefunden"); COPY["state"] = "fehler"; return
    if dfs != "ext4":
        cp_log("FEHLER: Ziel ist nicht ext4 – Abbruch"); COPY["state"] = "fehler"; return
    os.makedirs("/mnt/quelle", exist_ok=True); os.makedirs("/mnt/ziel", exist_ok=True)
    tries = ([["mount", "-t", "ntfs3", "-o", "ro", src, "/mnt/quelle"],
              ["ntfs-3g", "-o", "ro", src, "/mnt/quelle"]] if sfs == "ntfs" else
             [["mount", "-o", "ro", src, "/mnt/quelle"]])
    for t in tries:
        m = subprocess.run(t, capture_output=True, text=True)
        if m.returncode == 0:
            cp_log(f"Quelle eingebunden mit: {t[0]} {t[2] if t[1] == '-t' else ''}".strip())
            break
    if m.returncode:
        cp_log(f"FEHLER: Quelle nicht einbindbar: {m.stderr.strip()}"); COPY["state"] = "fehler"; return
    m = subprocess.run(["mount", dst, "/mnt/ziel"], capture_output=True, text=True)
    if m.returncode:
        cp_log(f"FEHLER: Ziel nicht einbindbar: {m.stderr.strip()}"); subprocess.run(["umount", "/mnt/quelle"])
        COPY["state"] = "fehler"; return
    qn, qb = tree_stats("/mnt/quelle")
    cp_log(f"Quelle {src} („{src_label}“, nur lesend): {qn} Dateien, {qb/1e9:.0f} GB → Ziel {dst} („{dst_label}“)")
    p = subprocess.Popen(["rsync", "-rltD", "--no-perms", "--no-owner", "--no-group", "--info=progress2",
                          "--exclude=$RECYCLE.BIN", "--exclude=System Volume Information",
                          "/mnt/quelle/", "/mnt/ziel/"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    last = 0
    buf = ""
    while True:
        ch = p.stdout.read(1)
        if not ch:
            break
        if ch in "\r\n":
            if buf.strip() and time.time() - last > 60:
                cp_log("Fortschritt: " + " ".join(buf.split()[:4])); last = time.time()
            buf = ""
        else:
            buf += ch
    rc = p.wait()
    zn, zb = tree_stats("/mnt/ziel")
    diff = subprocess.run(["rsync", "-rn", "--size-only", "--out-format=%n", "--exclude=$RECYCLE.BIN",
                           "--exclude=System Volume Information", "/mnt/quelle/", "/mnt/ziel/"],
                          capture_output=True, text=True).stdout.split("\n")
    missing = [x for x in diff if x and not x.endswith("/")]
    ok = rc == 0 and not missing and zn >= qn
    COPY["result"] = {"ok": ok, "rsync": rc, "quelle_dateien": qn, "quelle_gb": round(qb/1e9, 1),
                      "ziel_dateien": zn, "ziel_gb": round(zb/1e9, 1), "fehlend": missing[:50]}
    cp_log(("FERTIG: vollständig – " if ok else "WARNUNG: unvollständig – ") +
           f"Quelle {qn} Dateien/{qb/1e9:.1f} GB, Ziel {zn} Dateien/{zb/1e9:.1f} GB, fehlend {len(missing)}")
    subprocess.run(["sync"]); subprocess.run(["umount", "/mnt/ziel"]); subprocess.run(["umount", "/mnt/quelle"])
    COPY["state"] = "fertig" if ok else "unvollständig"


@app.route("/api/copy", methods=["POST"])
def api_copy():
    data = request.get_json(silent=True) or request.form
    if COPY["state"] == "läuft":
        return {"ok": False, "err": "Kopie läuft bereits"}, 409
    threading.Thread(target=copy_job, args=(data.get("src_label"), data.get("dst_label")), daemon=True).start()
    return {"ok": True}


@app.route("/api/copy_status")
def api_copy_status():
    return COPY


# ---------------------------------------------------------------- Ordner anlegen / sortieren
def mount_label(label, where):
    dev, fs = by_label(label)
    if not dev:
        return None
    os.makedirs(where, exist_ok=True)
    if os.path.ismount(where):
        return dev
    r = subprocess.run(["mount", dev, where], capture_output=True, text=True)
    return dev if r.returncode == 0 else None


@app.route("/api/organize", methods=["POST"])
def api_organize():
    """Alles im Wurzelverzeichnis der Zielplatte nach <ordner>/ verschieben, weitere Ordner anlegen."""
    data = request.get_json(silent=True) or request.form
    label = data.get("label", "Medien")
    into = data.get("into", "Filme & Serien")
    extra = data.get("create", ["Fotos"])
    if COPY["state"] == "läuft":
        return {"ok": False, "err": "Kopie läuft noch"}, 409
    root = "/mnt/org"
    if not mount_label(label, root):
        return {"ok": False, "err": f"„{label}“ nicht gefunden/einbindbar"}, 400
    keep = {into, "lost+found", "_Gelöscht"} | set(extra)
    os.makedirs(os.path.join(root, into), exist_ok=True)
    moved = []
    for name in sorted(os.listdir(root)):
        if name in keep:
            continue
        dst = os.path.join(root, into, name)
        if os.path.exists(dst):
            return {"ok": False, "err": f"„{into}/{name}“ existiert schon – nichts weiter verschoben", "moved": moved}, 409
        os.rename(os.path.join(root, name), dst)  # gleiche Platte: nur umbenennen
        moved.append(name)
    for e in extra:
        os.makedirs(os.path.join(root, e), exist_ok=True)
    top = sorted(os.listdir(root))
    subprocess.run(["sync"]); subprocess.run(["umount", root])
    return {"ok": True, "moved": len(moved), "oben": top}


# ---------------------------------------------------------------- nächtlicher Foto-Abgleich
SYNC = {"state": "idle", "last": None, "lines": []}


def opt():
    try:
        return json.load(open("/data/options.json"))
    except Exception:  # noqa: BLE001
        return {}


def notify(msg, title="Foto-Abgleich 5 TB"):
    svc = opt().get("notify_service")
    if not svc:
        return
    try:
        import requests
        requests.post(f"http://supervisor/core/api/services/notify/{svc}",
                      headers={"Authorization": f"Bearer {os.environ.get('SUPERVISOR_TOKEN')}"},
                      json={"title": title, "message": msg}, timeout=10)
    except Exception:  # noqa: BLE001
        pass


def s_log(msg):
    SYNC["lines"].append(f"{time.strftime('%d.%m. %H:%M:%S')}  {msg}")
    del SYNC["lines"][:-100]
    print("SYNC " + msg, flush=True)


def sync_job():
    o = opt()
    SYNC["state"] = "läuft"
    wd, dst = "/mnt/wdsync", "/mnt/syncziel"
    os.makedirs(wd, exist_ok=True)
    cred = "/tmp/wdcred"
    with open(cred, "w") as f:
        f.write(f"username={o.get('wd_user','')}\npassword={o.get('wd_password','')}\n")
    os.chmod(cred, 0o600)
    ok_mount = False
    if os.path.ismount(wd):
        ok_mount = True
    else:
        for v in ("3.0", "2.1", "2.0"):
            r = subprocess.run(["mount", "-t", "cifs", o.get("wd_share"), wd, "-o",
                                f"credentials={cred},vers={v},ro,iocharset=utf8,soft,echo_interval=10"],
                               capture_output=True, text=True, timeout=90)
            if r.returncode == 0:
                ok_mount = True
                break
    os.remove(cred)
    if not ok_mount:
        s_log("WD nicht erreichbar – neuer Versuch in 15 min"); SYNC["state"] = "warte"; return False
    if not mount_label(o.get("ziel_name", "Medien"), dst):
        s_log("Zielplatte nicht gefunden"); subprocess.run(["umount", wd]); SYNC["state"] = "fehler"
        notify("Die 5-TB-Platte wurde nicht gefunden – Foto-Abgleich ausgefallen."); return True
    src = os.path.join(wd, o.get("wd_ordner", "Fotos")).rstrip("/") + "/"
    tgt = os.path.join(dst, o.get("ziel_ordner", "Fotos")).rstrip("/") + "/"
    day = time.strftime("%Y-%m-%d")
    trash = os.path.join(dst, "_Gelöscht", o.get("ziel_ordner", "Fotos"), day)
    os.makedirs(tgt, exist_ok=True)
    s_log(f"Abgleich {src} → {tgt} (Gelöschtes nach _Gelöscht/{o.get('ziel_ordner','Fotos')}/{day})")
    r = subprocess.run(["rsync", "-rlt", "--no-perms", "--no-owner", "--no-group", "--delete",
                        "--backup", f"--backup-dir={trash}", "--exclude=_Papierkorb/",
                        "--stats", src, tgt], capture_output=True, text=True)
    stats = {k: v for k, v in (l.split(":", 1) for l in r.stdout.splitlines() if ":" in l)}
    nfiles = stats.get("Number of regular files transferred", "?").strip()
    ndel = stats.get("Number of deleted files", "0").strip()
    s_log(f"rsync-Code {r.returncode}: {nfiles} Dateien neu/geändert, {ndel} in _Gelöscht verschoben")
    if r.returncode not in (0, 24):
        s_log("Fehler: " + r.stderr.strip()[-300:])
        notify(f"Foto-Abgleich WD → 5 TB fehlgeschlagen (Code {r.returncode}).")
    # alte Lösch-Ordner aufräumen
    base = os.path.join(dst, "_Gelöscht", o.get("ziel_ordner", "Fotos"))
    keep_days = int(o.get("geloescht_tage", 30))
    if os.path.isdir(base):
        for d in os.listdir(base):
            try:
                age = (time.time() - time.mktime(time.strptime(d, "%Y-%m-%d"))) / 86400
            except ValueError:
                continue
            if age > keep_days:
                subprocess.run(["rm", "-rf", os.path.join(base, d)])
                s_log(f"_Gelöscht/{d} älter als {keep_days} Tage – entfernt")
    subprocess.run(["sync"]); subprocess.run(["umount", dst]); subprocess.run(["umount", wd])
    SYNC["last"] = time.strftime("%Y-%m-%d %H:%M")
    SYNC["state"] = "fertig" if r.returncode in (0, 24) else "fehler"
    return True


def sync_scheduler():
    done_day = None
    while True:
        o = opt()
        now = time.localtime()
        if o.get("sync_aktiv") and now.tm_hour >= int(o.get("sync_uhrzeit", 5)) \
                and done_day != now.tm_yday and COPY["state"] != "läuft":
            try:
                if sync_job():
                    done_day = now.tm_yday
            except Exception as ex:  # noqa: BLE001
                s_log(f"Fehler: {ex}"); done_day = now.tm_yday
        time.sleep(900)


@app.route("/api/sync_now", methods=["POST"])
def api_sync_now():
    if SYNC["state"] == "läuft":
        return {"ok": False, "err": "läuft bereits"}, 409
    threading.Thread(target=sync_job, daemon=True).start()
    return {"ok": True}


@app.route("/api/sync_status")
def api_sync_status():
    return SYNC


JELLYFIN_HOOK = """#!/bin/bash
# Von Platten-Werkzeug angelegt: 5-TB-Platte „Medien“ unter dem alten Pfad bereitstellen,
# damit Bibliotheken und Gesehen-Status unverändert bleiben.
SRC="/mnt/Medien/Filme & Serien"
if [ -d "$SRC" ]; then
  mkdir -p /mnt/Portable-2
  mountpoint -q /mnt/Portable-2 || mount --bind "$SRC" /mnt/Portable-2 && echo "Medien/Filme & Serien -> /mnt/Portable-2 eingebunden"
else
  echo "WARNUNG: $SRC nicht gefunden (localdisks: Medien gesetzt?)"
fi
"""


@app.route("/api/jellyfin_hook", methods=["POST"])
def api_jellyfin_hook():
    base = "/addon_configs"
    cands = [d for d in os.listdir(base) if d.endswith("_jellyfin")] if os.path.isdir(base) else []
    if not cands:
        return {"ok": False, "err": f"Jellyfin-Konfigordner nicht gefunden: {os.listdir(base) if os.path.isdir(base) else 'kein /addon_configs'}"}, 400
    path = os.path.join(base, cands[0], "jellyfin.sh")
    if os.path.exists(path):
        os.rename(path, path + ".bak")
    with open(path, "w") as f:
        f.write(JELLYFIN_HOOK)
    os.chmod(path, 0o755)
    return {"ok": True, "path": path}


# ---------------------------------------------------------------- WD-Ordner auflisten / kopieren
WDC = {"state": "idle", "lines": [], "result": None, "stop": False}


def wd_mount(where="/mnt/wdlist", ro=True):
    o = opt()
    os.makedirs(where, exist_ok=True)
    if os.path.ismount(where):
        return True
    cred = "/tmp/wdcred2"
    with open(cred, "w") as f:
        f.write(f"username={o.get('wd_user','')}\npassword={o.get('wd_password','')}\n")
    os.chmod(cred, 0o600)
    try:
        for v in ("3.0", "2.1", "2.0"):
            r = subprocess.run(["mount", "-t", "cifs", o.get("wd_share"), where, "-o",
                                f"credentials={cred},vers={v},{'ro,' if ro else ''}iocharset=utf8,soft,echo_interval=10"],
                               capture_output=True, text=True, timeout=90)
            if r.returncode == 0:
                return True
    finally:
        os.remove(cred)
    return False


@app.route("/api/wd_list")
def api_wd_list():
    """Ordner der WD-Freigabe (2 Ebenen) mit Größe – Fotos/ wird nur gezählt, nicht vermessen."""
    root = "/mnt/wdlist"
    if not wd_mount(root):
        return {"ok": False, "err": "WD nicht einbindbar (wd_user/wd_password in den Optionen?)"}, 400
    out = []
    for top in sorted(os.listdir(root)):
        full = os.path.join(root, top)
        if not os.path.isdir(full) or top.startswith("."):
            continue
        entry = {"name": top, "subs": []}
        try:
            subs = sorted(x for x in os.listdir(full) if not x.startswith("."))
        except OSError:
            subs = []
        entry["anzahl"] = len(subs)
        if top.lower() not in ("fotos",):
            r = subprocess.run(["du", "-s", "-B1G", "--max-depth=1", full], capture_output=True, text=True, timeout=900)
            for line in r.stdout.splitlines():
                gb, path = line.split("\t", 1)
                name = os.path.relpath(path, full)
                if name == ".":
                    entry["gb"] = int(gb)
                else:
                    entry["subs"].append({"name": name, "gb": int(gb)})
        else:
            entry["subs"] = [{"name": x} for x in subs[:50]]
        out.append(entry)
    return {"ok": True, "ordner": out}


def wd_copy_job(folders, dest_sub, stop_hour):
    WDC.update(state="läuft", lines=[], result=None, stop=False)
    def lg(m):
        WDC["lines"].append(f"{time.strftime('%d.%m. %H:%M:%S')}  {m}"); del WDC["lines"][:-200]; print("WDCOPY " + m, flush=True)
    src_root, dst_root = "/mnt/wdcopy", "/mnt/wdziel"
    if not wd_mount(src_root):
        lg("FEHLER: WD nicht einbindbar"); WDC["state"] = "fehler"; return
    if not mount_label(opt().get("ziel_name", "Medien"), dst_root):
        lg("FEHLER: Zielplatte nicht gefunden"); WDC["state"] = "fehler"; return
    results = []
    for f in folders:
        src = os.path.join(src_root, f).rstrip("/") + "/"
        dst = os.path.join(dst_root, dest_sub, os.path.basename(f.rstrip("/"))) + "/"
        if not os.path.isdir(src):
            lg(f"übersprungen (nicht gefunden): {f}"); continue
        os.makedirs(dst, exist_ok=True)
        lg(f"Kopiere „{f}“ → {dest_sub}/{os.path.basename(f.rstrip('/'))}")
        p = subprocess.Popen(["rsync", "-rlt", "--no-perms", "--no-owner", "--no-group", "--partial",
                              "--info=progress2", src, dst], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        last, buf, stopped = 0, "", False
        while True:
            ch = p.stdout.read(1)
            if not ch:
                break
            if ch in "\r\n":
                if buf.strip() and time.time() - last > 120:
                    lg("Fortschritt: " + " ".join(buf.split()[:4])); last = time.time()
                buf = ""
                h = time.localtime().tm_hour
                if WDC["stop"] or (stop_hour is not None and h >= stop_hour):
                    p.terminate(); stopped = True; break
            else:
                buf += ch
        rc = p.wait()
        if stopped:
            lg(f"PAUSIERT ({'Stopp angefordert' if WDC['stop'] else f'ab {stop_hour} Uhr für das Foto-Backup'}) – beim nächsten Start geht es nahtlos weiter")
            WDC["state"] = "pausiert"; break
        diff = subprocess.run(["rsync", "-rn", "--size-only", "--out-format=%n", src, dst], capture_output=True, text=True).stdout.split("\n")
        missing = [x for x in diff if x and not x.endswith("/")]
        results.append({"ordner": f, "rsync": rc, "fehlend": len(missing)})
        lg(f"„{f}“ fertig: rsync-Code {rc}, fehlend {len(missing)}")
    WDC["result"] = results
    if WDC["state"] == "läuft":
        WDC["state"] = "fertig"
    subprocess.run(["sync"]); subprocess.run(["umount", dst_root]); subprocess.run(["umount", src_root])


@app.route("/api/wd_copy", methods=["POST"])
def api_wd_copy():
    data = request.get_json(silent=True) or {}
    if WDC["state"] == "läuft":
        return {"ok": False, "err": "läuft bereits"}, 409
    folders = data.get("folders") or []
    if not folders:
        return {"ok": False, "err": "keine Ordner angegeben"}, 400
    threading.Thread(target=wd_copy_job, args=(folders, data.get("dest_sub", "Filme & Serien"),
                     data.get("stop_hour", 23)), daemon=True).start()
    return {"ok": True}


@app.route("/api/wd_copy_stop", methods=["POST"])
def api_wd_copy_stop():
    WDC["stop"] = True
    return {"ok": True}


@app.route("/api/wd_copy_status")
def api_wd_copy_status():
    return WDC


@app.route("/")
def index():
    return "<pre>" + json.dumps(api_disks(), indent=2) + "</pre>"


threading.Thread(target=sync_scheduler, daemon=True).start()
waitress.serve(app, host="0.0.0.0", port=8120)
