# Tuya Local Key Extractor – HA Addon

Extrahiert Device-IDs, Local Keys und IPs aus deinem Tuya Cloud Account für die `localtuya` Integration.

## Einmalige Vorbereitung (Tuya IoT Platform)

1. Account anlegen auf https://iot.tuya.com
2. **Cloud → Create Project** → Beliebiger Name, "Smart Home" auswählen, Data Center = deine Region (EU/US/CN/IN)
3. Im Projekt: **Service API** → sicherstellen, dass "IoT Core", "Authorization" und "Smart Home Scene Linkage" aktiviert sind (Standard bei Trial)
4. **Devices → Link Tuya App Account → Add App Account → Automatic Link** → QR-Code mit der Smart Life / Tuya Smart App scannen (Account, mit dem deine Geräte verknüpft sind)
5. Im Projekt-Overview: **Access ID/Client ID** und **Access Secret/Client Secret** kopieren

## Addon installieren

1. In Home Assistant: **Einstellungen → Add-ons → Add-on Store → ⋮ → Repositories**
2. Repo-URL hinzufügen (nachdem du diesen Ordner z.B. nach `Pitbull19850119/tuya-local-key-extractor-addon` gepusht hast)
3. Addon installieren, starten, Web-UI (Ingress) öffnen

## Nutzung

1. Access ID, Access Secret, Region eintragen
2. Tuya-Account-Username (E-Mail/Telefonnummer der App) eintragen
3. "Verbinden & Geräte laden" klicken → lädt Cloud-Daten und startet automatisch einen Netzwerk-Scan (IP + Protokollversion)
4. Gerätename eingeben (Tippfehler-tolerant) → Textblock mit Name/IP/Device ID/Local Key/Version erscheint, zeilenweise oder per Button kopierbar
5. Optional: "✅ Live-Test" klicken → das Addon verbindet sich **wirklich** lokal mit dem Gerät (TCP 6668) und fragt per Tuya-Protokoll den Status ab. Nur bei einer erfolgreich entschlüsselten Antwort erscheint der grüne Haken — jeder Fehler (falscher Key, falsche IP, Timeout) führt zu einer orangen "nicht verifiziert"-Meldung, nie zu einem falschen Haken.
6. Werte in localtuya eintragen: **Settings → Devices & Services → LocalTuya → Add device**

## Persistenter Cache

Geladene Geräte (inkl. local_keys) werden nach `/data/device_cache.json` gespeichert und überstehen einen Addon-Neustart. **Sicherheitshinweis:** diese Datei liegt unverschlüsselt auf der Home-Assistant-Disk/SD-Karte, genau wie die Addon-Optionen selbst. Wer physischen oder Root-Zugriff auf dein HA-System hat, könnte sie lesen.

## Grenzen der Live-Verifikation

- Protokoll 3.3: zuverlässig getestet (Standard-AES-ECB-Direktverschlüsselung mit local_key)
- Protokoll 3.4: Session-Key-Handshake implementiert und gegen ein simuliertes Gerät erfolgreich getestet, aber ohne echtes 3.4-Gerät hier nicht validierbar — bei Problemen erneut versuchen
- Protokoll 3.1: läuft über denselben Pfad wie 3.3; manche 3.1-Firmwares antworten unverschlüsselt, das wird ebenfalls abgefangen

## Hinweise

- Falls die Username→UID-Suche fehlschlägt (Tuya ändert diese Endpoints gelegentlich), kannst du die UID manuell eintragen. Die UID findest du in der Tuya IoT Platform unter **Devices → Link Tuya App Account** bei deinem verknüpften Account.
- IPs werden nur korrekt geliefert, wenn das Gerät online ist und sich im gleichen Netz-Kontext befindet. Sonst manuell in localtuya nachtragen.
- Access Secret niemals veröffentlichen – wird nur lokal in den Addon-Optionen gespeichert.
