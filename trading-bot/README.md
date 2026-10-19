# Trading Bot für Docker

Der CLI-Client signiert explizit angegebene Solana-Mainnet-Swaps lokal und
übermittelt sie an Jupiter Swap API V2. Helius liefert Guthaben, Token-Daten und
On-chain-Bestätigungen. Jede Order erhält eine dauerhafte ID im Orderbuch.
Der Container öffnet keinen HTTP-Port.

Alle folgenden Befehle werden im Hauptverzeichnis des Repositorys ausgeführt.
Voraussetzung: Docker mit Compose v2 und BuildKit. Das Image verwendet Linux;
unter Windows/macOS Docker Desktop mit Linux-Containern nutzen.

## Image bauen

```bash
docker build -t trading-bot:latest .
docker run --rm --network none trading-bot:latest --live-status
```

Ohne weiteren Befehl zeigt der Container ausschließlich den Konfigurationsstatus.
Das Image enthält keine kontobezogenen Werte. Der Build-Kontext erlaubt nur
Dockerfile, Python-Programmdateien und die Abhängigkeitsliste.

## API-Schlüssel und Wallet einrichten

1. Eigenen Helius-Key unter <https://dashboard.helius.dev> erstellen.
2. Eigenen Jupiter-Key unter <https://developers.jup.ag/portal> erstellen.
3. Die leere Konfigurationsvorlage kopieren:

```bash
cp .env.example .env
mkdir -p secrets orders
```

Unter Windows PowerShell: `Copy-Item .env.example .env` und
`New-Item -ItemType Directory -Force secrets, orders`.

Die Datei `.env` lokal im Editor öffnen:

```dotenv
HELIUS_API_KEY=DEIN_EIGENER_HELIUS_KEY
JUPITER_API_KEY=DEIN_EIGENER_JUPITER_KEY
HOST_SOLANA_KEYPAIR_FILE=./secrets/trading-wallet.json
LIVE_TRADING_ENABLED=false
```

Die Platzhalter durch die eigenen Werte ersetzen. Schlüssel nicht in Quellcode,
Dockerfile, Build-Argumente, Image-Labels oder ein veröffentlichtes Archiv schreiben.
Compose liest `.env` zur Laufzeit. Andere Secret-Manager können dieselben Variablen
in die Container-Umgebung einbinden.

Der Bot benötigt eine eigene signierfähige Solana-Wallet als JSON-Liste aus 64
Byte-Werten. Eine öffentliche Adresse oder Seed-Phrase reicht dafür nicht.
Eine separate Handelswallet verwenden und ihre Datei unter dem konfigurierten
Host-Pfad speichern. Mit lokal installierter Solana-CLI lässt sich eine neue
Wallet erstellen:

```bash
solana-keygen new --outfile secrets/trading-wallet.json
solana-keygen pubkey secrets/trading-wallet.json
```

Die Datei wird schreibgeschützt nach `/run/secrets/trading-wallet.json` gemountet.
Sie muss für Container-UID 1000 lesbar sein. Auf Linux mit Host-UID 1000:

```bash
chmod 700 secrets
chmod 400 secrets/trading-wallet.json
```

Bei anderer UID Dateieigentümer, Zugriffsrechte und gegebenenfalls Container-UID
passend konfigurieren. Die Datei nicht pauschal weltlesbar machen. Unter Windows
entsprechende Dateizugriffsrechte setzen.

`.env*`, `secrets/`, `orders/`, `state/` und SQLite-Dateien werden von Git ignoriert;
`.env.example` enthält ausschließlich Vorlagenwerte. Der Docker-Build schließt
auch Git-Historie, Tests, Orderdateien und Wallets aus.

Ohne Compose unterstützt der Client alternativ `SOLANA_PRIVATE_KEY` als
serverseitige Base58-Keypair-Variable. Genau eine Quelle setzen:
`SOLANA_KEYPAIR_FILE` oder `SOLANA_PRIVATE_KEY`. Private Schlüssel niemals als
Kommandozeilenargument übergeben.

## Konfiguration prüfen

```bash
docker compose build
docker compose run --rm trading-bot --live-status
docker compose run --rm trading-bot --check-helius
```

Die Wallet-Datei muss für Compose bereits existieren. `--live-status` zeigt nur,
welche Konfigurationsbestandteile gesetzt sind; es validiert keine Schlüssel
und übermittelt keine Order. Der Helius-Test prüft den RPC-Zugang.

## Order ausführen

Die Ordervorlage enthält ausschließlich öffentliche Token-Mints und neutrale Werte:

```bash
cp trading-bot/examples/live-order.json orders/my-order.json
```

Orderdatei lokal anpassen:

```json
{
  "client_order_id": "order-001",
  "input_mint": "So11111111111111111111111111111111111111112",
  "output_mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
  "amount": "0.001",
  "slippage_bps": 50
}
```

Diese Order verkauft 0.001 SOL gegen USDC. `amount` ist der Eingangsbetrag als
Dezimaltext. 50 BPS entsprechen 0,5 % Slippage. Für jeden neu beabsichtigten Swap
eine neue ID verwenden. Anschließend in `.env` aktivieren:

```dotenv
LIVE_TRADING_ENABLED=true
```

**Der folgende Befehl kann echtes Geld bewegen:**

```bash
docker compose run --rm trading-bot --live-order /orders/my-order.json
```

Es werden nicht-gaslose Metis-Routen mit einem Signierer unterstützt. Der Client
prüft Guthaben, Betrag, Mints, Slippage, Preiseinfluss, geschätzte Gebühren,
Gebührenzahler und Ablauf vor der Signierung. Das sind keine vollständige
semantische Prüfung aller Instruktionen oder garantierte On-chain-Ausgabenlimits.
Jupiter und RPC bleiben Vertrauensabhängigkeiten; Route und Liquidität müssen
verfügbar sein.

## Orderstatus und Wiederholungen

```bash
docker compose run --rm trading-bot --live-order-status order-001
docker compose run --rm trading-bot --live-reconcile order-001
```

- `confirmed`: Signatur on-chain confirmed/finalized und ohne Fehler.
- `failed`: On-chain fehlgeschlagen; Netzwerkgebühren können angefallen sein.
- `rejected`: Vor der Übermittlung abgelehnt.
- `submitted`: Signatur bekannt, Bestätigung steht aus.
- `unknown`: Ausgang nicht eindeutig; Signatur abgleichen.
- `preparing`, `quoted`, `submitting`: Verarbeitung wurde noch nicht abgeschlossen.

Exitcodes: 0 für bestätigte Order oder erfolgreichen Informationsbefehl,
2 für abgelehnte/fehlgeschlagene Order oder ungültige Konfiguration,
3 für ausstehende bzw. unklare Verarbeitung.

Dieselbe ID führt keine zweite Order aus. Bei unklarem Ausgang keine neue ID
verwenden, bevor die bestehende Transaktion geklärt ist. Das Volume `order-state`
nicht löschen: Es enthält die IDs, öffentliche Wallet-Adresse, Absicht, Signatur
und Ergebnisdaten. Private Schlüssel und signierte Transaktionsbytes werden
nicht darin gespeichert. Das Orderbuch ist nutzerspezifisch und gehört nicht
in veröffentlichte Images oder Archive.

Pro Wallet wird jeweils eine Order verarbeitet. Eine noch offene oder ungeklärte
Order blockiert weitere IDs derselben Wallet, damit Guthaben nicht mehrfach
verplant wird. Alle Container derselben Wallet müssen dafür dasselbe Order-Volume
verwenden. Unabhängige Kopien des Orderbuchs bieten keine gemeinsame Sperre.

Nach einem Prozessabbruch `--live-reconcile` verwenden. Ohne gespeicherte Signatur
kann eine abgebrochene Vorbereitung sicher als abgelehnt geschlossen werden,
sobald kein Prozess mehr daran arbeitet. Mit Signatur wird nur der On-chain-Status
abgefragt; es erfolgt keine erneute Übermittlung.

Bei eigenem `docker run` für echte Orders dasselbe persistente Volume nach `/state`
einbinden. Der Image-Standardpfad ist `/state/orders.sqlite3`.

## Grenzen konfigurieren

| Variable | Standard / Bedeutung |
| --- | --- |
| LIVE_ALLOWED_MINTS | SOL und USDC; erlaubte Eingangs- und Ausgangs-Mints |
| LIVE_MAX_INPUT_AMOUNTS | Maximaler Eingangsbetrag pro Order: SOL 0.01, USDC 1 |
| LIVE_SOL_RESERVE | 0.01 SOL zusätzlich zu Eingang und geschätzten Gebühren |
| LIVE_MAX_SLIPPAGE_BPS | Höchstens 100 BPS = 1 % |
| LIVE_MAX_PRICE_IMPACT_PCT | Höchstens 2 Prozentpunkte laut Quote |
| LIVE_MAX_NETWORK_FEE_LAMPORTS | Höchstens 10.000.000 geschätzte Lamports |

Jedes erlaubte Eingangstoken benötigt eine eigene Grenze in
`LIVE_MAX_INPUT_AMOUNTS`. Überschreitungen werden abgelehnt.
Beträge werden exakt in kleinste Token-Einheiten umgerechnet; nicht darstellbare
Nachkommastellen werden abgelehnt. Mindest-Auszahlung und Gebührenzahler der Quote
müssen zu den angeforderten Grenzen und der eigenen Wallet passen.
Der Client führt ausdrücklich beauftragte Einzelorders aus. Ein automatischer
Fomo-Profilimport bzw. automatisches proportionales Copy-Trading ist nicht enthalten.

## Image veröffentlichen

Eigene Registry und eigenen Namespace einsetzen:

```bash
docker login
# REGISTRY-NAMESPACE durch den eigenen Docker-Hub-Namespace ersetzen.
docker build -t REGISTRY-NAMESPACE/trading-bot:1.0.0 .
docker push REGISTRY-NAMESPACE/trading-bot:1.0.0
```

Bei anderen Registries mit deren Hostnamen anmelden und den vollständigen
Image-Namen verwenden. Dockerfile, `.dockerignore`, `compose.yaml`,
`.env.example` und die erlaubten Quelldateien können ohne Zugangsdaten verteilt
werden. Zugangsdaten und Handelswallet richtet jeder Betreiber selbst ein.
Dieses Repository führt keinen automatischen Registry-Push aus.

Um ein veröffentlichtes Image mit Compose zu verwenden, lokal setzen:

```dotenv
TRADING_BOT_IMAGE=REGISTRY-NAMESPACE/trading-bot:1.0.0
```

```bash
docker compose pull
docker compose run --rm --no-build trading-bot --live-status
```

## Betrieb und Prüfung

HTTPS-Zugriff auf `mainnet.helius-rpc.com` und `api.jup.ag` erlauben.
In einer verwalteten Umgebung mit eigener CA:

```bash
docker compose -f compose.yaml -f compose.cloud.yaml build
docker compose -f compose.yaml -f compose.cloud.yaml run --rm trading-bot --live-status
```

Die CA wird beim Build als temporäres Secret und zur Laufzeit schreibgeschützt
eingebunden. TLS-Prüfung bleibt aktiv.

Lokale Tests ohne Geldbewegungen:

```bash
python -m pip install -r trading-bot/requirements.txt
cd trading-bot
python -m unittest discover -s tests -v
```

Die Tests verwenden erzeugte, unfinanzierte Schlüssel und kontrollierte
Transportantworten. Ein erfolgreicher Test belegt keine tatsächliche
Mainnet-Ausführung mit den eigenen Zugangsdaten.

## Veröffentlichung auf sensible Daten prüfen

Im Hauptverzeichnis vor einem Upload:

```bash
python scripts/check_publication.py
python scripts/check_publication.py --history
```

Die Prüfung betrachtet Git-erfasste Dateien, leere Schlüsselvorlagen und optional
Commit-E-Mail-Adressen. Treffer werden ohne Schlüsselwerte oder Adressen gemeldet.
Sie ergänzt die eigene Prüfung; sie erkennt nicht jede denkbare Art persönlicher
Informationen. Für neue Commits die GitHub-`noreply`-Adresse des Projektkontos
verwenden und auf GitHub unter „Settings → Emails“ den E-Mail-Schutz aktivieren.
Bereits veröffentlichte Commits können auch nach einer Bereinigung in externen
Kopien oder GitHub-Zwischenspeichern verbleiben.

## Verantwortung

Der Betreiber entscheidet über Wallet, Beträge und Freigaben und trägt das
wirtschaftliche Risiko bis zum vollständigen Verlust des eingesetzten Kapitals.
Es gibt keine Gewinn-, Preis- oder Geschwindigkeitsgarantie. Blockchain-Transaktionen
lassen sich in der Regel nicht rückgängig machen. Zwingende gesetzliche Ansprüche
bleiben unberührt.
