"""Prepare a larger canvas and exact repaint mask from an existing image asset."""
from __future__ import annotations
import hashlib
import math
import tempfile
from pathlib import Path
from PIL import Image
from . import engine

def prepare(store, asset_id, width, height, anchor_x=0.5, anchor_y=0.5):
    if any(isinstance(v,bool) or not isinstance(v,int) or not 64<=v<=4096 or v%8 for v in (width,height)):
        raise ValueError('Outpaint dimensions must be multiples of 8, from 64 to 4096')
    if any(not isinstance(v,(int,float)) or not math.isfinite(v) or not 0<=v<=1 for v in (anchor_x,anchor_y)):
        raise ValueError('Anchors must be finite fractions from 0 to 1')
    source=store.get_asset(asset_id)
    if source['kind']!='image':
        raise ValueError('Outpaint needs an image asset')
    source_path=(store.data_dir/source['file_path']).resolve()
    source_path.relative_to(store.data_dir.resolve())
    digest=hashlib.sha256(source_path.read_bytes()).hexdigest()
    with Image.open(source_path) as original:
        image=original.convert('RGB')
    w,h=image.size
    if width<w or height<h or (width==w and height==h):
        raise ValueError('Enlarge at least one dimension without shrinking the original')
    left,top=round((width-w)*anchor_x),round((height-h)*anchor_y)
    canvas=Image.new('RGB',(width,height),(128,128,128));canvas.paste(image,(left,top))
    mask=Image.new('L',(width,height),255);mask.paste(0,(left,top,left+w,top+h))
    recipe={'backend':'local','operation':'prepare_outpaint','input_asset_ids':[asset_id],'source_sha256':digest,'original_box':[left,top,left+w,top+h],
            'params':{**((source.get('recipe') or {}).get('params') or {}),'width':width,'height':height,'anchor_x':anchor_x,'anchor_y':anchor_y}}
    work=store.data_dir/'tmp';work.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work) as directory:
        a,b=Path(directory)/'outpaint-canvas.png',Path(directory)/'outpaint-mask.png'
        canvas.save(a);mask.save(b)
        expanded=engine.import_asset(store,source['project_id'],a,recipe=recipe,source='derived',tags=['outpaint-canvas'])
        matte=engine.import_asset(store,source['project_id'],b,recipe={**recipe,'operation':'outpaint_mask'},source='derived',tags=['outpaint-mask'])
    return {'canvas':expanded,'mask':matte,'source_asset_id':asset_id,'original_box':recipe['original_box'],'source_sha256':digest}
