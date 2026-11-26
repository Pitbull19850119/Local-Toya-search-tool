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
    m = subprocess.run(["ntfs-3g", "-o", "ro", src, "/mnt/quelle"] if sfs == "ntfs" else
                       ["mount", "-o", "ro", src, "/mnt/quelle"], capture_output=True, text=True)
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


@app.route("/")
def index():
    return "<pre>" + json.dumps(api_disks(), indent=2) + "</pre>"


waitress.serve(app, host="0.0.0.0", port=8120)
