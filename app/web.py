import base64
from flask import request,render_template,jsonify
from app.errors import DomainError
from app.config import TITLE,MODULES,SHORT
from app.workflow import run,load
from app import store

def payload():
    data=request.get_json(silent=True) or {}
    if not isinstance(data,dict):raise DomainError('PAYLOAD','请求必须是对象')
    if 'csv' in data and not isinstance(data['csv'],str):raise DomainError('CSV','CSV内容必须是文本')
    sample=data.get('sample',request.args.get('sample','dirty.csv'))
    content=data.get('csv')
    return (content.encode() if isinstance(content,str) else load(sample)),data.get('parameters',{})

def _with_checksum(state):
    from app.modules.m7_archive.export_out import checksum
    if not state.get('archive',{}).get('sha256'):
        state['archive']['sha256']=checksum(state)
    return state

CHAIN_KEYS=('policies','segments','calculations','business','adjudication','succession',
            'reconcile','archive','published','publishable','published_version','fingerprint')

def overlay_published(state):
    """Return exactly one chain for display: the published snapshot when it
    exists, otherwise the fresh preview.  Stage-specific fields (rows,
    quarantine, counts) stay from the requested stage so each screen keeps its
    own grain, while every chain/amount field comes from the same snapshot."""
    published=store.get_published(state['run_id'])
    if not published:return state
    merged=dict(state)
    for key in CHAIN_KEYS:
        if key in published:merged[key]=published[key]
    merged['manifest']=dict(state.get('manifest') or {},parameters=published.get('manifest',{}).get('parameters',{}),
                            published_version=published.get('published_version'))
    merged.update(review_version=state.get('review_version',1),decision=state.get('decision','待复核'),preview=False)
    return _with_checksum(merged)

def api(stage):
    # A bare GET reflects the latest stored batch (including its published
    # chain); an explicit csv/sample previews that source instead.  Either way
    # a run with a published snapshot is shown from that single chain.
    data=request.get_json(silent=True) or {}
    has_payload=isinstance(data,dict) and bool('csv' in data or 'sample' in data or 'parameters' in data)
    if not has_payload and 'sample' not in request.args and store.history():
        stored=store.get(store.history()[0]['id'])
        if stored.get('source_b64'):
            state=run(base64.b64decode(stored['source_b64']),stored.get('manifest',{}).get('parameters',{}),stage)
            state.update(review_version=stored.get('review_version',1),decision=stored.get('decision','待复核'))
            return jsonify(overlay_published(state))
    content,parameters=payload()
    return jsonify(overlay_published(run(content,parameters,stage)))

def page(stage,template):
    latest=store.history();state=store.get(latest[0]['id']) if latest else run(load('dirty.csv'),{})
    source=load(request.args['sample']) if 'sample' in request.args else base64.b64decode(state['source_b64']) if state.get('source_b64') else load('dirty.csv')
    data=run(source,state.get('manifest',{}).get('parameters',{}),stage)
    data=overlay_published(data);data['preview']=not bool(store.get_published(data['run_id']))
    return render_template(template,title=TITLE,short=SHORT,modules=MODULES,current=stage,data=data,run=state,history=latest,published=store.get_published(data['run_id']))
