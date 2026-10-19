"""Read-only RPC boundaries and sanitized failures."""
import os
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from helius import HeliusClient,HeliusError,NoRedirect,validate_wallet

class HeliusTests(unittest.TestCase):
    def test_wallet_validation(self):
        self.assertEqual(validate_wallet('11111111111111111111111111111111'),'11111111111111111111111111111111')
        for value in [None,'0'*32,'short','1'*44]:
            with self.subTest(value=value),self.assertRaises(HeliusError):validate_wallet(value)

    def test_missing_key_and_write_methods_never_open_connection(self):
        with patch.dict(os.environ,{},clear=True),patch('helius.build_opener') as opener:
            with self.assertRaises(HeliusError):HeliusClient().call('getSlot',[])
            with self.assertRaises(HeliusError):HeliusClient().call('sendTransaction',[])
            opener.assert_not_called()

    def test_http_failure_does_not_expose_provider_payload_or_key(self):
        marker='test-sentinel-not-a-real-credential'
        with patch.dict(os.environ,{'HELIUS_API_KEY':marker},clear=True),patch('helius.build_opener') as opener:
            opener.return_value.open.side_effect=HTTPError('https://example.invalid/'+marker,401,marker,{},None)
            with self.assertRaises(HeliusError) as error:HeliusClient().call('getSlot',[])
            self.assertNotIn(marker,str(error.exception))

    def test_redirects_never_forward_credentials(self):
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'',{},'https://example.invalid'))

    def test_rpc_request_envelope_and_response_are_checked(self):
        with patch.dict(os.environ,{'HELIUS_API_KEY':'test-only'},clear=True),patch('helius.build_opener') as opener:
            response=opener.return_value.open.return_value.__enter__.return_value
            response.read.return_value=json.dumps({'jsonrpc':'2.0','id':1,'result':7}).encode()
            self.assertEqual(HeliusClient().call('getSlot',[]),7)
            request=opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_method(),'POST')
            self.assertEqual(json.loads(request.data),{'jsonrpc':'2.0','id':1,'method':'getSlot','params':[]})
            for payload in [{'result':7},{'jsonrpc':'2.0','id':2,'result':7},{'jsonrpc':'2.0','id':True,'result':7}]:
                response.read.return_value=json.dumps(payload).encode()
                with self.assertRaises(HeliusError):HeliusClient().call('getSlot',[])
