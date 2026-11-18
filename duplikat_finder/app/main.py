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

STATE = {"internal": [], "removed": set(), "bulk": None, "groups": [], "scanning": False, "scanned": None, "log": [],
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
        internal = []
        for side, files in (("a", fa), ("b", fb)):
            for (name, size), paths in sorted(files.items()):
                if len(paths) > 1:
                    internal.append({"side": side, "name": os.path.basename(paths[0]),
                                     "size_h": human(size), "size": size,
                                     "paths": sorted(paths)})
        STATE["internal"] = internal
        STATE["removed"] = set()
        STATE["counts"] = {"a": sum(len(v) for v in fa.values()),
                           "b": sum(len(v) for v in fb.values()),
                           "dup": len(groups),
                           "intern": len(internal),
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
    sp = space()
    sp = {k: v for k, v in sp.items() if isinstance(v, dict)}
    return render_template("index.html", s=STATE, root=ROOT, space=sp,
                           mounted={k: os.path.isdir(r["path"]) for k, r in ROOT.items()})


@app.route("/api/space")
def space():
    out = {}
    for k, r in ROOT.items():
        try:
            u = __import__("shutil").disk_usage(r["mnt"])
            out[k] = {"total": human(u.total), "used": human(u.used), "free": human(u.free)}
        except Exception as ex:  # noqa: BLE001
            out[k] = str(ex)
    return out


@app.route("/api/sizes")
def sizes():
    out = {}
    for k in ROOT:
        total, count = 0, 0
        for (name, size), paths in walk(k).items():
            total += size * len(paths)
            count += len(paths)
        out[k] = {"files": count, "size": human(total), "bytes": total}
    return out


def empty_worker():
    import shutil as sh
    STATE["emptying"] = True
    try:
        seen = set()
        for k, r in ROOT.items():
            trash = os.path.realpath(os.path.join(r["mnt"], "_Papierkorb"))
            if trash in seen or not os.path.isdir(trash):
                continue
            seen.add(trash)
            n = sum(len(f) for _, _, f in os.walk(trash))
            log(f"Leere Papierkorb {k.upper()} ({n} Dateien) …")
            sh.rmtree(trash)
            log(f"Papierkorb {k.upper()} geleert.")
    except Exception as ex:  # noqa: BLE001
        log(f"Fehler beim Leeren: {ex}")
    finally:
        STATE["emptying"] = False


@app.route("/empty_trash", methods=["POST"])
def empty_trash():
    data = request.form.to_dict() or (request.get_json(silent=True) or {})
    if data.get("confirm") == "LEEREN" and not STATE.get("emptying"):
        threading.Thread(target=empty_worker, daemon=True).start()
    return redirect("./")


@app.route("/scan", methods=["POST"])
def do_scan():
    threading.Thread(target=scan, daemon=True).start()
    return redirect("./")


def bulk_worker(ids, side):
    STATE["bulk"] = {"total": len(ids), "done": 0, "side": side.upper()}
    for gid in ids:
        group = next((g for g in STATE["groups"] if g["id"] == gid), None)
        if group and gid not in STATE["decided"]:
            try:
                for rel in group[side]:
                    to_trash(side, rel)
                STATE["decided"][gid] = f"aus {side.upper()} entfernt"
            except Exception as ex:  # noqa: BLE001
                log(f"Fehler bei {group['name']}: {ex}")
        STATE["bulk"]["done"] += 1
    log(f"{len(ids)} Einträge aus {side.upper()} in den Papierkorb verschoben.")
    STATE["bulk"] = None


@app.route("/bulk", methods=["POST"])
def bulk():
    ids = request.form.getlist("sel")
    side = request.form.get("action")
    if side in ("a", "b") and ids and not STATE.get("bulk"):
        threading.Thread(target=bulk_worker, args=(ids, side), daemon=True).start()
    elif side == "keep":
        for gid in ids:
            STATE["decided"].setdefault(gid, "beide behalten")
    return redirect("./")


def files_worker(items):
    STATE["bulk"] = {"total": len(items), "done": 0, "side": "A/B"}
    for side, rel in items:
        try:
            to_trash(side, rel)
            STATE["removed"].add(f"{side}|{rel}")
        except Exception as ex:  # noqa: BLE001
            log(f"Fehler bei {rel}: {ex}")
        STATE["bulk"]["done"] += 1
    log(f"{len(items)} doppelte Kopien in den Papierkorb verschoben.")
    STATE["bulk"] = None


@app.route("/bulk_files", methods=["POST"])
def bulk_files():
    items = []
    for v in request.form.getlist("f"):
        side, rel = v.split("|", 1)
        if side in ("a", "b") and v not in STATE.get("removed", set()):
            items.append((side, rel))
    # Sicherheit: pro Gruppe muss mindestens eine Kopie bleiben
    chosen = {f"{a}|{b}" for a, b in items}
    for g in STATE.get("internal", []):
        keys = [f"{g['side']}|{p}" for p in g["paths"]]
        left = [k for k in keys if k not in chosen and k not in STATE["removed"]]
        if not left:
            keep = keys[0]
            items = [i for i in items if f"{i[0]}|{i[1]}" != keep]
            log(f"„{g['name']}“: letzte Kopie wird behalten.")
    if items and not STATE.get("bulk"):
        threading.Thread(target=files_worker, args=(items,), daemon=True).start()
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
