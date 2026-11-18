"""Duplikat-Finder: vergleicht Ordner A und B, zeigt doppelte Dateien.

Gelöscht wird nie direkt: "Entfernen" verschiebt die Datei in den Ordner
_Papierkorb auf derselben Freigabe. Von dort kann man sie zurückholen.
"""

import json
import os
import threading
from datetime import datetime

import waitress
from flask import Flask, redirect, render_template, request

OPT = json.load(open("/data/options.json"))
VIDEO = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".mpg",
         ".mpeg", ".iso", ".vob", ".webm", ".flv", ".m2ts", ".divx"}


def sub(path):
    """Unterordner nach //server/freigabe."""
    return "/".join(path.split("/")[4:])


ROOT = {
    "a": {"name": OPT["ordner_a"], "mnt": "/mnt/a", "sub": sub(OPT["ordner_a"])},
    "b": {"name": OPT["ordner_b"], "mnt": "/mnt/b", "sub": sub(OPT["ordner_b"])},
}
for r in ROOT.values():
    r["path"] = os.path.join(r["mnt"], r["sub"]) if r["sub"] else r["mnt"]

STATE = {"groups": [], "scanning": False, "scanned": None, "log": [],
         "counts": {}, "decided": {}}
LOCK = threading.Lock()


def log(msg):
    STATE["log"].append(f"{datetime.now():%H:%M:%S}  {msg}")
    del STATE["log"][:-100]
    print(msg, flush=True)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def walk(key):
    base = ROOT[key]["path"]
    files = {}
    for root, dirs, names in os.walk(base):
        dirs[:] = [d for d in dirs if d != "_Papierkorb" and not d.startswith(".")]
        for n in names:
            if n.startswith("."):
                continue
            if OPT.get("nur_videos", True) and os.path.splitext(n)[1].lower() not in VIDEO:
                continue
            full = os.path.join(root, n)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            files.setdefault((n.lower(), size), []).append(os.path.relpath(full, base))
    return files


def scan():
    with LOCK:
        if STATE["scanning"]:
            return
        STATE["scanning"] = True
    try:
        log("Lese Ordner A ein …")
        fa = walk("a")
        log(f"Ordner A: {sum(len(v) for v in fa.values())} Dateien.")
        log("Lese Ordner B ein …")
        fb = walk("b")
        log(f"Ordner B: {sum(len(v) for v in fb.values())} Dateien.")
        groups = []
        for key in sorted(set(fa) & set(fb)):
            name, size = key
            groups.append({
                "id": f"{name}|{size}", "name": os.path.basename(fa[key][0]),
                "size": size, "size_h": human(size), "a": fa[key], "b": fb[key]})
        STATE["groups"] = groups
        STATE["counts"] = {"a": sum(len(v) for v in fa.values()),
                           "b": sum(len(v) for v in fb.values()),
                           "dup": len(groups),
                           "dup_size": human(sum(g["size"] for g in groups))}
        STATE["scanned"] = datetime.now().strftime("%d.%m. %H:%M")
        log(f"Fertig: {len(groups)} doppelte Dateien gefunden.")
    except Exception as ex:  # noqa: BLE001
        log(f"Fehler beim Einlesen: {ex}")
    finally:
        STATE["scanning"] = False


def to_trash(key, rel):
    """Verschiebt eine Datei in _Papierkorb auf derselben Freigabe."""
    src = os.path.join(ROOT[key]["path"], rel)
    trash_rel = os.path.join(ROOT[key]["sub"], rel) if ROOT[key]["sub"] else rel
    dst = os.path.join(ROOT[key]["mnt"], "_Papierkorb", trash_rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        dst += f".{datetime.now():%Y%m%d%H%M%S}"
    os.rename(src, dst)
    log(f"In den Papierkorb ({key.upper()}): {rel}")


app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html", s=STATE, root=ROOT,
                           mounted={k: os.path.isdir(r["path"]) for k, r in ROOT.items()})


@app.route("/scan", methods=["POST"])
def do_scan():
    threading.Thread(target=scan, daemon=True).start()
    return redirect("./")


@app.route("/decide", methods=["POST"])
def decide():
    gid, action = request.form["id"], request.form["action"]
    group = next((g for g in STATE["groups"] if g["id"] == gid), None)
    if group and gid not in STATE["decided"]:
        try:
            if action in ("a", "b"):
                for rel in group[action]:
                    to_trash(action, rel)
                STATE["decided"][gid] = f"aus {action.upper()} entfernt"
            else:
                STATE["decided"][gid] = "beide behalten"
        except Exception as ex:  # noqa: BLE001
            log(f"Fehler: {ex}")
    return redirect("./#g" + str(STATE["groups"].index(group)) if group else "./")


if __name__ == "__main__":
    for k, r in ROOT.items():
        log(f"Ordner {k.upper()}: {r['name']} → {'OK' if os.path.isdir(r['path']) else 'NICHT GEFUNDEN'}")
    threading.Thread(target=scan, daemon=True).start()
    waitress.serve(app, host="0.0.0.0", port=8099)
