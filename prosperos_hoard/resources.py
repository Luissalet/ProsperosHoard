"""Resolve owned assets in an installed distribution or a development tree."""
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent


def installed():
    return (PACKAGE / 'resources/manifest.json').is_file()


def resource(relative, development):
    packaged = PACKAGE / 'resources'
    if installed():
        return packaged / relative
    return PACKAGE.parent / development


def generation_ui():
    return resource('generation-ui', 'frontend/dist')


def app_icon():
    return resource('app-icon.png', 'app-icon.png')


def editing_engine(filename):
    return resource('editing-bin/' + filename, 'native/editing/bin/' + filename)
