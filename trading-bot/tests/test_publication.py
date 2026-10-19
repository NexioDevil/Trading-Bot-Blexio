"""Publication checks redact findings and distinguish runtime data from code."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT=next((p/'scripts/check_publication.py' for p in Path(__file__).resolve().parents if (p/'scripts/check_publication.py').exists()),None)

@unittest.skipUnless(SCRIPT,'Source checks run from the source checkout')
class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.git('init','--quiet')
        self.git('config','user.name','PublicProject')
        self.git('config','user.email','public@users.noreply.github.com')

    def git(self,*args):
        return subprocess.check_output(['git','-C',str(self.root),*args],text=True,stderr=subprocess.DEVNULL)

    def scan(self,history=False):
        self.git('add','-A')
        result=subprocess.run([sys.executable,str(SCRIPT),'--root',str(self.root),*(['--history'] if history else [])],capture_output=True,text=True)
        return result,json.loads(result.stdout)

    def test_source_and_empty_templates_pass(self):
        (self.root/'.env.example').write_text('HELIUS_API_KEY=\nJUPITER_API_KEY=\n')
        result,data=self.scan();self.assertEqual(result.returncode,0);self.assertTrue(data['passed'])

    def test_secret_findings_never_echo_values(self):
        secret='ghp_'+'a'*36
        (self.root/'config.py').write_text('TOKEN="'+secret+'"')
        result,data=self.scan();self.assertEqual(result.returncode,1)
        self.assertNotIn(secret,result.stdout+result.stderr)
        self.assertEqual(data['findings'][0]['kind'],'access-token')

    def test_wallet_json_and_runtime_files_are_rejected(self):
        (self.root/'keypair.json').write_text(json.dumps([7]*64))
        (self.root/'.env').write_text('LOCAL_SETTING=yes\n')
        _,data=self.scan();self.assertEqual({f['kind'] for f in data['findings']},{'runtime-file','wallet-keypair'})

    def test_personal_commit_email_is_flagged_without_echoing_it(self):
        (self.root/'README.md').write_text('Project\n');self.git('add','-A')
        self.git('-c','user.email=owner@example.invalid','commit','--quiet','-m','Initial')
        result,data=self.scan(history=True)
        self.assertEqual(result.returncode,1)
        self.assertNotIn('owner@example.invalid',result.stdout)
        self.assertEqual(data['findings'][0]['kind'],'personal-commit-email')
