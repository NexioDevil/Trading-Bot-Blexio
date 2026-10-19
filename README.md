# Trading Bot

Docker-Anwendung für explizit beauftragte Solana-Mainnet-Swaps über Jupiter.
Das Image enthält den CLI-Client, lokale Wallet-Signierung und ein persistentes Orderbuch.

[Einrichtung, Order-Befehle und Veröffentlichung](trading-bot/README.md)

```bash
docker build -t trading-bot:latest .
docker run --rm --network none trading-bot:latest --live-status
```

Zugangsdaten werden erst beim Start des Containers bereitgestellt. Das Repository
enthält ausschließlich leere Schlüsselvorlagen und keine persönlichen Profile.
