"""单独跑图片分流探针（带 stderr）。"""
import paramiko

HOST, USER, PWD = "101.43.55.223", "ubuntu", "789456asd."

PROBE = r'''
import io, json, sys
from io import BytesIO
sys.path.insert(0, "/home/ubuntu/AstrBot/data/plugins")
from astrbot_plugin_naraka import main
from PIL import Image, ImageDraw

cfg = json.loads(io.open("/home/ubuntu/AstrBot/data/config/astrbot_plugin_naraka_config.json", encoding="utf-8-sig").read())
p = object.__new__(main.NarakaPlugin)
p.config = cfg

class E:
    def __init__(self, n): self._n = n
    def get_platform_name(self): return self._n

wx, qq = E("wechat_agent"), E("aiocqhttp")
print("MODE", p._image_send_mode(wx), "|", p._image_send_mode(qq))
print("SPLIT", p._image_split_height(wx), "|", p._image_split_height(qq))

tall = Image.new("RGB", (1080, 3956), "#ffffff")
d = ImageDraw.Draw(tall)
for y in range(0, 3956, 200):
    d.line((60, y, 1020, y), fill="#ccd6e0", width=10)
buf = BytesIO(); tall.save(buf, format="PNG")
pages = p._image_pages(wx, [buf.getvalue()])
print("PAGES", len(pages), [Image.open(BytesIO(b)).size for b in pages])
'''


def main(attempts=3):
    for i in range(attempts):
        try:
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect(HOST, 22, USER, PWD, timeout=40, banner_timeout=60,
                      auth_timeout=60, allow_agent=False, look_for_keys=False)
            sftp = c.open_sftp()
            with sftp.open("/tmp/z_pages2.py", "w") as fh:
                fh.write(PROBE)
            sftp.close()
            _, out, _ = c.exec_command(
                "cd /tmp && /home/ubuntu/AstrBot/.venv/bin/python3 /tmp/z_pages2.py 2>&1 | tail -12; rm -f /tmp/z_pages2.py",
                timeout=200)
            print(out.read().decode("utf-8", "replace"))
            c.close()
            return
        except Exception as exc:  # noqa: BLE001
            print("retry", i, type(exc).__name__, exc)


main()
