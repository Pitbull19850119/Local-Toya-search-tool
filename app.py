import binascii
import difflib
import hashlib
import hmac
import json
import os
import socket
import struct
import time

import requests
import yaml
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from flask import Flask, render_template, request, jsonify, Response

# Fester Tuya UDP-Broadcast-Schlüssel (öffentlich bekannt, genutzt von tinytuya u.a.
# für das unauthentifizierte lokale Discovery-Protokoll auf Port 6667)
UDP_BROADCAST_KEY = hashlib.md5(b"yGAdlopoPVldABfn").digest()

app = Flask(__name__)

OPTIONS_PATH = "/data/options.json"
# ACHTUNG Sicherheit: diese Datei enthält lokale Zugangsdaten (local_key) im Klartext.
# Nur lesbar durch den Addon-Container/Host, aber unverschlüsselt auf der Disk.
CACHE_PATH = "/data/device_cache.json"

# In-Memory-Cache (Single-User-Lokal-Addon, kein Multi-User-Betrieb vorgesehen).
# Wird zusätzlich nach /data persistiert, damit ein Addon-Neustart die Geräte nicht vergisst.
STATE = {
    "devices": [],       # zuletzt via /extract geladene Geräte (inkl. gescannter IP/Version)
    "scan_time": None,   # Zeitpunkt des letzten UDP-Scans
    "uid": None,
}


def save_state():
    try:
        with open(CACHE_PATH, "w") as f:
            json.dump(STATE, f)
    except Exception:
        pass  # Cache ist ein Komfort-Feature, darf nie einen Request zum Absturz bringen


def load_state():
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, "r") as f:
                data = json.load(f)
            STATE["devices"] = data.get("devices", [])
            STATE["scan_time"] = data.get("scan_time")
            STATE["uid"] = data.get("uid")
        except Exception:
            pass


load_state()

REGION_HOSTS = {
    "eu": "https://openapi.tuyaeu.com",
    "us": "https://openapi.tuyaus.com",
    "cn": "https://openapi.tuyacn.com",
    "in": "https://openapi.tuyain.com",
}


def load_options():
    """Vorbelegte Werte aus den Addon-Optionen laden (falls vorhanden)."""
    defaults = {"access_id": "", "access_secret": "", "region": "eu", "username": ""}
    if os.path.exists(OPTIONS_PATH):
        try:
            with open(OPTIONS_PATH, "r") as f:
                data = json.load(f)
            defaults.update({k: data.get(k, v) for k, v in defaults.items()})
        except Exception:
            pass
    return defaults


class TuyaClient:
    """Minimaler Client für die Tuya Cloud (OpenAPI) Business-Signatur, v1.0/v2.0."""

    def __init__(self, access_id, access_secret, region):
        self.access_id = access_id
        self.access_secret = access_secret
        self.host = REGION_HOSTS.get(region, REGION_HOSTS["eu"])
        self.access_token = ""

    def _sign(self, method, path, body, token=""):
        t = str(int(time.time() * 1000))
        body_str = json.dumps(body) if body else ""
        content_sha256 = hashlib.sha256(body_str.encode("utf-8")).hexdigest()
        string_to_sign = f"{method}\n{content_sha256}\n\n{path}"
        prefix = self.access_id + token + t
        sign_str = prefix + string_to_sign
        sign = hmac.new(
            self.access_secret.encode("utf-8"),
            sign_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest().upper()
        headers = {
            "client_id": self.access_id,
            "sign": sign,
            "t": t,
            "sign_method": "HMAC-SHA256",
            "Content-Type": "application/json",
        }
        if token:
            headers["access_token"] = token
        return headers, body_str

    def _request(self, method, path, body=None, use_token=True):
        token = self.access_token if use_token else ""
        headers, body_str = self._sign(method, path, body, token)
        url = self.host + path
        if method == "GET":
            resp = requests.get(url, headers=headers, timeout=15)
        else:
            resp = requests.post(url, headers=headers, data=body_str, timeout=15)
        return resp.json()

    def get_token(self):
        result = self._request("GET", "/v1.0/token?grant_type=1", use_token=False)
        if not result.get("success"):
            raise RuntimeError(f"Token-Abruf fehlgeschlagen: {result}")
        self.access_token = result["result"]["access_token"]
        return result["result"]

    def find_uid_by_username(self, username, schema="tuyaSmart"):
        path = f"/v1.0/apps/{schema}/user/matchers?username={username}"
        result = self._request("GET", path)
        if not result.get("success"):
            # Manche Konten benoetigen das smartlife-Schema statt tuyaSmart
            path = f"/v1.0/apps/smartlife/user/matchers?username={username}"
            result = self._request("GET", path)
        if not result.get("success"):
            raise RuntimeError(f"UID-Suche fehlgeschlagen: {result}")
        return result["result"]["uid"]

    def get_devices_for_uid(self, uid):
        path = f"/v1.0/users/{uid}/devices"
        result = self._request("GET", path)
        if not result.get("success"):
            raise RuntimeError(f"Geraeteliste fehlgeschlagen: {result}")
        return result["result"]

    def get_device_detail(self, device_id):
        path = f"/v1.0/devices/{device_id}"
        for attempt in range(3):
            result = self._request("GET", path)
            if result.get("success"):
                return result["result"]
            # Trial-Cloud-Projekte haben oft nur 1 QPS -> bei Rate-Limit kurz warten & retry
            if result.get("code") in (28841002, 1010) or "too many request" in str(result).lower():
                time.sleep(1.0 + attempt)
                continue
            return {}
        return {}


def _decrypt_udp_payload(payload):
    cipher = AES.new(UDP_BROADCAST_KEY, AES.MODE_ECB)
    decrypted = cipher.decrypt(payload)
    pad_len = decrypted[-1]
    return decrypted[:-pad_len]


def _parse_udp_packet(data):
    """Tuya UDP-Discovery-Paket parsen: 4B Prefix, 4B Seq, 4B Cmd, 4B Länge, Payload, 4B CRC, 4B Suffix."""
    if len(data) < 20:
        return None
    payload_len = struct.unpack(">I", data[12:16])[0]
    if payload_len < 8 or len(data) < 16 + payload_len:
        return None
    return data[16:16 + payload_len - 8]


def scan_local_network(timeout=10):
    """Lauscht auf Tuya-Broadcasts (Port 6666 unverschlüsselt / 6667 AES-verschlüsselt)
    und liefert ein Mapping gwId -> {ip, version}. Die 'version' (3.1/3.3/3.4) kommt
    NUR aus dem lokalen Broadcast, die Cloud API liefert sie nicht zuverlässig."""
    found = {}
    socks = []
    for port in (6666, 6667):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", port))
            s.settimeout(0.5)
            socks.append((s, port))
        except OSError:
            continue

    end_time = time.time() + timeout
    while time.time() < end_time:
        for s, port in socks:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                continue
            try:
                payload = _parse_udp_packet(data)
                if payload is None:
                    continue
                if port == 6667:
                    payload = _decrypt_udp_payload(payload)
                info = json.loads(payload.decode("utf-8"))
                gw_id = info.get("gwId")
                ip = info.get("ip") or addr[0]
                version = info.get("version", "")
                if gw_id:
                    found[gw_id] = {"ip": ip, "version": version}
            except Exception:
                continue

    for s, _ in socks:
        s.close()
    return found


TUYA_LOCAL_PORT = 6668
CMD_DP_QUERY = 0x0A
CMD_SESS_KEY_NEG_START = 0x03
CMD_SESS_KEY_NEG_RESP = 0x04
CMD_SESS_KEY_NEG_FINISH = 0x05


def _crc32(data):
    return binascii.crc32(data) & 0xFFFFFFFF


def _build_packet(seq, cmd, payload, hmac_key=None):
    """hmac_key gesetzt = Protokoll 3.4 nach Session-Key-Aushandlung (HMAC-SHA256 statt CRC32)."""
    footer_size = 36 if hmac_key else 8
    length = len(payload) + footer_size
    header = b"\x00\x00\x55\xAA" + struct.pack(">III", seq, cmd, length)
    body = header + payload
    sig = hmac.new(hmac_key, body, hashlib.sha256).digest() if hmac_key else struct.pack(">I", _crc32(body))
    return body + sig + b"\x00\x00\xAA\x55"


def _recv_packet(sock, hmac_mode=False, timeout=5):
    sock.settimeout(timeout)
    data = b""
    while len(data) < 16:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("Verbindung vom Gerät geschlossen (keine Antwort)")
        data += chunk
    _, cmd, length = struct.unpack(">III", data[4:16])
    total_needed = 16 + length
    while len(data) < total_needed:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("Verbindung geschlossen (unvollständige Antwort)")
        data += chunk
    footer_size = 36 if hmac_mode else 8
    payload = data[16:16 + length - footer_size]
    return cmd, payload


def _try_parse_status_payload(payload, key):
    """Mehrere plausible Dekodier-Varianten probieren (manche Firmwares schicken
    unverschlüsseltes JSON mit 4-Byte Return-Code, andere direkt AES-ECB-verschlüsselt).
    Gibt nur dann ein dict zurück, wenn ein plausibles Tuya-Status-JSON erkannt wurde
    -> das ist die Grundlage für den grünen Haken, alles andere bleibt 'nicht verifiziert'."""
    candidates = [payload, payload[4:] if len(payload) > 4 else b""]
    for raw in candidates:
        # Variante A: Klartext-JSON (manche 3.1-Antworten sind unverschlüsselt)
        try:
            obj = json.loads(raw.decode("utf-8"))
            if isinstance(obj, dict) and ("dps" in obj or "devId" in obj or "t" in obj):
                return obj
        except Exception:
            pass
        # Variante B: AES-ECB-verschlüsselt mit lokalem Key / Session-Key
        if len(raw) % 16 == 0 and len(raw) > 0:
            try:
                cipher = AES.new(key, AES.MODE_ECB)
                decrypted = cipher.decrypt(raw)
                try:
                    decrypted = unpad(decrypted, 16)
                except Exception:
                    pass
                obj = json.loads(decrypted.decode("utf-8"))
                if isinstance(obj, dict) and ("dps" in obj or "devId" in obj or "t" in obj):
                    return obj
            except Exception:
                pass
    return None


def verify_device_live(ip, device_id, local_key, version, timeout=5):
    """Baut eine ECHTE lokale Verbindung zum Gerät auf und fragt den Status ab (DP_QUERY).
    Nur wenn eine gültige, entschlüsselte Status-Antwort mit dem korrekten local_key
    ankommt, gilt das Gerät als verifiziert. Jeder Fehler (Timeout, Verbindung abgelehnt,
    Entschlüsselung fehlgeschlagen) führt zu ok=False -- nie zu einem falschen 'grünen Haken'."""
    if not ip or not device_id or not local_key:
        return {"ok": False, "reason": "IP, Device ID oder Local Key fehlt."}

    key_bytes = local_key.encode("utf-8")
    if len(key_bytes) != 16:
        return {"ok": False, "reason": f"Local Key hat {len(key_bytes)} Byte, erwartet werden 16 -- Key vermutlich falsch/unvollständig."}

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((ip, TUYA_LOCAL_PORT))
    except Exception as exc:
        return {"ok": False, "reason": f"Kein TCP-Connect zu {ip}:{TUYA_LOCAL_PORT} möglich ({exc}). IP/Firewall prüfen."}

    try:
        if version == "3.4":
            try:
                local_nonce = os.urandom(16)
                start_payload = AES.new(key_bytes, AES.MODE_ECB).encrypt(pad(local_nonce, 16))
                sock.send(_build_packet(0, CMD_SESS_KEY_NEG_START, start_payload))
                cmd, resp_payload = _recv_packet(sock, hmac_mode=False, timeout=timeout)
                raw = AES.new(key_bytes, AES.MODE_ECB).decrypt(resp_payload[:48])
                remote_nonce = raw[:16]
                xor_nonce = bytes(a ^ b for a, b in zip(local_nonce, remote_nonce))
                session_key = AES.new(key_bytes, AES.MODE_ECB).encrypt(xor_nonce)[:16]
                finish_hmac = hmac.new(key_bytes, remote_nonce, hashlib.sha256).digest()
                finish_payload = AES.new(key_bytes, AES.MODE_ECB).encrypt(pad(finish_hmac, 16))
                sock.send(_build_packet(1, CMD_SESS_KEY_NEG_FINISH, finish_payload))
            except Exception as exc:
                return {"ok": False, "reason": f"3.4 Session-Key-Handshake fehlgeschlagen ({exc}). 3.4-Verifikation ist experimentell -- versuche es ggf. erneut."}

            try:
                query_payload = AES.new(session_key, AES.MODE_ECB).encrypt(pad(json.dumps({"gwId": device_id, "devId": device_id, "t": str(int(time.time()))}).encode(), 16))
                sock.send(_build_packet(2, CMD_DP_QUERY, query_payload, hmac_key=session_key))
                cmd, payload = _recv_packet(sock, hmac_mode=True, timeout=timeout)
                status = _try_parse_status_payload(payload, session_key)
            except Exception as exc:
                return {"ok": False, "reason": f"3.4 Status-Abfrage nach Handshake fehlgeschlagen ({exc})."}
        else:
            # Protokoll 3.1 / 3.3 (und Fallback, falls Version unbekannt): direkte AES-ECB-Verschlüsselung mit local_key
            try:
                query_json = json.dumps({"gwId": device_id, "devId": device_id, "t": str(int(time.time()))}).encode()
                query_payload = AES.new(key_bytes, AES.MODE_ECB).encrypt(pad(query_json, 16))
                sock.send(_build_packet(0, CMD_DP_QUERY, query_payload))
                cmd, payload = _recv_packet(sock, hmac_mode=False, timeout=timeout)
                status = _try_parse_status_payload(payload, key_bytes)
            except Exception as exc:
                return {"ok": False, "reason": f"Status-Abfrage fehlgeschlagen ({exc})."}

        if status:
            return {"ok": True, "reason": "Gültige, entschlüsselte Status-Antwort vom Gerät erhalten.", "sample": status}
        return {"ok": False, "reason": "Antwort erhalten, aber nicht entschlüsselbar/parsebar -- Local Key oder Protokoll-Version stimmen vermutlich nicht."}
    finally:
        try:
            sock.close()
        except Exception:
            pass


@app.route("/verify", methods=["POST"])
def verify():
    data = request.get_json(force=True) or {}
    device_id = data.get("id")
    dev = next((d for d in STATE["devices"] if d.get("id") == device_id), None)
    if not dev:
        return jsonify({"ok": False, "reason": "Gerät nicht im Cache gefunden -- erneut laden."}), 404
    result = verify_device_live(dev.get("ip"), dev.get("id"), dev.get("local_key"), dev.get("version"))
    return jsonify(result)


@app.route("/scan_local", methods=["POST"])
def scan_local():
    """Antwort: {"found": {gwId: {"ip": ..., "version": "3.3"}}} - aktualisiert zusätzlich
    den Geräte-Cache (STATE['devices']), damit /search sofort aktuelle IPs liefert."""
    data = request.get_json(force=True) or {}
    timeout = int(data.get("timeout", 10))
    timeout = max(3, min(timeout, 30))
    try:
        mapping = scan_local_network(timeout=timeout)
        for dev in STATE["devices"]:
            hit = mapping.get(dev.get("id"))
            if hit:
                dev["ip"] = hit["ip"]
                dev["version"] = hit["version"]
        STATE["scan_time"] = time.time()
        save_state()
        return jsonify({"found": mapping})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def _format_device_text(d):
    lines = [
        f"Name: {d.get('name') or ''}",
        f"IP-Adresse: {d.get('ip') or 'UNBEKANNT – Netzwerk scannen'}",
        f"Device ID: {d.get('id') or ''}",
        f"Local Key: {d.get('local_key') or ''}",
        f"Protokoll-Version: {d.get('version') or 'auto (nicht gescannt)'}",
    ]
    if d.get("sub"):
        lines.append("Hinweis: Sub-Gerät hinter einem Gateway (z.B. Zigbee) — in localtuya meist nicht direkt lokal ansteuerbar.")
    if d.get("online") is False:
        lines.append("Hinweis: Gerät ist laut Tuya Cloud aktuell offline.")
    return "\n".join(lines)


@app.route("/search")
def search():
    query = (request.args.get("q") or "").strip().lower()
    if not query:
        return jsonify({"matches": []})

    devices = STATE["devices"]

    # Exakter Treffer (Gross-/Kleinschreibung egal) -> nur diesen einen zeigen
    exact = [d for d in devices if (d.get("name") or "").strip().lower() == query]
    if exact:
        top = exact[:1]
    else:
        scored = []
        for dev in devices:
            name = (dev.get("name") or "").lower()
            if not name:
                continue
            if query in name:
                score = 1.0 + (len(query) / max(len(name), 1))  # Substring-Treffer priorisieren
            else:
                score = difflib.SequenceMatcher(None, query, name).ratio()
            if score > 0.35:
                scored.append((score, dev))
        scored.sort(key=lambda x: x[0], reverse=True)
        top = [d for _, d in scored[:5]]

    matches = [{"device": d, "text": _format_device_text(d)} for d in top]
    return jsonify({
        "matches": matches,
        "total_cached": len(devices),
        "scan_time": STATE["scan_time"],
    })


@app.route("/state")
def get_state():
    return jsonify({
        "devices": STATE["devices"],
        "scan_time": STATE["scan_time"],
        "uid": STATE["uid"],
    })


@app.route("/")
def index():
    return render_template("index.html", options=load_options())


@app.route("/extract", methods=["POST"])
def extract():
    data = request.get_json(force=True)
    access_id = data.get("access_id", "").strip()
    access_secret = data.get("access_secret", "").strip()
    region = data.get("region", "eu")
    username = data.get("username", "").strip()
    manual_uid = data.get("uid", "").strip()

    if not access_id or not access_secret:
        return jsonify({"error": "Access ID und Access Secret werden benoetigt."}), 400

    client = TuyaClient(access_id, access_secret, region)

    try:
        client.get_token()

        uid = manual_uid
        if not uid:
            if not username:
                return jsonify({"error": "Bitte Username oder UID angeben."}), 400
            uid = client.find_uid_by_username(username)

        devices = client.get_devices_for_uid(uid)

        results = []
        for idx, dev in enumerate(devices):
            device_id = dev.get("id") or dev.get("device_id")
            local_key = dev.get("local_key")
            ip = dev.get("ip")
            online = dev.get("online")
            if not ip or not local_key or online is None:
                if idx > 0:
                    time.sleep(0.35)  # Trial-Projekte: meist nur 1 QPS erlaubt
                detail = client.get_device_detail(device_id)
                local_key = local_key or detail.get("local_key")
                ip = ip or detail.get("ip")
                online = detail.get("online") if online is None else online
            results.append({
                "name": dev.get("name"),
                "id": device_id,
                "local_key": local_key,
                "ip": ip,
                "online": online,
                "category": dev.get("category"),
                "product_name": dev.get("product_name"),
                "sub": dev.get("sub", False),
                "version": "",
            })

        STATE["devices"] = results
        STATE["uid"] = uid
        save_state()
        return jsonify({"uid": uid, "devices": results})

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/download.yaml", methods=["POST"])
def download_yaml():
    """WICHTIG: localtuya akzeptiert seit v4.0.0 KEIN YAML mehr (nur noch Config Flow
    über die UI). Diese Datei ist eine Referenz-/Kopiervorlage zum manuellen Ausfüllen
    des 'Add device'-Dialogs in Settings -> Devices & Services -> LocalTuya."""
    devices = request.get_json(force=True).get("devices", [])
    localtuya_entries = []
    for d in devices:
        localtuya_entries.append({
            "host": d.get("ip") or "IP_UNBEKANNT_MANUELL_EINTRAGEN",
            "device_id": d.get("id"),
            "local_key": d.get("local_key"),
            "friendly_name": d.get("name"),
            "protocol_version": d.get("version") or "auto (aus lokalem Scan ermitteln)",
            "sub_device_hinter_gateway": bool(d.get("sub")),
        })
    yaml_str = "# REFERENZ-Liste, kein direkter localtuya-Import (v4+ akzeptiert kein YAML mehr).\n"
    yaml_str += "# Werte manuell im 'Add device'-Dialog von localtuya eintragen.\n"
    yaml_str += yaml.dump({"localtuya_devices_reference": localtuya_entries}, allow_unicode=True, sort_keys=False)
    return Response(
        yaml_str,
        mimetype="application/x-yaml",
        headers={"Content-Disposition": "attachment;filename=localtuya_devices_reference.yaml"},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8099, threaded=True)
