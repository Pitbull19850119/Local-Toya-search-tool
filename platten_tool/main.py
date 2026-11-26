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


@app.route("/")
def index():
    return "<pre>" + json.dumps(api_disks(), indent=2) + "</pre>"


threading.Thread(target=sync_scheduler, daemon=True).start()
waitress.serve(app, host="0.0.0.0", port=8120)
