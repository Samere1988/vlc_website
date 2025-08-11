from starlette.middleware.sessions import SessionMiddleware

class PatchedSessionMiddleware(SessionMiddleware):
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" or scope["type"] == "websocket":
            await super().__call__(scope, receive, send)
        else:
            await self.app(scope, receive, send)

