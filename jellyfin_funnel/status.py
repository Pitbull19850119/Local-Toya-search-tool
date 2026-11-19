"""Kleine Statusseite fürs Home-Assistant-Seitenmenü."""
import html
import http.server
import os


def read(p):
    try:
        return open(p).read().strip()
    except OSError:
        return ""


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        login, public = read("/tmp/login_url"), read("/tmp/public_url")
        log = html.escape("\n".join(read("/tmp/status.txt").splitlines()[-30:][::-1]))
        if public:
            box = f'<h2>✅ Öffentlich erreichbar</h2><p class="big"><a href="{public}" target="_blank">{public}</a></p>'
        elif login:
            box = f'<h2>1. Bei Tailscale anmelden</h2><p><a class="btn" href="{login}" target="_blank">Jetzt anmelden</a></p>'
        else:
            box = "<h2>⏳ Startet …</h2><p>Seite in ein paar Sekunden neu laden.</p>"
        page = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="10">
<title>Jellyfin Funnel</title><style>
:root{{--bg:#f6f7f9;--card:#fff;--t:#1c1e21;--m:#6b7280;--l:#e5e7eb}}
@media(prefers-color-scheme:dark){{:root{{--bg:#111418;--card:#1c2026;--t:#e6e8eb;--m:#9aa3ad;--l:#2c323a}}}}
body{{margin:0;font:15px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--t)}}
main{{max-width:760px;margin:0 auto;padding:16px}}.c{{background:var(--card);border:1px solid var(--l);border-radius:12px;padding:16px;margin-bottom:12px}}
.big{{font-size:20px;word-break:break-all}}a{{color:#03a9f4}}.btn{{display:inline-block;background:#03a9f4;color:#fff;padding:10px 16px;border-radius:8px;text-decoration:none}}
pre{{background:var(--bg);padding:10px;border-radius:8px;font-size:12px;white-space:pre-wrap;max-height:320px;overflow:auto}}
</style></head><body><main><h1>🛰️ Jellyfin Funnel</h1><div class="c">{box}</div>
<div class="c"><b>Protokoll</b><pre>{log}</pre></div></main></body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(page.encode())

    def log_message(self, *a):
        pass


http.server.ThreadingHTTPServer(("0.0.0.0", 8099), H).serve_forever()
