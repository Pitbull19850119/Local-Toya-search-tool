"""Erzeugt eine WireGuard-Konfiguration für Cloudflare WARP (wie wgcf)."""
import base64
import datetime
import html
import http.server
import json
import os

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

DATA = "/data/warp.json"
API = "https://api.cloudflareclient.com/v0a2158"
HEAD = {"User-Agent": "okhttp/3.12.1", "CF-Client-Version": "a-6.30-3596",
        "Content-Type": "application/json"}


def b64(b):
    return base64.b64encode(b).decode()


def register():
    priv = X25519PrivateKey.generate()
    priv_b = priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
    pub_b = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    body = {"install_id": "", "fcm_token": "", "key": b64(pub_b), "type": "Android",
            "locale": "de_DE",
            "tos": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")}
    r = requests.post(f"{API}/reg", headers=HEAD, json=body, timeout=20)
    r.raise_for_status()
    d = r.json()
    tok, rid = d["token"], d["id"]
    # WARP aktivieren
    requests.patch(f"{API}/reg/{rid}", headers={**HEAD, "Authorization": f"Bearer {tok}"},
                   json={"warp_enabled": True}, timeout=20)
    cfg = d["config"]
    peer = cfg["peers"][0]
    info = {
        "private_key": b64(priv_b),
        "address_v4": cfg["interface"]["addresses"]["v4"],
        "address_v6": cfg["interface"]["addresses"]["v6"],
        "peer_public_key": peer["public_key"],
        "endpoint": "engage.cloudflareclient.com:2408",
        "endpoint_ip": peer["endpoint"].get("v4", "").split(":")[0] or "162.159.192.1",
    }
    with open(DATA, "w") as f:
        json.dump(info, f)
    return info


def conf(i, use_ip=True):
    ep = f"{i['endpoint_ip']}:2408" if use_ip else i["endpoint"]
    return (f"[Interface]\nPrivateKey = {i['private_key']}\n"
            f"Address = {i['address_v4']}/32\nDNS = 1.1.1.1\nMTU = 1280\n\n"
            f"[Peer]\nPublicKey = {i['peer_public_key']}\n"
            f"AllowedIPs = 0.0.0.0/0\nEndpoint = {ep}\nPersistentKeepalive = 25\n")


def load():
    try:
        return json.load(open(DATA))
    except Exception:  # noqa: BLE001
        return None


class H(http.server.BaseHTTPRequestHandler):
    def send(self, body, ctype="text/html; charset=utf-8", extra=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        i = load()
        if self.path.endswith("/warp.conf") and i:
            return self.send(conf(i), "text/plain; charset=utf-8",
                             {"Content-Disposition": 'attachment; filename="warp.conf"'})
        msg = ""
        if i:
            c = html.escape(conf(i))
            box = f"""<h2>✅ Konfiguration bereit</h2>
<p><a class="btn" href="warp.conf">warp.conf herunterladen</a></p>
<p class="k">Oder Inhalt kopieren und im TP-Link manuell eintragen:</p><pre>{c}</pre>"""
        else:
            box = """<h2>WARP-Konfiguration erzeugen</h2>
<form method="post" action="gen"><button class="btn">Jetzt erzeugen</button></form>"""
        self.send(f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>WARP</title><style>
:root{{--bg:#f6f7f9;--card:#fff;--t:#1c1e21;--m:#6b7280;--l:#e5e7eb}}
@media(prefers-color-scheme:dark){{:root{{--bg:#111418;--card:#1c2026;--t:#e6e8eb;--m:#9aa3ad;--l:#2c323a}}}}
body{{margin:0;font:15px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--t)}}main{{max-width:760px;margin:0 auto;padding:16px}}
.c{{background:var(--card);border:1px solid var(--l);border-radius:12px;padding:16px}}.k{{color:var(--m)}}
.btn{{display:inline-block;background:#f38020;color:#fff;border:0;padding:10px 16px;border-radius:8px;text-decoration:none;font:inherit;cursor:pointer}}
pre{{background:var(--bg);padding:10px;border-radius:8px;font-size:13px;white-space:pre-wrap;word-break:break-all}}
</style></head><body><main><h1>☁️ Cloudflare WARP für den Router</h1><div class="c">{box}{msg}</div></main></body></html>""")

    def do_POST(self):
        try:
            register()
            self.send_response(303)
            self.send_header("Location", "./")
            self.end_headers()
        except Exception as ex:  # noqa: BLE001
            self.send(f"<p>Fehler bei der Registrierung: {html.escape(str(ex))}</p><p><a href='./'>zurück</a></p>")

    def log_message(self, *a):
        pass


http.server.ThreadingHTTPServer(("0.0.0.0", 8099), H).serve_forever()
