"""Only the common Faustus routes; no native engine import or activation."""
from fastapi import FastAPI
from .. import __version__
from ..hoard_link.copilot import install_copilot


def create_app(root):
    app = FastAPI(title="Prospero's Hoard", version=__version__, docs_url=None, redoc_url=None)
    install_copilot(app, 'prospero', str(root), app_name="Prospero's Hoard", inject=False)
    return app
