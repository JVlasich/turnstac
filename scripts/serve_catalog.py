"""Static server for the STAC browser.

The prebuilt browser expects /browser and /catalog at the web root
, but the browser lives in this repo while the catalog lives in the data root.
Serve both from one port by routing /browser/* to the repo
and everything else to the data root.

Usage: python serve_catalog.py DATA_ROOT REPO_ROOT [PORT]
"""
import os
import sys
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

data_root = sys.argv[1]
repo_root = sys.argv[2]
port = int(sys.argv[3]) if len(sys.argv) > 3 else 8111


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def translate_path(self, path):
        clean = path.split("?", 1)[0].split("#", 1)[0]
        is_browser = clean == "/browser" or clean.startswith("/browser/")
        self.directory = repo_root if is_browser else data_root
        local = super().translate_path(path)
        # browser routes (history mode) have no file behind them: serve the app shell
        if is_browser and not os.path.exists(local):
            return os.path.join(repo_root, "browser", "index.html")
        return local


print(f"Serving {data_root} (browser from {repo_root}) at http://localhost:{port}/browser/")
ThreadingHTTPServer(("localhost", port), Handler).serve_forever()
