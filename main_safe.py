import os
from aiohttp import web
import time

_start_time = time.monotonic()

async def handle(request):
    uptime = int(time.monotonic() - _start_time)
    return web.json_response({
        "status": "safe-placeholder",
        "message": "This is a safe placeholder app created to allow deployment without running the original bot logic.",
        "uptime_seconds": uptime
    })

app = web.Application()
app.router.add_get('/', handle)
app.router.add_get('/health', handle)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    web.run_app(app, host='0.0.0.0', port=port)
