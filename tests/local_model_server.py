"""Container-only entry point for the controlled SSE integration model stub."""
from http.server import ThreadingHTTPServer
import threading
from knowledge_review_sse import ModelHandler

server = ThreadingHTTPServer(("0.0.0.0", 9090), ModelHandler)
server.requests, server.lock, server.release = [], threading.Lock(), threading.Event()
server.release.set()
server.serve_forever()
