"""Regenerate exact public tool schemas with isolated stores and no workers/network."""
import json
import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from prosperos_hoard.api import create_app
    from prosperos_hoard.family_api import catalogue
    from prosperos_hoard.editing.agent_tools import tool_catalog
    from prosperos_hoard.hoard_link.agentkit import _with_reason
    from prosperos_hoard.studio_contract import TOOLS
    with tempfile.TemporaryDirectory(prefix='prospero-catalog-') as raw:
        app = create_app(Path(raw), port=64239, start_workers=False)
        try:
            generation = catalogue(app, ROOT / 'prosperos_hoard' / 'mcp_server.py')['tools']
            editing = tool_catalog()
            if isinstance(editing, dict):
                editing = editing['tools']
            data = {k: [_with_reason(t) for t in v] for k, v in
                    {'generation': generation, 'editing': editing, 'host': TOOLS}.items()}
            output = ROOT / 'prosperos_hoard' / 'studio_catalog.json'
            output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            os.environ['PROSPERO_CATALOG_BUILD'] = '1'
            from prosperos_hoard import mcp_server
            os.environ.pop('PROSPERO_CATALOG_BUILD')
            try:
                native_mcp = [t.model_dump(exclude_none=True) for t in asyncio.run(mcp_server.mcp.list_tools())]
                mcp_data = {**data,'generation':native_mcp}
                (ROOT/'prosperos_hoard'/'studio_mcp_catalog.json').write_text(json.dumps(mcp_data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            finally:
                mcp_server._client.close()
            print({key: len(value) for key, value in data.items()})
        finally:
            app.state.shutdown()


if __name__ == '__main__':
    main()
