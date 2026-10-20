"""Container-only synthetic work-governance model with loopback test controls."""
from http.server import ThreadingHTTPServer
from job_model import JobModelHandler, initialize

initialize(ThreadingHTTPServer(('0.0.0.0', 9090), JobModelHandler)).serve_forever()
