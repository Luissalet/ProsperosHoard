"""Build the complete owned Windows distribution from existing build outputs."""
import hashlib
import json
from pathlib import Path
import struct
import sys
import uuid

from setuptools import setup
from setuptools.command.build import build
from setuptools.command.build_py import build_py
from setuptools.command.bdist_wheel import bdist_wheel

ROOT = Path(__file__).resolve().parent


class FreshBuild(build):
    def initialize_options(self):
        super().initialize_options()
        # A previous fixture/build cannot leak stale files into the distribution.
        self.build_base = 'build/wheel-' + uuid.uuid4().hex[:12]


class CompleteBuild(build_py):
    def run(self):
        inputs = [ROOT / 'frontend/dist/index.html', ROOT / 'prosperos_hoard/editing/static/index.html']
        for index in inputs:
            if not index.is_file() or index.stat().st_size < 128 or b'<html' not in index.read_bytes().lower():
                raise RuntimeError(f'Build the complete frontend before packaging: {index}')
        super().run()
        destination = Path(self.build_lib) / 'prosperos_hoard/resources'
        records = []

        def retain(source, relative):
            if source.is_symlink() or source.stat().st_file_attributes & 0x400:
                raise RuntimeError(f'Linked build input is not supported: {source}')
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            self.copy_file(str(source), str(target))
            records.append({'path': relative.as_posix(), 'bytes': source.stat().st_size,
                            'sha256': hashlib.sha256(source.read_bytes()).hexdigest()})

        for source in sorted((ROOT / 'frontend/dist').rglob('*')):
            if source.is_file():
                retain(source, Path('generation-ui') / source.relative_to(ROOT / 'frontend/dist'))
        retain(ROOT / 'app-icon.png', Path('app-icon.png'))
        for name in ('lumiere-compositor.exe', 'lumiere-editor.exe'):
            source = ROOT / 'native/editing/bin' / name
            with source.open('rb') as stream:
                if stream.read(2) != b'MZ':
                    raise RuntimeError('Expected an owned Windows executable: ' + name)
                stream.seek(0x3C)
                offset = struct.unpack('<I', stream.read(4))[0]
                stream.seek(offset)
                if stream.read(4) != b'PE\0\0' or struct.unpack('<H', stream.read(2))[0] != 0x8664:
                    raise RuntimeError('Expected Windows x64 executable: ' + name)
            retain(source, Path('editing-bin') / name)
        retain(ROOT / 'native/editing/bin/build.json', Path('editing-bin/build.json'))
        retain(ROOT / 'native/editing/PROVENANCE.md', Path('licenses/native/PROVENANCE.md'))
        for component in ('motion', 'edit', 'codecbase'):
            folder = ROOT / 'native/editing' / component
            for source in sorted(folder.rglob('*')):
                relative = source.relative_to(folder)
                if 'target' in relative.parts or '.build' in relative.parts:
                    continue
                if source.is_file() and (source.name.startswith(('LICENSE', 'OFL-'))):
                    retain(source, Path('licenses/native') / component / relative)
        retain(ROOT / 'LICENSE', Path('licenses/Prospero-MIT.txt'))
        (destination / 'manifest.json').write_text(json.dumps({'platform': 'win_amd64', 'files': records}, indent=2), encoding='utf-8')


class WindowsWheel(bdist_wheel):
    def finalize_options(self):
        super().finalize_options()
        if sys.platform != 'win32' or struct.calcsize('P') != 8:
            raise RuntimeError('The bundled engine distribution must be built on Windows x64.')
        self.root_is_pure = False

    def get_tag(self):
        return 'py3', 'none', 'win_amd64'


setup(cmdclass={'build': FreshBuild, 'build_py': CompleteBuild, 'bdist_wheel': WindowsWheel})
