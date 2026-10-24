"""Bound request bodies before JSON decoding, including chunked requests."""
from starlette.responses import JSONResponse


class RequestSizeLimit:
    def __init__(self, app, max_bytes=65536):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] not in ('POST', 'PUT', 'PATCH', 'DELETE'):
            await self.app(scope, receive, send)
            return
        for name, value in scope['headers']:
            if name.lower() == b'content-length':
                try:
                    if not value.isdigit():
                        raise ValueError
                    length = int(value)
                except ValueError:
                    await JSONResponse({'detail': '请求长度无效'}, status_code=400)(scope, receive, send)
                    return
                if length > self.max_bytes:
                    await JSONResponse({'detail': '请求过大'}, status_code=413)(scope, receive, send)
                    return
        body = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            if len(body) + len(chunk) > self.max_bytes:
                await JSONResponse({'detail': '请求过大'}, status_code=413)(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get('more_body', False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
            return await receive()

        await self.app(scope, replay, send)
