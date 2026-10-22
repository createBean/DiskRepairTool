# NTFS-Datenrettung (nur lesend)

[English](README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [Español](README.es.md) | **Deutsch**

Ein lokales Web-Tool, das Dateien aus einem beschädigten NTFS-Datenträger rettet – von der Sorte, die Windows als **RAW** anzeigt, nicht öffnet und immer wieder zum Formatieren auffordert.

Es liest den Rohdatenträger, durchsucht die gesamte MFT (Master File Table) und rekonstruiert den Verzeichnisbaum aus der Elternreferenz, die in jedem Datensatz gespeichert ist. Deshalb funktioniert es auch dann, wenn das Wurzelverzeichnis selbst zerstört wurde – und es behält Dateinamen und Ordnerstruktur bei.

**Nur lesend: Es schreibt niemals auf den Quelldatenträger.**

---

![Oberfläche](docs/screenshot.png)

## Warum es funktioniert, wenn andere Tools scheitern

Herkömmliche Rettungstools laufen die Kette *Datenträger → Wurzelverzeichnis → Unterverzeichnis → Datei* ab. Ist MFT-Datensatz Nr. 5 – das Wurzelverzeichnis – beschädigt, bricht die Kette schon beim ersten Glied. Windows markiert den Datenträger als RAW, die meisten Tools sehen eine leere Platte, und die, die weitermachen, weichen auf Signatur-Carving aus, bei dem sämtliche Dateinamen verloren gehen.

NTFS-Dateidatensätze sind **in sich geschlossen**. Jeder 1024-Byte-Datensatz enthält den Dateinamen, die Datensatznummer des Elternverzeichnisses und die Cluster, in denen die Daten liegen. Dieses Tool ignoriert das Wurzelverzeichnis daher vollständig:

1. Bootsektor lesen, um MFT-Startposition und Clustergröße zu ermitteln
2. die eigenen Daten-Runs von `$MFT` auflösen – die MFT ist selbst eine Datei und kann in hunderte Extents fragmentiert sein, die über mehrere Datensätze verteilt sind
3. alle MFT-Datensätze sequenziell durchlaufen und aus jedem *Elternreferenz + Dateiname* ziehen
4. rückwärts aggregieren und den gesamten Baum rekonstruieren

---

## Funktionen

- **Übersteht ein zerstörtes Wurzelverzeichnis** – die häufigste Ursache dafür, dass ein Datenträger zu RAW wird
- **Behält Dateinamen und Ordner** – anders als Signatur-Carving
- **Jeder Dateityp** – Fotos, Videos, Dokumente, Archive; keine Filterung nach Endung
- **Hängt nicht bei defekten Sektoren** – jeder Block wird in einem eigenen Thread mit Timeout gelesen; unlesbare Bereiche werden übersprungen und gezählt
- **Lokale Weboberfläche** – Datenträgerliste, Diagnose, Scan-Fortschritt, Ordnerbaum, Typfilter, Extraktionsfortschritt
- **Scan-Ergebnisse werden zwischengespeichert** – erneutes Öffnen erfordert keinen neuen Scan
- **Kein Framework, kein Build-Schritt** – HTTP-Server aus der Standardbibliothek und reines JavaScript
- **Strikt nur lesend** – kein Codepfad öffnet das Quellgerät zum Schreiben

---

## Voraussetzungen

- Windows (Rohgerätezugriff ist Windows-spezifisch)
- Python 3.8 oder neuer
- `dissect.ntfs` – optional; wird nur benötigt, um die Runs von `$MFT` aufzulösen, wenn diese stark fragmentiert ist

---

## Schnellstart

```bash
git clone https://github.com/createBean/DiskRepairTool.git
cd DiskRepairTool
pip install dissect.ntfs
```

Danach `启动.bat` doppelklicken. Es fordert selbst Administratorrechte an, installiert die Abhängigkeit falls nötig und öffnet die Oberfläche im Browser.

Alternativ manuell starten:

```bash
python server.py
```

Der Server lauscht auf `http://127.0.0.1:8765/`.

Kein defekter Datenträger zur Hand? `http://127.0.0.1:8765/?demo=1` öffnen, um die Oberfläche mit Beispieldaten anzusehen.

---

## Bedienung

1. **Datenträger wählen** – das defekte Laufwerk anhand von Modell und Kapazität auswählen
2. **Partition wählen und diagnostizieren** – prüft Bootsektor und die ersten 16 MFT-Datensätze und sagt, ob das Wurzelverzeichnis beschädigt ist
3. **Scannen** – liest die gesamte MFT und baut den Baum neu auf; eine MFT mit Millionen Datensätzen dauert einige Minuten
4. **Durchsuchen und auswählen** – durch den Ordnerbaum navigieren und die gewünschten Dateien ankreuzen; das Panel listet außerdem die Ordner mit den meisten Bildern
5. **Extrahieren** – Zielordner wählen und kopieren; Fortschritt und Byteanzahl werden live angezeigt

---

## Grenzen

**Funktioniert bei** logischer NTFS-Beschädigung: fehlendes Wurzelverzeichnis, beschädigte Partitionstabelle, zerstörte Dateisystemstrukturen, Datenträger nach Stromausfall oder unsicherem Entfernen auf RAW gesetzt.

**Funktioniert nicht bei**:

- **Physischem Schaden** – Laufwerke mit Klacken, Schleifen, Sturz- oder Wasserschaden. Nicht einschalten; sie gehören in einen Reinraum.
- **exFAT / FAT32** – andere On-Disk-Strukturen, nicht implementiert
- **Verschlüsselten Datenträgern** – BitLocker / VeraCrypt müssen zuerst entsperrt werden
- **Überschriebenen Datensätzen** – zeigt der Datensatz einer Datei inzwischen auf wiederverwendete Cluster, lässt sich der Inhalt darüber nicht retten; nur Signatur-Carving hilft, und das verliert die Namen

---

## Fehlerbehebung

**Der Datenträger hat keinen Laufwerksbuchstaben.** Mit erhöhten Rechten öffnet das Tool `\\.\PhysicalDriveN`, ein Buchstabe ist dann nicht nötig. Ohne Erhöhung wird auf ein Volume-Handle zurückgefallen – dann wird einer gebraucht.

**Die Antivirus-Software schlägt an.** Das ist zu erwarten: das Verhalten (alle Datenträger durchlaufen, Gigabytes lesen) ähnelt Ransomware. Der Quellcode ist kurz und prüfbar, und Geräte werden ausschließlich lesend geöffnet.

**Eine gebaute exe startet nicht.** Smart App Control und SmartScreen von Windows blockieren unsignierte Executables ohne Reputation. Aus dem Quellcode starten oder die Binärdatei signieren.

**Der Scan findet nichts.** Zuerst die Diagnose prüfen: Ist der Bootsektor kein NTFS oder lassen sich die MFT-Runs nicht auflösen, ist der Datenträger physisch beschädigt oder nutzt ein anderes Dateisystem.

---

## Sicherheit

- Das Programm liest ausschließlich vom Quelldatenträger; kein Codepfad öffnet ihn zum Schreiben
- Administratorrechte werden nur benötigt, um ein Rohgeräte-Handle zu öffnen
- Der einzige Schreibvorgang erfolgt in den von Ihnen gewählten Zielordner
- Die Rettung erfolgt nach bestem Bemühen – kein Tool kann jede Datei garantieren

---

## Lizenz

MIT
