# Deal-Radar

Dein persönlicher Deal-Finder: du trägst Produkte ein, der Radar prüft alle 10 Minuten
Deal-Quellen, schickt passende Treffer per Push aufs Handy und lernt aus deinen 👍/👎.
Für Handytarife mit Gerät rechnet er aus, ob nach dem Verkauf des Handys Gewinn übrig bleibt.

Komplett kostenlos: GitHub (Abruf + Web-App) und ntfy (Push).

## Einrichtung (einmalig, ca. 15 Minuten, am besten am PC)

1. **GitHub-Konto** anlegen auf github.com.
2. **Repository** anlegen: „New repository“ → Name `deal-radar` → **Public** → erstellen.
   Dann „uploading an existing file“ und alle Dateien aus dem Zip hineinziehen,
   inklusive des Ordners `.github` (auf dem Mac ist er versteckt: im Finder `Cmd + Shift + .`).
3. **Push-App**: „ntfy“ aus dem App Store / Play Store installieren → „+“ →
   Thema abonnieren: `deal-radar-60f092136554` (oder einen eigenen, schwer erratbaren Namen).
4. **Push-Thema hinterlegen**: im Repo → Settings → Secrets and variables → Actions →
   „New repository secret“ → Name `NTFY_TOPIC`, Wert = dein Thema aus Schritt 3.
5. **Web-App einschalten**: Settings → Pages → Source „Deploy from a branch“ → Branch `main`, Ordner `/ (root)` → Save.
6. **Radar starten**: Reiter „Actions“ → Workflows aktivieren → „Deal-Radar“ → „Run workflow“.
7. **Zugangs-Token** für die App: Profilbild → Settings → Developer settings →
   Personal access tokens → Fine-grained tokens → „Generate new token“ →
   Repository access: *Only select repositories* → `deal-radar` →
   Permissions: **Contents: Read and write** → erstellen und kopieren.
8. **App öffnen**: `https://DEINNAME.github.io/deal-radar` → Einstellungen → Token einfügen → Verbinden.
   Auf dem iPhone: in Safari Teilen → „Zum Home-Bildschirm“ – dann startet es mit eigenem Icon im Vollbild wie eine App.

## Bedienung

- **Watchlist**: Produkte/Abos mit Suchbegriffen anlegen. Mehrere Begriffe mit Komma = „oder“.
  „Ausschließen“ filtert Zubehör raus (z. B. `controller, hülle`).
- **Rubriken**: „Rabatte & Gutscheine“ (🏷️) und „Gewinnspiele“ (🎁) sammeln alles dieser Art,
  ganz ohne Suchbegriff. Push ist dort standardmäßig aus, sonst kommt zu viel – in der Watchlist einschaltbar.
- **Preisvergleich**: jeder Deal hat Direktlinks zu idealo, Geizhals, Google Shopping, billiger.de,
  Amazon und eBay. Steht im Deal ein Vergleichspreis, zeigt der Radar die Ersparnis in %.
  Pro Produkt merkt er sich den Bestpreis seit Start.
- **Deals**: alle Treffer; 👍/👎 trainiert die „Passt zu dir“-Bewertung (ab je 2 Bewertungen).
- **Rechner**: Tarif-Gewinnrechnung. Unter „Wiederverkaufswerte“ trägst du ein, was du für
  ein neues Gerät beim Verkauf bekommst – dann rechnet der Radar jeden Tarif-Deal automatisch.
- **Einstellungen**: Push erst ab einer bestimmten „Passt“-Prozentzahl, Status der Quellen.

## Gut zu wissen

- Das Repo ist öffentlich: Watchlist und gefundene Deals sind dort einsehbar. Token und
  Push-Thema nicht (die liegen im Browser bzw. als Secret).
- Die Tarif-Werte werden aus dem Deal-Text gelesen und können danebenliegen –
  vor dem Abschluss immer mit dem Rechner und den echten Vertragsdetails gegenprüfen.
- Eigene mydealz-Schlagwort-Alarme lassen sich als zusätzliche Quelle in `config.json` unter
  `feeds` eintragen (Feed-Adresse `https://www.mydealz.de/rssx/keyword-alarm/…`).
- Auf einem lokalen Rechner testen: `python radar.py` (ohne Push, wenn `NTFY_TOPIC` fehlt).
