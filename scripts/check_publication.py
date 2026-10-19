"""Check tracked release files and optionally commit emails without echoing secrets."""
import argparse
import json
import re
import subprocess
from pathlib import Path

PATTERNS={
    'private-key-block':re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'access-token':re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16})'),
    'literal-api-key':re.compile(r'''(?i)(?:api[-_]?key|SOLANA_PRIVATE_KEY|SMTP_PASSWORD)["']?\s*[:=]\s*["']([A-Za-z0-9_-]{20,})["']'''),
    'key-in-url':re.compile(r'(?i)[?&](?:api[-_]?key|access_token|secret)=([A-Za-z0-9_-]{16,})'),
    'wallet-keypair':re.compile(r'\[\s*(?:\d{1,3}\s*,\s*){63}\d{1,3}\s*\]'),
    'profile-reference':re.compile(r'https://fomo\.family/profile/[A-Za-z0-9_-]+'),
}
EMAIL=re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')
FIXTURES={'test-sentinel-not-a-real-credential'}


def git(root,*args):
    return subprocess.check_output(['git','-C',str(root),*args],text=True)


def public_email(email):
    return email.endswith(('@users.noreply.github.com','@noreply.github.com')) or email=='noreply@github.com'


def check(root,history=False):
    findings=[]
    files=git(root,'ls-files','-z').split('\0')
    for name in filter(None,files):
        path=root/name
        if not path.is_file():continue
        if name=='.env' or name.startswith(('secrets/','orders/','state/')) or name.endswith(('.sqlite3','.pem','.key')):
            findings.append({'file':name,'kind':'runtime-file'});continue
        source=path.read_text(encoding='utf-8',errors='replace')
        for kind,pattern in PATTERNS.items():
            for match in pattern.finditer(source):
                if kind=='literal-api-key' and match.group(1) in FIXTURES:continue
                findings.append({'file':name,'line':source.count('\n',0,match.start())+1,'kind':kind})
        for match in EMAIL.finditer(source):
            email=match.group()
            if public_email(email) or email.endswith(('.invalid','@example.com','@example.org')):continue
            findings.append({'file':name,'line':source.count('\n',0,match.start())+1,'kind':'personal-email'})
    template=root/'.env.example'
    if template.exists():
        for line in template.read_text().splitlines():
            if line.startswith(('HELIUS_API_KEY=','JUPITER_API_KEY=')) and line.partition('=')[2].strip():
                findings.append({'file':'.env.example','kind':'nonempty-key-template'})
    if history:
        for row in git(root,'log','--format=%H%x00%ae%x00%ce').splitlines():
            commit,author,committer=row.split('\0')
            if not all(public_email(email) for email in (author,committer)):
                findings.append({'commit':commit[:12],'kind':'personal-commit-email'})
    return findings


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--history',action='store_true')
    args=parser.parse_args()
    findings=check(args.root,args.history)
    print(json.dumps({'passed':not findings,'findings':findings}))
    return int(bool(findings))


if __name__=='__main__':raise SystemExit(main())
