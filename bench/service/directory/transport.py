import json

from starlette.responses import JSONResponse


class BodyLimits:
    """Bound buffering and JSON nesting before the framework parses a write body."""

    unscanned_paths = frozenset({'/cities/validate'})

    @staticmethod
    def _replay(body, receive):
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            return await receive()
        return replay

    def __init__(self, app, max_bytes=65536, max_depth=32):
        self.app = app
        self.max_bytes = max_bytes
        self.max_depth = max_depth

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] not in {'POST', 'PUT'}:
            return await self.app(scope, receive, send)
        headers = dict(scope['headers'])
        media = headers.get(b'content-type', b'').decode('latin1').lower().split(';')
        if media[0].strip() != 'application/json' or any(
            part.strip() not in {'charset=utf-8', 'charset="utf-8"'} for part in media[1:]
        ):
            return await JSONResponse({'error': 'unsupported_media_type'}, 415)(scope, receive, send)
        if headers.get(b'content-encoding', b'identity') != b'identity':
            return await JSONResponse({'error': 'unsupported_media_type'}, 415)(scope, receive, send)
        chunks = []
        size = 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            size += len(chunk)
            if size > self.max_bytes:
                return await JSONResponse({'error': 'body_too_large'}, 413)(scope, receive, send)
            chunks.append(chunk)
            if not message.get('more_body', False):
                break
        body = b''.join(chunks)
        try:
            body.decode('utf-8')
        except UnicodeDecodeError:
            return await JSONResponse({'error': 'invalid_json'}, 400)(scope, receive, send)
        if scope['path'] in self.unscanned_paths:
            # Ingest check (benchmark series 3): only size, media type and UTF-8 above; the
            # per-byte Python scan below would cost more than the decoding being compared.
            return await self.app(scope, self._replay(body, receive), send)
        # This common transport policy is identical for every future codec variant.
        # It avoids a complete generic value tree while rejecting duplicate object keys.
        stack = []
        quoted = escaped = False
        start = 0
        invalid_fields = False
        invalid = b'\x00' in body or body.startswith(b'\xef\xbb\xbf')
        for offset, char in enumerate(body):
            if quoted:
                if escaped:
                    escaped = False
                elif char == 92:
                    escaped = True
                elif char == 34:
                    quoted = False
                    token = body[start:offset + 1]
                    if b'\\u' in token:
                        try:
                            json.loads(token).encode('utf-8')
                        except UnicodeEncodeError:
                            invalid_fields = True
                        except (ValueError, UnicodeError):
                            invalid = True
                            break
                    following = offset + 1
                    while following < len(body) and body[following] in b' \t\r\n':
                        following += 1
                    if following < len(body) and body[following] == 58 and stack and stack[-1] is not None:
                        try:
                            key = json.loads(body[start:offset + 1])
                        except (ValueError, UnicodeError):
                            invalid = True
                            break
                        if key in stack[-1]:
                            invalid = True
                            break
                        stack[-1].add(key)
            elif char == 34:
                quoted = True
                start = offset
            elif char in (91, 123):
                stack.append(set() if char == 123 else None)
                if len(stack) > self.max_depth:
                    invalid = True
                    break
            elif char in (93, 125):
                if stack:
                    stack.pop()
            elif char in (78, 73):
                # NaN/Infinity are accepted by stdlib json but excluded by this contract.
                invalid = True
                break
        if invalid:
            return await JSONResponse({'error': 'invalid_json'}, 400)(scope, receive, send)
        if invalid_fields:
            # Preserve syntax-error priority on this exceptional Unicode path.
            try:
                json.loads(body)
            except (ValueError, UnicodeError):
                return await JSONResponse({'error': 'invalid_json'}, 400)(scope, receive, send)
            return await JSONResponse({'error': 'invalid_fields'}, 422)(scope, receive, send)
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            return await receive()

        await self.app(scope, bounded_receive, send)
