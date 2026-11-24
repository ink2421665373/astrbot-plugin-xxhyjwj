"""Explicitly simulated local preview; never contacts AstrBot or Xiaoheihe."""
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diagnostics import DiagnosticStore

store = DiagnosticStore()
cache_entries = 4
store.append("detail", "12", "", dict(data=[dict(desc="振刀", value=0), dict(desc="击杀", value=3), dict(desc="总伤害", value=12345)]), elapsed_ms=128)
store.append("home", "12", "pre-01", dict(player_info={}, seasons=[dict(key="pre-01", value="全部", selected=True), dict(key="qianji", value="千机赛季", selected=False)], overview=[dict(desc="场均振刀", value="0.0"), dict(desc="场均伤害", value=11357), dict(desc="场均击杀", value=None), dict(desc="KD", value="1.5")]), elapsed_ms=236)
BRIDGE = """<script>
async function send(method,path='/_mock/diagnostics'){const r=await fetch(path,{method});return await r.json();}
window.AstrBotPluginView={ready:async()=>({isDark:false}),apiGet:()=>send('GET'),apiPost:(path)=>send('POST',path==='diagnostics/cache/clear'? '/_mock/cache/clear':'/_mock/diagnostics')};
</script>"""


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "pages/diagnostics"), **kwargs)

    def log_message(self, *args):
        pass

    def respond(self, content, mime):
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.path == "/_mock/diagnostics":
            return self.respond(json.dumps(dict(store.snapshot(), cache_entries=cache_entries), ensure_ascii=False).encode(), "application/json")
        if self.path in ("/", "/index.html"):
            content = (ROOT / "pages/diagnostics/index.html").read_text(encoding="utf-8")
            content = content.replace("</head>", BRIDGE + "</head>").replace("<main>", '<main><p class="help">本地界面预览 · 模拟数据，仅验证诊断界面交互</p>')
            return self.respond(content.encode(), "text/html; charset=utf-8")
        super().do_GET()

    def do_POST(self):
        global cache_entries
        if self.path == "/_mock/cache/clear":
            previous = cache_entries
            cache_entries = 0
            return self.respond(json.dumps(dict(cleared_count=previous, cache_entries=0)).encode(), "application/json")
        if self.path != "/_mock/diagnostics":
            self.send_error(404)
            return
        store.clear()
        self.respond(json.dumps(store.snapshot()).encode(), "application/json")


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    print(f"http://127.0.0.1:{server.server_port}", flush=True)
    server.serve_forever()
