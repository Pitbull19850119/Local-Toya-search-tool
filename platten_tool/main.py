"""Platten-Werkzeug: listet Laufwerke, formatiert eine gewählte Platte als ext4.
Formatieren nur mit exakter Seriennummer als Bestätigung und nie die System-Platte."""
import json, subprocess, time
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
    if not serial or serial != d.get("serial"):
        return {"ok": False, "err": "Seriennummer stimmt nicht – Abbruch"}, 400
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


@app.route("/")
def index():
    return "<pre>" + json.dumps(api_disks(), indent=2) + "</pre>"


waitress.serve(app, host="0.0.0.0", port=8120)
