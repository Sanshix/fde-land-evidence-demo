"""Explicit test-space switching; test cookies never select the formal DB for reset."""
import asyncio
import json

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import demo_runs, store

COOKIE = 'fde_test_run'


def register(app, env):
    @app.middleware('http')
    async def test_workspace(request, call_next):
        identifier = request.cookies.get(COOKIE)
        if not identifier or request.url.path.startswith(('/api/demo/', '/static/')) or request.url.path in {'/', '/healthz'}:
            return await call_next(request)
        try:
            run = demo_runs.get(identifier)
            current = demo_runs.workspace(run)
        except ValueError as exc:
            response = JSONResponse({'detail': str(exc), 'test_space_unavailable': True}, status_code=409)
            response.delete_cookie(COOKIE)
            return response
        lock = demo_runs.space_lock(identifier)
        await asyncio.to_thread(lock.acquire)
        try:
            # Recheck after waiting for an in-flight reset; no stale-path late writes.
            current = demo_runs.workspace(demo_runs.get(identifier))
            if request.method == 'POST' and (request.url.path in {'/api/import', '/api/import-batch'} or request.url.path.startswith('/api/sample/') or request.url.path.endswith('/retry')):
                return JSONResponse({'detail': '测试运行清单已锁定；请通过测试按钮运行，重新测试须先归档。不会额外发起识别请求。'}, status_code=409)
            if request.method != 'GET' and request.url.path.endswith('/send'):
                return JSONResponse({'detail': '测试空间禁止发送真实通知'}, status_code=403)
            with store.use_workspace(current):
                return await call_next(request)
        except ValueError as exc:
            return JSONResponse({'detail': str(exc)}, status_code=409)
        finally:
            lock.release()

    def owner(run, request):
        who = env['actor'](request)
        identity = getattr(request.state, 'identity', None)
        role = identity['role'] if identity else request.headers.get('x-fde-role')
        if run['actor'] != who and role != 'admin':
            raise HTTPException(403, '只能归档自己创建的测试空间，管理员可协助')
        return who

    @app.get('/api/demo/status')
    def status(request: Request):
        try:
            runs = demo_runs.list_runs()
            selected = request.cookies.get(COOKIE)
            return {'runs': runs, 'selected': selected,
                    'datasets': [{'id': k, 'label': v['label']} for k, v in demo_runs.configs().items()],
                    'default_mode': 'replay', 'formal_data_unchanged': True}
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post('/api/demo/start')
    async def start(request: Request):
        who = env['actor'](request)
        body = await request.json()
        identity = getattr(request.state, 'identity', None)
        role = identity['role'] if identity else request.headers.get('x-fde-role')
        try:
            run, reused = await asyncio.to_thread(demo_runs.start, body.get('dataset'), body.get('mode'), who, role, env)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        response = JSONResponse({'run': demo_runs.public(run), 'reused': reused})
        response.set_cookie(COOKIE, run['id'], httponly=True, samesite='strict', max_age=86400 * 30)
        return response

    @app.post('/api/demo/formal')
    def formal(request: Request):
        env['actor'](request)
        response = JSONResponse({'space': 'formal'})
        response.delete_cookie(COOKIE)
        return response

    @app.post('/api/demo/{identifier}/activate')
    def activate(identifier: str, request: Request):
        env['actor'](request)
        try:
            demo_runs.workspace(demo_runs.get(identifier))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        response = JSONResponse({'id': identifier})
        response.set_cookie(COOKIE, identifier, httponly=True, samesite='strict', max_age=86400 * 30)
        return response

    @app.get('/api/demo/{identifier}/report')
    def report(identifier: str, format: str = 'json'):
        try:
            demo_runs.get(identifier)
            if format not in {'json', 'md'}:
                raise ValueError('报告格式不支持')
            path = demo_runs.folder(identifier) / ('report.' + format)
            if not path.is_file():
                raise ValueError('报告尚未生成，请查看实际处理进度')
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if format == 'json':
            return json.loads(path.read_text())
        return FileResponse(path, filename='contract-test-' + identifier + '.md', media_type='text/markdown')

    @app.get('/api/demo/{identifier}/reset-plan')
    def reset_plan(identifier: str):
        try:
            return demo_runs.reset_plan(identifier)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/demo/{identifier}/reset')
    async def reset(identifier: str, request: Request):
        try:
            who = owner(demo_runs.get(identifier), request)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        body = await request.json()
        if body.get('confirm') is not True:
            raise HTTPException(400, '必须确认具体测试空间和归档数量')
        try:
            result = await asyncio.to_thread(demo_runs.reset, identifier, body.get('token'), who)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        response = JSONResponse(result)
        if request.cookies.get(COOKIE) == identifier:
            response.delete_cookie(COOKIE)
        return response

    @app.post('/api/demo/{identifier}/restore')
    def restore(identifier: str, request: Request):
        try:
            who = owner(demo_runs.get(identifier), request)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        try:
            result = demo_runs.restore(identifier, who)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        response = JSONResponse(result)
        response.set_cookie(COOKIE, identifier, httponly=True, samesite='strict', max_age=86400 * 30)
        return response
