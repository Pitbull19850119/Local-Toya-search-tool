# iCloud Foto-Backup

Sichert die Alben aus iCloud-Fotos jede Nacht auf eine Netzwerkfreigabe (z. B. WD My Cloud).

- Jedes Album wird ein eigener Ordner unter `Alben/` mit dem Namen aus iCloud.
- Es wird **nie** etwas gelöscht oder überschrieben – nur fehlende Fotos werden ergänzt.
- Vorhandene alte Ordner werden erkannt; umbenannt wird erst nach Bestätigung in der Oberfläche.
- Läuft nur im eingestellten Zeitfenster (Standard 23–4 Uhr) und pausiert dann.
- Push-Nachricht bei fälligem Zwei-Faktor-Code, wenig Speicher oder Fehlern.

Voraussetzungen in iCloud: „Erweiterter Datenschutz“ aus, „Auf iCloud-Daten im Web zugreifen“ an.
