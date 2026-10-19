"""Build both complete native clients and self-host the common family fonts."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prosperos_hoard import procutil


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    npm = 'npm.cmd' if sys.platform == 'win32' else 'npm'
    for folder in ('frontend', 'editing_frontend'):
        for command in ('ci', 'run'):
            args = [npm, command] + (['build'] if command == 'run' else [])
            result = procutil.run(args, cwd=ROOT / folder, timeout=600, text=True)
            print(result.stdout, flush=True)
            if result.stderr:
                print(result.stderr, file=sys.stderr, flush=True)
            result.check_returncode()
    fonts = ROOT / 'prosperos_hoard' / 'studio_ui' / 'fonts'
    fonts.mkdir(exist_ok=True)
    for name in ('cinzel', 'source-sans-3'):
        package = ROOT / 'frontend' / 'node_modules' / '@fontsource' / name
        for weight in (400, 700):
            filename = f'{name}-latin-{weight}-normal.woff2'
            (fonts / filename).write_bytes((package / 'files' / filename).read_bytes())
        (fonts / (name + '-LICENSE.txt')).write_bytes((package / 'LICENSE').read_bytes())


if __name__ == '__main__':
    main()
