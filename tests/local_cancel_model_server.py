"""Container-only controlled cancellation model; no external requests."""
from http.server import ThreadingHTTPServer
from cancel_model import CancelModelHandler, initialize

initialize(ThreadingHTTPServer(('0.0.0.0', 9090), CancelModelHandler)).serve_forever()
