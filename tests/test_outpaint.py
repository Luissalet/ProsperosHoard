from PIL import Image
from prosperos_hoard import engine
from prosperos_hoard.outpaint import prepare
import pytest

def test_expand_keeps_original_bytes_and_exact_repaint_region(store,project,tmp_path):
    path=tmp_path/'source.png';Image.new('RGB',(64,64),(30,80,120)).save(path)
    src=engine.import_asset(store,project['id'],path)
    original=(store.data_dir/src['file_path']).read_bytes()
    prepared=prepare(store,src['id'],128,96,1,0)
    assert prepared['original_box']==[64,0,128,64]
    with Image.open(store.data_dir/prepared['canvas']['file_path']) as canvas:
        assert canvas.crop((64,0,128,64)).tobytes()==Image.open(path).tobytes()
    with Image.open(store.data_dir/prepared['mask']['file_path']) as mask:
        assert mask.getpixel((65,1))==0 and mask.getpixel((1,1))==255 and mask.getpixel((127,95))==255
    assert (store.data_dir/src['file_path']).read_bytes()==original
    assert prepared['canvas']['recipe']['input_asset_ids']==[src['id']]
    with pytest.raises(ValueError,match='without shrinking'):
        prepare(store,src['id'],64,64)

def test_shared_http_and_agent_contract_offer_same_outpaint_operation(client):
    http,app,_=client
    tools=http.get('/api/agent/tools').json()['tools']
    tool=next(t for t in tools if t['name']=='studio_outpaint')
    assert {'asset_id','width','height','prompt'}<=set(tool['inputSchema']['required'])
    assert 'outpaint' in tool['description'].lower()
