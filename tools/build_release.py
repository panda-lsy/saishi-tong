"""Build an explicit public source tree; never copy a working directory wholesale."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil

ROOT=Path(__file__).resolve().parents[1]
EXACT=['README.md','LICENSE','NOTICE.md','.gitignore','.gitattributes','.env.example','requirements.txt','requirements.lock.txt',
       'docs/ARCHITECTURE.md','docs/VALIDATION.md','docs/ARTICLE.md','docs/RELEASE_NOTES.md',
       'docs/images/search-quality-replay-20261025.png',
       'examples/web.env.example','examples/worker.env.example',
       'skills/saishi-curate/SKILL.md','skills/saishi-sources/SKILL.md',
       'bin/saishi','bin/saishi-web','bin/saishi-searchpro','bin/saishi-fetch',
       'deploy/saishi-openclaw-worker.service','deploy/saishi-openclaw-worker.timer',
       'tools/build_release.py','tools/evaluate_search.py','.github/workflows/tests.yml']

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);args=ap.parse_args()
    target=args.output.resolve()
    if target.exists() and any(target.iterdir()):raise SystemExit('Output must be a new or empty directory')
    target.mkdir(parents=True,exist_ok=True)
    files=set(EXACT)
    for folder,extensions in [('saishi_tong',{'.py','.html','.js','.mjs','.css','.png','.ico'}),('tests',{'.py','.mjs','.json'})]:
        files.update(str(p.relative_to(ROOT)).replace('\\','/') for p in (ROOT/folder).rglob('*')
                     if p.is_file() and p.suffix in extensions and '__pycache__' not in p.parts and 'frontend-captures' not in p.parts)
    # Do not include developer-only visual automation or external binaries.
    files.discard('tests/frontend-visual.mjs')
    manifest={}
    for name in sorted(files):
        source=ROOT/name
        if not source.resolve().is_relative_to(ROOT) or source.is_symlink():raise SystemExit('Unsafe source path: '+name)
        if not source.is_file():raise SystemExit('Missing release input: '+name)
        data=source.read_bytes()
        if name=='tests/fixtures/online-search-20261025.json':
            rows=json.loads(data.decode('utf-8-sig'))
            for row in rows:
                row['page']['passage']=row['page'].get('passage','')[:180]
            data=(json.dumps(rows,ensure_ascii=False,indent=2)+'\n').encode()
        if source.suffix not in {'.png','.ico'}:
            text=data.decode('utf-8-sig')
            patterns=[r'gh[pousr]_[A-Za-z0-9]{30,}',r'github_pat_[A-Za-z0-9_]{30,}',r'sk-proj-[A-Za-z0-9_-]{25,}',r'(?m)^-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----']
            if any(re.search(pattern,text) for pattern in patterns):raise SystemExit('Credential pattern in '+name)
            for match in re.finditer(r'AKID[A-Za-z0-9]{20,60}',text):
                if not re.search(r'synthetic|fixture|preview|test|evaluation',match[0],re.I):raise SystemExit('Credential ID pattern in '+name)
            data=text.replace('\r\n','\n').replace('\r','\n').encode('utf-8')
        dest=target/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(data)
        if name.startswith('bin/'):dest.chmod(0o755)
        manifest[name]=hashlib.sha256(data).hexdigest()
    (target/'SOURCE_MANIFEST.json').write_bytes((json.dumps(manifest,indent=2)+'\n').encode('utf-8'))
    print(json.dumps({'files':len(files),'output':str(target),'production_data_included':False}))

if __name__=='__main__':main()
