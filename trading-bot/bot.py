"""Command-line entry point for explicit Solana mainnet orders."""
import argparse
import json
from pathlib import Path
from helius import HeliusClient, HeliusError
from live_trading import LiveTrader, OrderStore, live_status


def main(argv=None):
    parser=argparse.ArgumentParser(description='Solana-Mainnet-Orders über Jupiter ausführen und prüfen')
    action=parser.add_mutually_exclusive_group()
    action.add_argument('--live-status',action='store_true',help='Konfigurationsstatus ohne Schlüsselwerte anzeigen (Standard)')
    action.add_argument('--live-order',type=Path,help='Order aus einer JSON-Datei ausführen')
    action.add_argument('--live-order-status',help='Gespeicherte Order anhand ihrer ID anzeigen')
    action.add_argument('--live-reconcile',help='Öffentliche Order-Signatur on-chain abgleichen')
    action.add_argument('--check-helius',action='store_true',help='Helius-Zugang prüfen')
    args=parser.parse_args(argv)
    try:
        if args.live_order:
            result=LiveTrader().execute(json.loads(args.live_order.read_text(encoding='utf-8')))
        elif args.live_order_status:
            result=OrderStore().get(args.live_order_status)
        elif args.live_reconcile:
            result=LiveTrader().reconcile(args.live_reconcile)
        elif args.check_helius:
            result=HeliusClient().check()
        else:
            result=live_status()
        print(json.dumps(result,indent=2,allow_nan=False))
        if result.get('state') in ('unknown','submitted','submitting','preparing','quoted'):
            return 3
        if result.get('state') in ('rejected','failed'):
            return 2
        return 0
    except (ValueError,OSError,HeliusError) as exc:
        parser.error(str(exc))


if __name__=='__main__':
    raise SystemExit(main())
