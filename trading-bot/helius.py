"""Read-only Helius RPC for mainnet order validation and confirmation."""
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler


class HeliusError(ValueError):
    pass


def validate_wallet(value):
    alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    if not isinstance(value, str) or not 32 <= len(value) <= 44:
        raise HeliusError('Eine öffentliche Solana-Wallet-Adresse ist erforderlich.')
    number = 0
    for char in value:
        if char not in alphabet:
            raise HeliusError('Ungültige Solana-Adresse (Base58).')
        number = number * 58 + alphabet.index(char)
    length = (number.bit_length() + 7) // 8 + len(value) - len(value.lstrip('1'))
    if length != 32:
        raise HeliusError('Eine Solana-Adresse muss genau 32 Bytes enthalten.')
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a URL containing the API credential to another host.
        return None


class HeliusClient:
    allowed_methods = {'getSlot', 'getTokenSupply', 'getBalance', 'getTokenAccountsByOwner', 'getBlockHeight', 'getSignatureStatuses'}

    @property
    def configured(self):
        return bool(os.environ.get('HELIUS_API_KEY', '').strip())

    def call(self, method, params):
        if method not in self.allowed_methods:
            raise HeliusError('Nur lesende RPC-Methoden sind erlaubt.')
        key = os.environ.get('HELIUS_API_KEY', '').strip()
        if not key:
            raise HeliusError('HELIUS_API_KEY ist im Server nicht verfügbar. Secret zuweisen und Server neu starten.')
        request = Request('https://mainnet.helius-rpc.com/?' + urlencode({'api-key': key}),
                          data=json.dumps(dict(jsonrpc='2.0', id=1, method=method, params=params)).encode(),
                          headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with build_opener(NoRedirect()).open(request, timeout=10) as response:
                raw = response.read(4_000_001)
            if len(raw) > 4_000_000:
                raise HeliusError('Helius-Antwort überschreitet die Größenbegrenzung.')
            result = json.loads(raw)
            if (not isinstance(result, dict) or result.get('jsonrpc') != '2.0' or type(result.get('id')) is not int
                    or result['id'] != 1 or 'result' not in result or result.get('error')):
                raise HeliusError('Helius meldet einen RPC-Fehler. Zugang, Kontingent und Parameter prüfen.')
            return result['result']
        except HTTPError as exc:
            messages = {401: 'Helius lehnt den API-Schlüssel ab.', 403: 'Helius-Zugriff durch Anbieter oder Netzwerkregel gesperrt.',
                        429: 'Helius-Kontingent oder Anfragelimit erreicht.'}
            raise HeliusError(messages.get(exc.code, f'Helius antwortet mit HTTP {exc.code}.')) from None
        except (URLError, OSError, TimeoutError):
            raise HeliusError('Helius ist nicht erreichbar. Netzwerkfreigabe, TLS und Verbindung prüfen.') from None
        except (ValueError, UnicodeError):
            raise HeliusError('Helius hat keine gültige RPC-Antwort geliefert.') from None

    def check(self):
        started = time.perf_counter()
        slot = self.call('getSlot', [{'commitment': 'confirmed'}])
        if not isinstance(slot, int) or isinstance(slot, bool) or slot < 0:
            raise HeliusError('Unerwartete Helius-Antwort beim Verbindungstest.')
        return dict(connected=True, slot=slot, rpc_roundtrip_ms=round((time.perf_counter()-started)*1000, 1))
