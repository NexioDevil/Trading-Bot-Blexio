"""Verify the public container entry point without submitting transactions."""
from contextlib import redirect_stdout,redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import bot

class CLITests(unittest.TestCase):
    def test_default_command_is_status_without_loading_a_wallet(self):
        output=io.StringIO()
        with patch.dict(os.environ,{},clear=True),patch('bot.LiveTrader') as trader,redirect_stdout(output):
            code=bot.main([])
        data=json.loads(output.getvalue())
        self.assertEqual(code,0)
        self.assertFalse(data['live_enabled'])
        self.assertFalse(data['helius_key_configured'])
        self.assertFalse(data['wallet_configured'])
        trader.assert_not_called()

    def test_http_server_flag_is_not_available(self):
        with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit) as raised:
            bot.main(['--serve'])
        self.assertEqual(raised.exception.code,2)

    def test_order_exit_codes_match_confirmation_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'order.json';path.write_text('{}')
            for state,expected in [('confirmed',0),('failed',2),('rejected',2),('quoted',3),('unknown',3),('submitted',3)]:
                with self.subTest(state=state),patch('bot.LiveTrader') as trader,redirect_stdout(io.StringIO()):
                    trader.return_value.execute.return_value={'state':state}
                    self.assertEqual(bot.main(['--live-order',str(path)]),expected)
                    trader.return_value.execute.assert_called_once_with({})

    def test_helius_check_remains_read_only(self):
        with patch('bot.HeliusClient') as rpc,patch('bot.LiveTrader') as trader,redirect_stdout(io.StringIO()):
            rpc.return_value.check.return_value={'connected':True,'slot':1}
            self.assertEqual(bot.main(['--check-helius']),0)
            trader.assert_not_called()
