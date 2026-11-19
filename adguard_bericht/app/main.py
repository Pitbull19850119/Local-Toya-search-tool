"""AdGuard Wochenbericht: liest die AdGuard-Statistik (letzte 7 Tage),
macht daraus einen leicht verständlichen Bericht und schickt ihn per Push."""
import json, os, threading, time
from datetime import datetime, timedelta
import requests, waitress
from flask import Flask, redirect

OPT = json.load(open("/data/options.json"))
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
STATE = {"report": None, "last": None, "error": None, "port": None}
LAST_FILE = "/data/last_sent"

CATS = [  # (Stichwort, Kategorie, Erklärung)
    ("a2z.com", "Amazon", "Fire TV / Prime Video Nutzungsdaten"),
    ("amazon", "Amazon", "Alexa / Fire TV Statistik"),
    ("app-analytics-services", "Amazon", "App-Tracking"),
    ("doubleclick", "Google", "Werbung"), ("googlesyndication", "Google", "Werbung"),
    ("googleadservices", "Google", "Werbung"), ("google-analytics", "Google", "Tracking"),
    ("app-measurement", "Google", "App-Tracking"), ("crashlytics", "Google", "Absturzberichte"),
    ("googletagmanager", "Google", "Tracking"),
    ("facebook", "Meta", "Tracking"), ("fbcdn", "Meta", "Tracking"), ("instagram", "Meta", "Tracking"),
    ("tiktok", "TikTok", "Tracking"), ("bytedance", "TikTok", "Tracking"),
    ("samsung", "Samsung", "TV-Telemetrie / Werbung"), ("apple", "Apple", "Statistik"),
    ("icloud", "Apple", "Statistik"), ("microsoft", "Microsoft", "Telemetrie"),
    ("msn.com", "Microsoft", "Werbung"), ("tuya", "Tuya", "Smart-Home-Statistik"),
    ("xiaomi", "Xiaomi", "Telemetrie"), ("unity3d", "Spiele", "Werbung in Spielen"),
    ("applovin", "Spiele", "Werbung in Spielen"), ("ironsrc", "Spiele", "Werbung in Spielen"),
    ("adjust", "Tracking", "App-Tracking"), ("appsflyer", "Tracking", "App-Tracking"),
    ("branch.io", "Tracking", "App-Tracking"), ("sentry", "Tracking", "Fehlerberichte"),
    ("telemetry", "Tracking", "Nutzungsdaten"), ("metrics", "Tracking", "Nutzungsdaten"),
    ("analytics", "Tracking", "Nutzungsdaten"), ("ads", "Werbung", "Werbung"),
]


def categorize(domain):
    d = domain.lower()
    for key, cat, why in CATS:
        if key in d:
            return cat, why
    if len(d.split(".")[0]) >= 30:
        return "Tracking", "verschleierter Tracker"
    return "Sonstiges", "Werbung/Tracking laut Filterliste"


def find_port():
    if OPT.get("adguard_port"):
        return OPT["adguard_port"]
    try:  # Supervisor-Discovery der AdGuard-App
        r = requests.get("http://supervisor/discovery", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=10)
        for d in r.json().get("data", {}).get("discovery", []):
            if d.get("service") == "adguard":
                return int(d["config"]["port"])
    except Exception:
        pass
    for p in (45158, 3000, 80):
        try:
            requests.get(f"http://127.0.0.1:{p}/control/status", timeout=3)
            return p
        except Exception:
            continue
    return 45158


def ag(path, method="GET", body=None):
    port = STATE["port"] or find_port()
    STATE["port"] = port
    r = requests.request(method, f"http://127.0.0.1:{port}/control/{path}", json=body, timeout=30)
    r.raise_for_status()
    return r.json() if r.text.strip().startswith(("{", "[")) else {}


def ensure_retention():
    """Statistik-Zeitraum auf mind. 7 Tage stellen."""
    week = 7 * 24 * 3600 * 1000
    try:
        cfg = ag("stats/config")
        if cfg.get("interval", 0) < week or not cfg.get("enabled", True):
            cfg.update({"enabled": True, "interval": week})
            ag("stats/config/update", "PUT", cfg)
    except Exception as ex:
        STATE["error"] = f"Statistik-Einstellung: {ex}"


def client_names():
    names = {}
    try:
        data = ag("clients")
        for c in (data.get("clients") or []):
            for i in c.get("ids", []):
                names[i] = c["name"]
        for c in (data.get("auto_clients") or []):
            if c.get("name"):
                names.setdefault(c["ip"], c["name"])
    except Exception:
        pass
    return names


def first(d):
    return next(iter(d.items()))


def build():
    s = ag("stats")
    names = client_names()
    total = s.get("num_dns_queries", 0)
    blocked = s.get("num_blocked_filtering", 0)
    malware = s.get("num_replaced_safebrowsing", 0)
    adult = s.get("num_replaced_parental", 0)
    pct = round(blocked * 100 / total, 1) if total else 0
    top_b = [first(x) for x in s.get("top_blocked_domains", [])][:10]
    top_c = [first(x) for x in s.get("top_clients", [])][:8]
    cats = {}
    for dom, n in [first(x) for x in s.get("top_blocked_domains", [])]:
        c, _ = categorize(dom)
        cats[c] = cats.get(c, 0) + n
    cats = sorted(cats.items(), key=lambda x: -x[1])
    days = s.get("dns_queries", [])[-7:]
    bdays = s.get("blocked_filtering", [])[-7:]
    today = datetime.now().date()
    daylist = []
    for i, (q, b) in enumerate(zip(days, bdays)):
        d = today - timedelta(days=len(days) - 1 - i)
        daylist.append((["Mo","Di","Mi","Do","Fr","Sa","So"][d.weekday()] + d.strftime(" %d.%m."), q, b))
    up = [first(x) for x in s.get("top_upstreams_responses", [])][:3]
    rep = {
        "zeit": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "total": total, "blocked": blocked, "pct": pct, "malware": malware, "adult": adult,
        "ms": round(s.get("avg_processing_time", 0) * 1000),
        "top_blocked": [(d, n, *categorize(d)) for d, n in top_b],
        "clients": [(names.get(ip, ip), n) for ip, n in top_c],
        "cats": cats, "days": daylist, "upstreams": up,
    }
    rep["titel"] = "die letzten 7 Tage"
    rep["hints"] = hints_for(rep)
    return rep


def build_range(start, end):
    """Wertet das Abfrageprotokoll für einen frei gewählten Zeitraum aus."""
    names = client_names()
    total = blocked = malware = adult = 0
    dom, cli, perday = {}, {}, {}
    older = None
    for _ in range(400):  # max 400.000 Einträge
        q = "querylog?limit=1000" + (f"&older_than={older}" if older else "")
        data = ag(q).get("data") or []
        if not data:
            break
        for e in data:
            t = datetime.fromisoformat(e["time"].replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
            if t >= end:
                continue
            if t < start:
                data = []
                break
            total += 1
            ip = e.get("client", "")
            name = names.get(ip) or (e.get("client_info") or {}).get("name") or ip
            cli[name] = cli.get(name, 0) + 1
            day = t.date()
            pd = perday.setdefault(day, [0, 0])
            pd[0] += 1
            reason = e.get("reason", "")
            if reason.startswith("Filtered"):
                if "SafeBrowsing" in reason:
                    malware += 1
                elif "Parental" in reason:
                    adult += 1
                else:
                    blocked += 1
                    d = (e.get("question") or {}).get("name", "?")
                    dom[d] = dom.get(d, 0) + 1
                pd[1] += 1
        if not data:
            break
        older = data[-1]["time"]
    top_b = sorted(dom.items(), key=lambda x: -x[1])[:10]
    cats = {}
    for d, n in dom.items():
        c, _ = categorize(d)
        cats[c] = cats.get(c, 0) + n
    days = []
    d = start.date()
    while d < end.date() or (d == end.date() and end.hour):
        q, b = perday.get(d, [0, 0])
        days.append((["Mo","Di","Mi","Do","Fr","Sa","So"][d.weekday()] + d.strftime(" %d.%m."), q, b))
        d += timedelta(days=1)
    pct = round(blocked * 100 / total, 1) if total else 0
    rep = {"zeit": datetime.now().strftime("%d.%m.%Y %H:%M"),
           "titel": f"{start:%d.%m.%Y} – {(end - timedelta(seconds=1)):%d.%m.%Y}",
           "total": total, "blocked": blocked, "pct": pct, "malware": malware, "adult": adult, "ms": None,
           "top_blocked": [(d, n, *categorize(d)) for d, n in top_b],
           "clients": sorted(cli.items(), key=lambda x: -x[1])[:8],
           "cats": sorted(cats.items(), key=lambda x: -x[1]), "days": days[-31:]}
    rep["hints"] = hints_for(rep)
    if total == 0:
        rep["hints"].insert(0, "Für diesen Zeitraum sind keine Daten im Protokoll (AdGuard läuft erst seit 19.11.2026).")
    return rep


def hints_for(r):
    hints = []
    if r["malware"]:
        hints.append(f"⚠️ {r['malware']}× wurde eine gefährliche Seite (Phishing/Schadsoftware) aufgehalten.")
    else:
        hints.append("✅ Keine gefährlichen Seiten (Phishing/Schadsoftware) aufgerufen.")
    if r["pct"] > 30:
        hints.append("ℹ️ Ungewöhnlich viel gesperrt – meist ein Gerät, das ständig nach Hause funkt.")
    if r["top_blocked"]:
        d, n, c, why = r["top_blocked"][0]
        hints.append(f"Häufigster Blocker: {c} ({why}) – {n}×.")
    if any(str(c[0]).count(".") == 3 for c in r["clients"]):
        hints.append("Tipp: Geräte in AdGuard benennen (Einstellungen → Client-Einstellungen), dann stehen hier Namen statt IPs.")
    return hints


def push_text(r):
    lines = [f"{r['total']:,} Anfragen, {r['blocked']:,} gesperrt ({r['pct']} %).".replace(",", "."),
             r["hints"][0]]
    if r["cats"]:
        lines.append("Gesperrt: " + ", ".join(f"{c} {n}" for c, n in r["cats"][:4]))
    if r["clients"]:
        c, n = r["clients"][0]
        lines.append(f"Aktivstes Gerät: {c} ({n}).")
    return "\n".join(lines)


def notify(title, msg):
    msg = msg
    for svc in OPT.get("notify_services", []):
        try:
            requests.post(f"http://supervisor/core/api/services/notify/{svc}",
                          headers={"Authorization": f"Bearer {TOKEN}"},
                          json={"title": title, "message": msg,
                                "data": {"url": "/app/" + os.environ.get("HOSTNAME", "").replace("-", "_"),
                                         "clickAction": "/app/" + os.environ.get("HOSTNAME", "").replace("-", "_")}},
                          timeout=15)
        except Exception as ex:
            STATE["error"] = f"Push {svc}: {ex}"


def run_report(send=True):
    try:
        r = build()
        STATE["report"], STATE["error"] = r, None
        if send:
            notify("🛡️ AdGuard-Bericht " + r["titel"], push_text(r))
            STATE["last"] = r["zeit"]
            open(LAST_FILE, "w").write(datetime.now().strftime("%Y-%W"))
    except Exception as ex:
        STATE["error"] = f"AdGuard nicht erreichbar (Port {STATE['port']}): {ex}"


def scheduler():
    ensure_retention()
    run_report(send=False)
    while True:
        now = datetime.now()
        week = now.strftime("%Y-%W")
        sent = open(LAST_FILE).read().strip() if os.path.exists(LAST_FILE) else ""
        if now.weekday() == OPT["wochentag"] and now.hour >= OPT["uhrzeit"] and sent != week:
            run_report(True)
        time.sleep(300)


app = Flask(__name__)
WD = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@app.route("/")
def index():
    r = STATE["report"]
    css = """body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#111;color:#eee;margin:0;padding:16px;max-width:860px}
h1{font-size:22px}h2{font-size:17px;margin-top:28px;color:#8fc}.kpi{display:flex;gap:10px;flex-wrap:wrap}
.k{background:#1d1d1d;border-radius:10px;padding:12px 16px;flex:1;min-width:130px}.k b{font-size:24px;display:block}
table{width:100%;border-collapse:collapse}td{padding:6px 4px;border-bottom:1px solid #2a2a2a;font-size:14px;word-break:break-all}
td.n{text-align:right;white-space:nowrap}.bar{height:8px;background:#e55;border-radius:4px}.q{background:#4a8}
.h{background:#1d2a22;border-radius:10px;padding:10px 14px;margin:6px 0}button{background:#2a7;border:0;color:#fff;padding:10px 16px;border-radius:8px;font-size:15px}
.muted{color:#999;font-size:13px}"""
    h = [f"<html><head><meta name=viewport content='width=device-width,initial-scale=1'><style>{css}</style></head><body>"]
    h.append(f"<h1>🛡️ AdGuard – {esc(r['titel']) if r else 'Bericht'}</h1>")
    today = datetime.now().date()
    h.append("<form action=range style='display:flex;gap:8px;flex-wrap:wrap;align-items:end;background:#1d1d1d;padding:12px;border-radius:10px'>"
             "<a href=week><button type=button onclick=\"location.href='week'\">Letzte 7 Tage</button></a>"
             "<button type=button onclick=\"location.href='range?tage=30'\" style='background:#357'>Letzte 30 Tage</button>"
             f"<label>von<br><input type=date name=von value={today - timedelta(days=7)}></label>"
             f"<label>bis<br><input type=date name=bis value={today}></label>"
             "<button style='background:#357'>Zeitraum anzeigen</button></form>")
    if STATE["error"]:
        h.append(f"<div class=h style='background:#3a1d1d'>{esc(STATE['error'])}</div>")
    if r:
        h.append(f"<p class=muted>Stand {r['zeit']} · Push jeden {WD[OPT['wochentag']]} um {OPT['uhrzeit']} Uhr"
                 f"{' · zuletzt gesendet ' + STATE['last'] if STATE['last'] else ''}</p>")
        h.append("<div class=kpi>"
                 f"<div class=k><b>{r['total']:,}</b>Anfragen gesamt</div>"
                 f"<div class=k><b>{r['blocked']:,}</b>gesperrt ({r['pct']} %)</div>"
                 f"<div class=k><b>{r['malware']}</b>gefährliche Seiten</div>"
                 (f"<div class=k><b>{r['ms']} ms</b>Antwortzeit</div>" if r.get('ms') is not None else "") + "</div>".replace(",", "."))
        h.append("<h2>Kurz gesagt</h2>" + "".join(f"<div class=h>{esc(x)}</div>" for x in r["hints"]))
        if r["cats"]:
            mx = r["cats"][0][1]
            h.append("<h2>Wer wollte Daten sammeln?</h2><table>")
            for c, n in r["cats"]:
                h.append(f"<tr><td>{esc(c)}</td><td style='width:45%'><div class=bar style='width:{n*100//mx}%'></div></td><td class=n>{n}</td></tr>")
            h.append("</table>")
        if r["top_blocked"]:
            h.append("<h2>Am häufigsten gesperrt</h2><table>")
            for d, n, c, why in r["top_blocked"]:
                h.append(f"<tr><td>{esc(d)}<br><span class=muted>{esc(c)} – {esc(why)}</span></td><td class=n>{n}×</td></tr>")
            h.append("</table>")
        if r["clients"]:
            h.append("<h2>Aktivste Geräte</h2><table>")
            for c, n in r["clients"]:
                h.append(f"<tr><td>{esc(c)}</td><td class=n>{n:,} Anfragen</td></tr>".replace(",", "."))
            h.append("</table>")
        if r["days"]:
            mx = max(q for _, q, _ in r["days"]) or 1
            h.append("<h2>Verlauf pro Tag</h2><table>")
            for d, q, b in r["days"]:
                h.append(f"<tr><td style='width:90px'>{d}</td><td><div class='bar q' style='width:{q*100//mx}%'></div>"
                         f"<div class=bar style='width:{b*100//mx}%;margin-top:2px'></div></td><td class=n>{q} / {b} gesperrt</td></tr>")
            h.append("</table>")
    h.append("<p><form method=post action=send><button>Angezeigten Bericht aufs Handy schicken</button></form></p>")
    h.append("<p class=muted>Grün = alle Anfragen, Rot = gesperrt. Gesperrt wird nur Werbung, Tracking und Gefährliches – "
             "falls etwas nicht funktioniert, in AdGuard im Abfrageprotokoll „Entsperren“.</p></body></html>")
    return "".join(h)


@app.route("/send", methods=["POST"])
def send():
    if STATE["report"]:
        notify("🛡️ AdGuard-Bericht " + STATE["report"]["titel"], push_text(STATE["report"]))
    else:
        run_report(True)
    return redirect("./")


@app.route("/week")
def week():
    run_report(False)
    return redirect("./")


@app.route("/range")
def rng():
    from flask import request
    try:
        if request.args.get("tage"):
            end = datetime.now()
            start = end - timedelta(days=int(request.args["tage"]))
        else:
            start = datetime.strptime(request.args["von"], "%Y-%m-%d")
            end = datetime.strptime(request.args["bis"], "%Y-%m-%d") + timedelta(days=1)
        STATE["report"], STATE["error"] = build_range(start, end), None
    except Exception as ex:
        STATE["error"] = f"Zeitraum-Auswertung fehlgeschlagen: {ex}"
    return redirect("./")


@app.route("/refresh")
def refresh():
    run_report(False)
    return redirect("./")


if __name__ == "__main__":
    threading.Thread(target=scheduler, daemon=True).start()
    waitress.serve(app, host="0.0.0.0", port=8113)
