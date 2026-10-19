"""All order tests use generated unfunded keypairs and mocked network transports."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from live_trading import LiveTrader, LiveError, OrderStore, JupiterClient, load_keypair, validate_intent, SOL, USDC, JUPITER_PROGRAM, token_units

HAS_SOLDERS=importlib.util.find_spec('solders') is not None

class FakeRPC:
    def __init__(self):self.balance=100_000_000;self.confirmation='confirmed';self.error=None;self.calls=[]
    def call(self,method,params):
        self.calls.append(method)
        if method=='getTokenSupply':return {'value':{'decimals':9 if params[0]==SOL else 6}}
        if method=='getBalance':return {'value':self.balance}
        if method=='getTokenAccountsByOwner':return {'value':[{'account':{'data':{'parsed':{'info':{'tokenAmount':{'amount':'10000000'}}}}}}]}
        if method=='getBlockHeight':return 100
        if method=='getSignatureStatuses':return {'value':[{'confirmationStatus':self.confirmation,'err':self.error}]}
        raise AssertionError('Unexpected RPC '+method)

class FakeJupiter:
    def __init__(self,signer):self.signer=signer;self.calls=[];self.change={};self.timeout=False
    def request(self,path,params=None,body=None):
        from solders.instruction import Instruction
        from solders.pubkey import Pubkey
        from solders.hash import Hash
        from solders.message import MessageV0
        from solders.transaction import VersionedTransaction
        self.calls.append(path)
        if path=='order':
            message=MessageV0.try_compile(self.signer.pubkey(),[Instruction(Pubkey.from_string(JUPITER_PROGRAM),b'test-only',[])],[],Hash.default())
            tx=VersionedTransaction(message,[self.signer])
            result=dict(router='metis',gasless=False,signatureFeePayer=str(self.signer.pubkey()),swapMode='ExactIn',inputMint=params['inputMint'],outputMint=params['outputMint'],
                        inAmount=params['amount'],outAmount='100000',otherAmountThreshold='99500',slippageBps=50,
                        priceImpact=0.1,signatureFeeLamports=5000,prioritizationFeeLamports=1000,rentFeeLamports=0,
                        requestId='test-request',transaction=base64.b64encode(bytes(tx)).decode(),lastValidBlockHeight='200')
            result.update(self.change);return result
        if self.timeout:raise LiveError('Simulated timeout')
        transaction=VersionedTransaction.from_bytes(base64.b64decode(body['signedTransaction']))
        transaction.verify_and_hash_message()
        return {'status':'Success','code':0,'signature':str(transaction.signatures[0])}

@unittest.skipUnless(HAS_SOLDERS,'Install requirements.txt for signing tests')
class LiveOrderTests(unittest.TestCase):
    def setUp(self):
        from solders.keypair import Keypair
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.signer=Keypair.from_seed(bytes([7]*32))
        self.rpc=FakeRPC();self.jupiter=FakeJupiter(self.signer)
        self.store=OrderStore(Path(self.tmp.name)/'orders.sqlite3')
        self.trader=LiveTrader(self.rpc,self.jupiter,self.store,self.signer)
        self.env=patch.dict(os.environ,{'LIVE_TRADING_ENABLED':'true','LIVE_SOL_RESERVE':'0.01',
            'LIVE_ALLOWED_MINTS':SOL+','+USDC,'LIVE_MAX_INPUT_AMOUNTS':json.dumps({SOL:'0.01',USDC:'1'}),
            'LIVE_MAX_SLIPPAGE_BPS':'100','LIVE_MAX_PRICE_IMPACT_PCT':'2','LIVE_MAX_NETWORK_FEE_LAMPORTS':'10000000'},clear=True)
        self.env.start();self.addCleanup(self.env.stop)
        self.intent=dict(client_order_id='test-order',input_mint=SOL,output_mint=USDC,amount='0.001',slippage_bps=50)

    def test_explicit_order_signs_submits_and_checks_chain(self):
        result=self.trader.execute(self.intent)
        self.assertEqual(result['state'],'confirmed');self.assertTrue(result['signature'])
        self.assertEqual(self.jupiter.calls,['order','execute'])
        self.assertIn('getSignatureStatuses',self.rpc.calls)

    def test_disabled_mode_never_requests_quote(self):
        os.environ['LIVE_TRADING_ENABLED']='false'
        with self.assertRaises(LiveError):self.trader.execute(self.intent)
        self.assertEqual(self.jupiter.calls,[])

    def test_duplicate_id_never_executes_twice_even_after_reopen(self):
        first=self.trader.execute(self.intent)
        other=LiveTrader(self.rpc,self.jupiter,OrderStore(self.store.path),self.signer)
        self.assertEqual(other.execute(self.intent)['signature'],first['signature'])
        self.assertEqual(self.jupiter.calls.count('execute'),1)
        with self.assertRaises(LiveError):other.execute({**self.intent,'amount':'0.002'})

    def test_ambiguous_submission_is_not_retried(self):
        self.jupiter.timeout=True
        result=self.trader.execute(self.intent);self.assertEqual(result['state'],'unknown')
        self.trader.execute(self.intent);self.assertEqual(self.jupiter.calls.count('execute'),1)
        self.assertEqual(self.trader.reconcile('test-order')['state'],'confirmed')

    def test_insufficient_balance_and_precision_never_submit(self):
        self.rpc.balance=100
        self.assertEqual(self.trader.execute(self.intent)['state'],'rejected')
        self.assertEqual(self.jupiter.calls,[])
        result=self.trader.execute({**self.intent,'client_order_id':'precision','amount':'0.0000000001'})
        self.assertEqual(result['state'],'rejected')

    def test_bad_quote_mismatched_fee_payer_and_expired_order_rejected(self):
        from solders.keypair import Keypair
        changes=[{'inAmount':'9999999'},{'outputMint':SOL},{'priceImpact':10},{'slippageBps':500},
                 {'prioritizationFeeLamports':999999999},{'lastValidBlockHeight':'1'},{'transaction':'invalid'},{'otherAmountThreshold':'1'},{'otherAmountThreshold':'100001'},
                 {'gasless':True},{'signatureFeePayer':str(Keypair.from_seed(bytes([9]*32)).pubkey())}]
        for i,change in enumerate(changes):
            self.jupiter.change=change
            self.assertEqual(self.trader.execute({**self.intent,'client_order_id':f'bad-{i}'})['state'],'rejected')
        self.jupiter.change={};self.jupiter.signer=Keypair.from_seed(bytes([8]*32))
        self.assertEqual(self.trader.execute({**self.intent,'client_order_id':'wrong-payer'})['state'],'rejected')
        self.assertNotIn('execute',self.jupiter.calls)

    def test_confirmed_success_requires_onchain_confirmation(self):
        self.rpc.confirmation='processed'
        self.assertEqual(self.trader.execute(self.intent)['state'],'submitted')
        self.rpc.error={'InstructionError':[0,'Custom']}
        self.assertEqual(self.trader.reconcile('test-order')['state'],'failed')

    def test_pending_order_blocks_other_ids_until_reconciled(self):
        self.jupiter.timeout=True
        self.assertEqual(self.trader.execute(self.intent)['state'],'unknown')
        with self.assertRaises(LiveError):
            self.trader.execute({**self.intent,'client_order_id':'other'})
        self.assertEqual(self.jupiter.calls.count('execute'),1)
        self.assertEqual(self.trader.reconcile('test-order')['state'],'confirmed')
        self.jupiter.timeout=False
        self.assertEqual(self.trader.execute({**self.intent,'client_order_id':'other'})['state'],'confirmed')

    def test_process_wallet_lock_blocks_execute_and_reconcile(self):
        other=OrderStore(self.store.path)
        with other.wallet_lock(str(self.signer.pubkey())):
            with self.assertRaises(LiveError):self.trader.execute(self.intent)
        self.assertEqual(self.jupiter.calls,[])
        self.trader.execute(self.intent)
        with other.wallet_lock(str(self.signer.pubkey())):
            with self.assertRaises(LiveError):self.trader.reconcile('test-order')

    def test_crashed_pre_submission_can_be_closed_without_resubmitting(self):
        wallet=str(self.signer.pubkey())
        self.store.reserve('interrupted','fingerprint',wallet,self.intent)
        self.store.update('interrupted','quoted')
        self.assertEqual(self.trader.reconcile('interrupted')['state'],'rejected')
        self.assertEqual(self.jupiter.calls,[])
        self.assertEqual(self.trader.execute(self.intent)['state'],'confirmed')

    def test_excess_decimal_precision_is_rejected_without_requesting_quote(self):
        self.assertEqual(self.trader.execute({**self.intent,'amount':'0.0010000000000000000000000000000000001'})['state'],'rejected')
        self.assertEqual(self.jupiter.calls,[])

    def test_token_to_sol_and_key_loading(self):
        self.assertEqual(self.trader.execute({**self.intent,'input_mint':USDC,'output_mint':SOL,'amount':'0.1'})['state'],'confirmed')
        file=Path(self.tmp.name)/'key.json';file.write_text(json.dumps(list(bytes(self.signer))))
        with patch.dict(os.environ,{'SOLANA_KEYPAIR_FILE':str(file)}):self.assertEqual(load_keypair().pubkey(),self.signer.pubkey())
        file.write_text('not a key')
        with patch.dict(os.environ,{'SOLANA_KEYPAIR_FILE':str(file)}):
            with self.assertRaises(LiveError) as error:load_keypair()
            self.assertNotIn('not a key',str(error.exception))

class LiveValidationTests(unittest.TestCase):
    def test_invalid_amounts_and_mints(self):
        intent=dict(client_order_id='test',input_mint=SOL,output_mint=USDC,amount='0.001')
        for field,value in [('amount',0.001),('amount','nan'),('amount','0.1'),('amount','-1'),('output_mint',SOL),('client_order_id','x\n')]:
            with self.assertRaises(ValueError):validate_intent({**intent,field:value})

    def test_unit_conversion_is_exact_at_u64_and_fractional_boundaries(self):
        self.assertEqual(token_units('0.0010000000000000000000000000000000000',9),1000000)
        self.assertEqual(token_units('18446744073.709551615',9),2**64-1)
        self.assertEqual(token_units('0',9,zero=True),0)
        for value in ['0.0000000001','18446744073.709551616','1e100000','1e-100000']:
            with self.subTest(value=value),self.assertRaises(LiveError):token_units(value,9)

    def test_jupiter_transport_sends_official_endpoint_headers_and_json(self):
        with patch.dict(os.environ,{'JUPITER_API_KEY':'test-only'},clear=True),patch('live_trading.build_opener') as opener:
            response=opener.return_value.open.return_value.__enter__.return_value
            response.read.return_value=b'{"requestId":"test-id"}'
            client=JupiterClient()
            self.assertEqual(client.request('order',params={'amount':'123'})['requestId'],'test-id')
            request=opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url,'https://api.jup.ag/swap/v2/order?amount=123')
            self.assertEqual(request.get_method(),'GET')
            self.assertEqual(request.get_header('X-api-key'),'test-only')
            payload={'requestId':'test-id','signedTransaction':'test-bytes','lastValidBlockHeight':'200'}
            client.request('execute',body=payload)
            request=opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url,'https://api.jup.ag/swap/v2/execute')
            self.assertEqual(request.get_method(),'POST');self.assertEqual(json.loads(request.data),payload)

    def test_jupiter_error_messages_do_not_echo_secret_or_payload(self):
        from urllib.error import HTTPError,URLError
        with patch.dict(os.environ,{'JUPITER_API_KEY':'do-not-show'}),patch('live_trading.build_opener') as opener:
            for failure in [URLError('do-not-show'),HTTPError('https://example',401,'do-not-show',{},None)]:
                opener.return_value.open.side_effect=failure
                with self.assertRaises(LiveError) as error:JupiterClient().request('order',params={})
                self.assertNotIn('do-not-show',str(error.exception))
