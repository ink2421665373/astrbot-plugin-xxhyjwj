"""Local UI verification server with an explicitly simulated AstrBot bridge."""
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mapping_config import collect_season_catalog, mapping_snapshot, validate_mappings

schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8-sig"))
settings = dict(hero_mappings=schema["hero_mappings"]["default"], season_mappings=[])
login_status = dict(data_mode="heybox", has_credentials=True, credential_file_state="saved", online_verified=False)
bridge = """<script>
const send=async(url,body)=>{const r=await fetch(url,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});const v=await r.json();if(!r.ok)throw new Error(v.message);return v;};
window.AstrBotPluginView={ready:async()=>({isDark:false}),apiGet:(path)=>send(path==='mappings/login-status'? '/_mock/login-status':'/_mock/mappings'),apiPost:(path,body)=>send(path==='mappings/seasons/sync'? '/_mock/sync':'/_mock/save',body)};
</script>"""


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "pages/mappings"), **kwargs)

    def log_message(self, *args):
        pass

    def json_response(self, payload, status=200):
        content = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.path == "/_mock/mappings":
            return self.json_response(dict(mapping_snapshot(settings), login_status=login_status))
        if self.path == "/_mock/login-status":
            return self.json_response(login_status)
        if self.path in ("/", "/index.html"):
            content = (ROOT / "pages/mappings/index.html").read_text(encoding="utf-8")
            content = content.replace("</head>", bridge + "</head>").replace("<main>", '<main><p class="help">本地界面预览 · 模拟 AstrBot bridge，仅验证编辑交互</p>')
            encoded = content.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(encoded)
            return
        super().do_GET()

    def do_POST(self):
        if self.path not in ("/_mock/save", "/_mock/sync"):
            return self.json_response({"message": "not found"}, 404)
        try:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if payload.get("revision") != mapping_snapshot(settings)["revision"]:
                return self.json_response({"message": "配置已变更，请重新载入"}, 409)
            if self.path == "/_mock/sync":
                rows = [dict(key="pre-01", value="全部")] + [dict(key=f"demo-season-{i}", value=f"示例赛季{i}（模拟）") for i in range(1, 36)]
                merged, stats = collect_season_catalog(settings["season_mappings"], rows)
                settings["season_mappings"] = merged
                return self.json_response(dict(mapping_snapshot(settings), **stats, synced=True))
            values = validate_mappings(payload)
            settings.update(values)
            return self.json_response(dict(mapping_snapshot(settings), saved=True))
        except (ValueError, KeyError) as exc:
            return self.json_response({"message": str(exc)}, 400)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    print(f"http://127.0.0.1:{server.server_port}", flush=True)
    server.serve_forever()
