"""Explicit Solana mainnet swaps through Jupiter V2. Sign keys locally; submit only explicitly requested orders."""
import base64
import fcntl
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener
from helius import HeliusClient, HeliusError, NoRedirect, validate_wallet

SOL = 'So11111111111111111111111111111111111111112'
JUPITER_PROGRAM = 'JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4'
USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'


class LiveError(ValueError):
    pass


def decimal(value, name, zero=False):
    if isinstance(value, (bool, float)):
        raise LiveError(f'{name}: Dezimalzahlen als Text angeben, nicht als JSON-Gleitkommazahl.')
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        raise LiveError(f'{name} ist keine gültige Dezimalzahl.') from None
    if not result.is_finite() or result < 0 or (not zero and result == 0):
        raise LiveError(f'{name} muss endlich und positiv sein.')
    return result


def integer(value, name):
    if isinstance(value, bool) or not re.fullmatch(r'[0-9]{1,24}', str(value)):
        raise LiveError(f'Ungültiger ganzzahliger Wert: {name}.')
    return int(value)


def token_units(value, decimals, name='Betrag', zero=False):
    """Convert exactly, without Decimal-context rounding or binary floating point."""
    amount = decimal(value, name, zero=zero)
    _, digits, exponent = amount.as_tuple()
    if amount == 0:
        return 0
    coefficient = ''.join(map(str, digits))
    shift = exponent + decimals
    if shift < 0:
        if -shift >= len(coefficient) or any(c != '0' for c in coefficient[shift:]):
            raise LiveError(f'{name} besitzt zu viele Nachkommastellen.')
        coefficient = coefficient[:shift]
    else:
        if len(coefficient) + shift > 20:
            raise LiveError(f'{name} überschreitet u64.')
        coefficient += '0' * shift
    result = int(coefficient)
    if not 0 < result <= 2**64-1:
        raise LiveError(f'{name} überschreitet u64 oder ist zu klein.')
    return result


def live_status():
    enabled = os.environ.get('LIVE_TRADING_ENABLED', '').lower() == 'true'
    return dict(live_enabled=enabled, network='solana-mainnet', provider='Jupiter Swap API V2',
                helius_key_configured=bool(os.environ.get('HELIUS_API_KEY')),
                jupiter_key_configured=bool(os.environ.get('JUPITER_API_KEY')),
                wallet_configured=bool(os.environ.get('SOLANA_KEYPAIR_FILE') or os.environ.get('SOLANA_PRIVATE_KEY')),
                signing_dependency_installed=importlib.util.find_spec('solders') is not None,
                automatic_fomo_copy=False,
                note='Explizite Mainnet-Swaps. Ohne LIVE_TRADING_ENABLED=true wird keine Order signiert.')


def load_keypair():
    try:
        from solders.keypair import Keypair
    except ImportError:
        raise LiveError('Live-Abhängigkeiten fehlen. requirements.txt installieren oder das Docker-Image verwenden.') from None
    filename, encoded = os.environ.get('SOLANA_KEYPAIR_FILE'), os.environ.get('SOLANA_PRIVATE_KEY')
    if bool(filename) == bool(encoded):
        raise LiveError('Genau eine Signierquelle setzen: SOLANA_KEYPAIR_FILE oder SOLANA_PRIVATE_KEY.')
    try:
        if filename:
            raw = json.loads(Path(filename).read_text())
            if not isinstance(raw, list) or len(raw) != 64 or any(type(v) is not int or not 0 <= v <= 255 for v in raw):
                raise ValueError
            return Keypair.from_bytes(bytes(raw))
        return Keypair.from_base58_string(encoded)
    except Exception:
        raise LiveError('Signierschlüssel nicht lesbar oder ungültig. Erwartet: Solana-JSON-Keypair (64 Bytes) bzw. Base58-Keypair; keine Seed-Phrase.') from None


class JupiterClient:
    def request(self, path, params=None, body=None):
        if path not in ('order', 'execute'):
            raise LiveError('Unzulässiger Jupiter-Endpunkt.')
        key = os.environ.get('JUPITER_API_KEY', '').strip()
        if not key:
            raise LiveError('JUPITER_API_KEY fehlt.')
        url = 'https://api.jup.ag/swap/v2/' + path
        if params:
            url += '?' + urlencode(params)
        request = Request(url, data=json.dumps(body).encode() if body is not None else None,
                          headers={'x-api-key':key, 'Content-Type':'application/json'})
        try:
            with build_opener(NoRedirect()).open(request, timeout=30) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise LiveError('Jupiter-Antwort zu groß.')
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise LiveError('Ungültige Jupiter-Antwort.')
            return data
        except HTTPError as exc:
            raise LiveError(f'Jupiter HTTP {exc.code}. Schlüssel, Kontingent und Order prüfen; keine automatische Wiederholung.') from None
        except (URLError, OSError, ValueError):
            raise LiveError('Jupiter-Anfrage fehlgeschlagen oder Antwort ungültig. Nach Übermittlung kann der Ausgang unbekannt sein.') from None


class OrderStore:
    def __init__(self, path=None):
        self.path = str(path or os.environ.get('LIVE_ORDERS_DB', '/tmp/orbit-live/orders.sqlite3'))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600); os.close(fd)
        os.chmod(self.path, 0o600)
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                          state TEXT NOT NULL, wallet TEXT, signature TEXT, request_id TEXT, message TEXT, updated REAL, intent TEXT, receipt TEXT)''')

            columns = {r[1] for r in db.execute('PRAGMA table_info(orders)')}
            for column in ('intent', 'receipt'):
                if column not in columns:
                    db.execute('ALTER TABLE orders ADD COLUMN '+column+' TEXT')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def wallet_lock(self, wallet):
        # flock is released by the OS even after a process/container crash.
        directory = Path(self.path).parent / '.wallet-locks'
        directory.mkdir(exist_ok=True, mode=0o700)
        path = directory / (hashlib.sha256(wallet.encode()).hexdigest() + '.lock')
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LiveError('Für diese Wallet läuft bereits eine Verarbeitung. Status prüfen und später erneut versuchen.') from None
            yield
        finally:
            os.close(fd)

    def get(self, identity):
        with self.db() as db:
            row = db.execute('SELECT id,state,wallet,signature,request_id,message,updated,intent,receipt FROM orders WHERE id=?', (identity,)).fetchone()
        if not row:
            raise LiveError('Order-ID ist nicht im lokalen Orderbuch vorhanden.')
        result = dict(zip(('client_order_id','state','wallet','signature','request_id','message','updated','intent','receipt'), row))
        for key in ('intent','receipt'):
            result[key] = json.loads(result[key]) if result[key] else None
        return result

    def reserve(self, identity, fingerprint, wallet, intent):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT fingerprint FROM orders WHERE id=?', (identity,)).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise LiveError('Diese Order-ID wurde bereits für andere Daten oder eine andere Wallet verwendet.')
                return False
            active = db.execute("SELECT 1 FROM orders WHERE wallet=? AND state IN ('preparing','quoted','submitting','submitted','unknown') LIMIT 1", (wallet,)).fetchone()
            if active:
                raise LiveError('Für diese Wallet ist eine Order offen oder ungeklärt. Zuerst mit --live-reconcile abgleichen.')
            db.execute('INSERT INTO orders(id,fingerprint,state,wallet,updated,intent) VALUES(?,?,?,?,?,?)',
                       (identity, fingerprint, 'preparing', wallet, time.time(), json.dumps(intent)))
        return True

    def update(self, identity, state, signature=None, request_id=None, message=None, receipt=None):
        with self.db() as db:
            db.execute('UPDATE orders SET state=?,signature=COALESCE(?,signature),request_id=COALESCE(?,request_id),message=?,updated=?,receipt=COALESCE(?,receipt) WHERE id=?',
                       (state, signature, request_id, message, time.time(), json.dumps(receipt) if receipt is not None else None, identity))
        return self.get(identity)


def validate_intent(data):
    if not isinstance(data, dict) or set(data) - {'client_order_id','input_mint','output_mint','amount','slippage_bps'}:
        raise LiveError('Ungültige Orderfelder.')
    identity = data.get('client_order_id', '')
    if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', identity):
        raise LiveError('client_order_id benötigt 1–80 Buchstaben, Ziffern, - oder _.')
    input_mint, output_mint = validate_wallet(data.get('input_mint')), validate_wallet(data.get('output_mint'))
    if input_mint == output_mint:
        raise LiveError('Eingangs- und Ausgangstoken müssen verschieden sein.')
    allowed = {v.strip() for v in os.environ.get('LIVE_ALLOWED_MINTS', SOL+','+USDC).split(',')}
    if not {input_mint,output_mint} <= allowed:
        raise LiveError('Token fehlt in LIVE_ALLOWED_MINTS.')
    if not isinstance(data.get('amount'), str) or len(data['amount']) > 128:
        raise LiveError('amount muss Dezimaltext mit höchstens 128 Zeichen sein.')
    amount = decimal(data['amount'], 'amount')
    try:
        limits = json.loads(os.environ.get('LIVE_MAX_INPUT_AMOUNTS', json.dumps({SOL:'0.01',USDC:'1'})))
        maximum = decimal(limits[input_mint], 'Maximaler Eingangs-Betrag')
    except (KeyError, TypeError, json.JSONDecodeError):
        raise LiveError('Für diesen Eingangstoken fehlt eine gültige Begrenzung in LIVE_MAX_INPUT_AMOUNTS.') from None
    if amount > maximum:
        raise LiveError('Order überschreitet den konfigurierten maximalen Eingangs-Betrag.')
    slippage = integer(data.get('slippage_bps', 50), 'slippage_bps')
    limit = integer(os.environ.get('LIVE_MAX_SLIPPAGE_BPS', '100'), 'LIVE_MAX_SLIPPAGE_BPS')
    if not 0 <= slippage <= limit <= 1000:
        raise LiveError('Slippage überschreitet die konfigurierte Grenze (höchstens 1000 BPS).')
    return dict(client_order_id=identity,input_mint=input_mint,output_mint=output_mint,amount=str(amount),slippage_bps=slippage)


class LiveTrader:
    def __init__(self, rpc=None, jupiter=None, store=None, signer=None):
        self.rpc, self.jupiter, self.store = rpc or HeliusClient(), jupiter or JupiterClient(), store or OrderStore()
        self.signer = signer

    def execute(self, data):
        if os.environ.get('LIVE_TRADING_ENABLED', '').lower() != 'true':
            raise LiveError('LIVE_TRADING_ENABLED ist nicht true. Es wird keine Transaktion signiert oder gesendet.')
        intent = validate_intent(data)
        signer = self.signer or load_keypair()
        wallet = str(signer.pubkey())
        with self.store.wallet_lock(wallet):
            return self._execute(intent, signer, wallet)

    def _execute(self, intent, signer, wallet):
        fingerprint = hashlib.sha256(json.dumps(dict(wallet=wallet,**intent),sort_keys=True).encode()).hexdigest()
        identity = intent['client_order_id']
        if not self.store.reserve(identity, fingerprint, wallet, intent):
            return self.store.get(identity)
        submitted = False
        try:
            mint = intent['input_mint']
            supply = self.rpc.call('getTokenSupply', [mint, {'commitment':'confirmed'}])
            decimals = supply.get('value', {}).get('decimals')
            if type(decimals) is not int or not 0 <= decimals <= 18:
                raise LiveError('Token-Dezimalstellen nicht verifizierbar.')
            units = token_units(intent['amount'], decimals)
            native = self.rpc.call('getBalance', [wallet, {'commitment':'confirmed'}])
            lamports = integer(native.get('value'), 'SOL-Guthaben')
            reserve = token_units(os.environ.get('LIVE_SOL_RESERVE','0.01'), 9, 'SOL-Reserve', zero=True)
            if mint == SOL:
                if lamports < units + reserve:
                    raise LiveError('Nicht genug SOL einschließlich der konfigurierten Reserve.')
            else:
                result = self.rpc.call('getTokenAccountsByOwner', [wallet, {'mint':mint}, {'encoding':'jsonParsed','commitment':'confirmed'}])
                balance = sum(integer(a['account']['data']['parsed']['info']['tokenAmount']['amount'],'Token-Guthaben') for a in result['value'])
                if balance < units:
                    raise LiveError('Nicht genug Guthaben im Eingangstoken.')
            quote = self.jupiter.request('order', params=dict(inputMint=mint,outputMint=intent['output_mint'],amount=str(units),
                            taker=wallet,swapMode='ExactIn',slippageBps=intent['slippage_bps'],excludeRouters='jupiterz,dflow,okx',
                            maxSupportedTransactionVersion='0'))
            if (quote.get('errorCode') is not None or quote.get('router') != 'metis' or quote.get('gasless') is not False or quote.get('signatureFeePayer') != wallet
                    or quote.get('swapMode') != 'ExactIn' or quote.get('inputMint') != mint
                    or quote.get('outputMint') != intent['output_mint'] or integer(quote.get('inAmount'),'Quote-Eingang') != units
                    or integer(quote.get('outAmount'),'Quote-Ausgang') <= 0
                    or integer(quote.get('otherAmountThreshold'),'Minimaler Ausgang') <= 0
                    or integer(quote.get('slippageBps'),'Quote-Slippage') > intent['slippage_bps']):
                raise LiveError('Quote passt nicht zur angeforderten Order oder Route wird nicht unterstützt.')
            expected_out = integer(quote['outAmount'], 'Quote-Ausgang')
            minimum_out = integer(quote['otherAmountThreshold'], 'Minimaler Ausgang')
            required_out = expected_out * (10000 - intent['slippage_bps']) // 10000
            if not max(1, required_out) <= minimum_out <= expected_out:
                raise LiveError('Mindest-Auszahlung der Quote verletzt die angeforderte Slippage-Grenze.')
            impact = decimal(str(abs(Decimal(str(quote['priceImpact'])))), 'Preiseinfluss', zero=True)
            if impact > decimal(os.environ.get('LIVE_MAX_PRICE_IMPACT_PCT','2'), 'Maximaler Preiseinfluss'):
                raise LiveError('Quote überschreitet den konfigurierten Preiseinfluss.')
            fees = sum(integer(quote.get(k),k) for k in ('signatureFeeLamports','prioritizationFeeLamports','rentFeeLamports'))
            if fees > integer(os.environ.get('LIVE_MAX_NETWORK_FEE_LAMPORTS','10000000'),'Netzwerkgebühren-Grenze'):
                raise LiveError('Geschätzte Netzwerkgebühren überschreiten die Grenze.')
            if lamports < reserve + fees + (units if mint == SOL else 0):
                raise LiveError('Nicht genug SOL für Eingang, Gebühren und Reserve.')
            request_id = quote.get('requestId')
            if not isinstance(request_id,str) or not 1 <= len(request_id) <= 200:
                raise LiveError('Jupiter requestId fehlt.')
            receipt = dict(input_units=str(units), expected_output_units=str(quote['outAmount']),
                           minimum_output_units=str(quote['otherAmountThreshold']), estimated_network_fee_lamports=fees,
                           input_decimals=decimals, router=quote['router'], price_impact_pct=str(impact))
            self.store.update(identity,'quoted',request_id=request_id,receipt=receipt)
            from solders.transaction import VersionedTransaction
            transaction = VersionedTransaction.from_bytes(base64.b64decode(quote['transaction'],validate=True))
            transaction.sanitize()
            if transaction.message.header.num_required_signatures != 1 or str(transaction.message.account_keys[0]) != wallet:
                raise LiveError('Unerwartete Signierer oder abweichender Gebührenzahler.')
            allowed_programs = {JUPITER_PROGRAM,'11111111111111111111111111111111',
                'ComputeBudget111111111111111111111111111111',
                'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',
                'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb',
                'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'}
            programs = [str(transaction.message.account_keys[i.program_id_index]) for i in transaction.message.instructions]
            if JUPITER_PROGRAM not in programs or not set(programs) <= allowed_programs:
                raise LiveError('Transaktion enthält keine unterstützte Jupiter-Route oder unbekannte Hauptprogramme.')
            signed = VersionedTransaction(transaction.message, [signer])
            signature = str(signed.signatures[0])
            expiry = integer(quote.get('lastValidBlockHeight'),'Gültigkeits-Blockhöhe')
            height = integer(self.rpc.call('getBlockHeight',[{'commitment':'confirmed'}]), 'Blockhöhe')
            if height > expiry:
                raise LiveError('Jupiter-Transaktion ist bereits abgelaufen.')
            self.store.update(identity,'submitting',signature,request_id)
            submitted = True
            result = self.jupiter.request('execute', body=dict(signedTransaction=base64.b64encode(bytes(signed)).decode(),
                                                               requestId=request_id,lastValidBlockHeight=str(expiry)))
            for key in ('totalInputAmount','totalOutputAmount','inputAmountResult','outputAmountResult'):
                if re.fullmatch(r'[0-9]{1,24}', str(result.get(key))):
                    receipt[key] = str(result[key])
            self.store.update(identity,'submitting',receipt=receipt)
            if result.get('signature') != signature or result.get('status') != 'Success' or result.get('code') != 0:
                return self.store.update(identity,'unknown',message='Jupiter bestätigt die Ausführung nicht eindeutig. Signatur abgleichen; nicht mit neuer ID erneut senden.')
            return self._reconcile(identity)
        except Exception as exc:
            # Never log provider payloads, private key material, or signed transaction bytes.
            message = str(exc) if isinstance(exc,(LiveError,HeliusError)) else 'Order konnte nicht vollständig verarbeitet werden. Konfiguration und Providerantwort prüfen.'
            return self.store.update(identity,'unknown' if submitted else 'rejected',message=message)

    def reconcile(self, identity):
        order = self.store.get(identity)
        with self.store.wallet_lock(order['wallet']):
            return self._reconcile(identity)

    def _reconcile(self, identity):
        order = self.store.get(identity)
        if not order['signature']:
            if order['state'] in ('preparing', 'quoted'):
                return self.store.update(identity, 'rejected', message='Verarbeitung vor Übermittlung abgebrochen; keine Signatur wurde übermittelt.')
            return order
        try:
            result = self.rpc.call('getSignatureStatuses', [[order['signature']],{'searchTransactionHistory':True}])
            status = result['value'][0]
            if status is None:
                return self.store.update(identity,'unknown',message='Signatur aktuell nicht gefunden. Das beweist keine endgültige Nichtausführung.')
            if status.get('err') is not None:
                return self.store.update(identity,'failed',message='On-chain als fehlgeschlagen bestätigt; Netzwerkgebühren können angefallen sein.')
            if status.get('confirmationStatus') in ('confirmed','finalized'):
                return self.store.update(identity,'confirmed',message='Transaktion on-chain bestätigt.')
            return self.store.update(identity,'submitted',message='Signatur vorhanden, aber noch nicht bestätigt.')
        except Exception:
            return self.store.update(identity,'unknown',message='On-chain-Status nicht prüfbar. Keine automatische Wiederholung.')
