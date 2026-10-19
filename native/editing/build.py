"""Build the incorporated engines from the source shipped with Lumiere."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent

def build(target_dir=None):
    os.environ.setdefault('CARGO_BUILD_JOBS', '2')
    target=Path(target_dir).resolve() if target_dir else ROOT/'.build'
    output=ROOT/'bin'
    output.mkdir(parents=True,exist_ok=True)
    cargo=shutil.which('cargo')
    if not cargo:
        raise RuntimeError('Rust 1.95.0 and cargo are required to build the native engines')
    receipts=[]
    for engine,binary in [('motion','lumiere-compositor'),('edit','lumiere-editor')]:
        subprocess.run([cargo,'+1.95.0','build','--release','--locked','--target-dir',str(target),
                        '--manifest-path',str(ROOT/engine/'Cargo.toml'),'-p',binary],check=True,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        name=binary+('.exe' if os.name=='nt' else '')
        destination=output/name
        shutil.copy2(target/'release'/name,destination)
        receipts.append({'engine':engine,'binary':name,'sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),
                         'lock_sha256':hashlib.sha256((ROOT/engine/'Cargo.lock').read_bytes()).hexdigest()})
    (output/'build.json').write_text(json.dumps({'protocol':'lumiere-worker/1','engines':receipts},indent=2),encoding='utf-8')
    return receipts

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target-dir')
    print(json.dumps(build(parser.parse_args().target_dir),indent=2))
