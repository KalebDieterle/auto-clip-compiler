import json
from pathlib import Path
import secrets
import threading
import webbrowser

from .store import Store, run_lock, identity, file_hash
from .selection import validate_override
from .media import command


def create_app(directory, token):
    from fastapi import FastAPI, Request, HTTPException
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    # Request must be available to FastAPI's runtime annotation resolution.
    globals()['Request']=Request
    directory=Path(directory).resolve()
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=['127.0.0.1'])
    proxy_lock=threading.Lock()

    @app.middleware('http')
    async def local_access(request, call_next):
        if request.url.path.startswith('/api/'):
            supplied=request.headers.get('x-review-token') or request.query_params.get('token','')
            if not secrets.compare_digest(supplied,token):
                return JSONResponse({'error':'Invalid review token'},status_code=403)
            origin=request.headers.get('origin')
            if origin and origin!=f'http://{request.headers.get("host")}':
                return JSONResponse({'error':'Cross-origin request blocked'},status_code=403)
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        return response

    @app.get('/')
    def index():
        return HTMLResponse((Path(__file__).parent/'static'/'index.html').read_text(encoding='utf-8'))

    @app.get('/api/items')
    def items():
        store=Store(directory)
        try:
            values=store.candidates(); sources=store.sources()
            return {'candidates':values,'sources':sources,'summary':store.get('summary'),
                    'alignments':[dict(r,data=json.loads(r['data'])) for r in store.db.execute('SELECT * FROM alignments')]}
        finally: store.close()

    @app.post('/api/override/{cid}')
    async def override(cid: str, request: Request):
        data=await request.json()
        with run_lock(directory):
            store=Store(directory)
            try:
                item=next((c for c in store.candidates() if c['id']==cid),None)
                if item is None:
                    row=store.db.execute('SELECT data FROM overrides WHERE id=?',(cid,)).fetchone()
                    item=json.loads(row[0]) if row else None
                if item is None: raise HTTPException(404,'Candidate not found')
                source=next(s for s in store.sources() if s['id']==item['source'])
                if source['hash']!=item['source_hash']: raise HTTPException(409,'Source content changed')
                try: validate_override(item,data,source['data']['duration'])
                except ValueError as exc: raise HTTPException(422,str(exc)) from exc
                store.override(item,data)
                return {'saved':True}
            finally: store.close()

    @app.get('/api/preview/{sid}')
    def preview(sid: str):
        store=Store(directory)
        try:
            source=next((s for s in store.sources() if s['id']==sid),None)
            if source is None: raise HTTPException(404,'Source not found')
            settings=store.get('settings')
        finally: store.close()
        with proxy_lock:
            proxies=directory/'previews'; proxies.mkdir(exist_ok=True)
            src=Path(source['path'])
            digest=file_hash(src)
            if source['hash'] and digest!=source['hash']: raise HTTPException(409,'Source changed; resume analysis')
            proxy=proxies/(identity([digest,source['data'].get('audio_stream'),'proxy-v1'])+'.mp4')
            if not proxy.exists():
                temp=proxy.with_suffix('.tmp.mp4')
                args=[settings['ffmpeg'],'-v','error','-y','-i',src,'-map','0:v:0']
                stream=source['data'].get('audio_stream')
                args+=['-map',f'0:{stream}'] if stream is not None else ['-an']
                command(args+['-vf','scale=960:540:force_original_aspect_ratio=decrease:force_divisible_by=2',
                              '-c:v','libx264','-preset','veryfast','-crf','25','-pix_fmt','yuv420p','-c:a','aac','-movflags','+faststart',temp])
                from .media import probe
                probe(temp,settings['ffprobe']); temp.replace(proxy)
        return FileResponse(proxy,media_type='video/mp4')

    @app.post('/api/render')
    def render():
        from .pipeline import render_saved
        try: return {'output':str(render_saved(directory,{'render':True}))}
        except Exception as exc: raise HTTPException(409,str(exc)) from exc

    return app


def serve(directory,port=8765):
    import uvicorn
    token=secrets.token_urlsafe(32)
    app=create_app(directory,token)
    url=f'http://127.0.0.1:{port}/#'+token
    print('Review URL (local access token): '+url,flush=True)
    webbrowser.open(url)
    uvicorn.run(app,host='127.0.0.1',port=port,access_log=False)
