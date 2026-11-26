"""核对 1.9.15 上线：版本日志、服务状态、图片分流与切页。"""
import time

import paramiko

HOST, USER, PWD = "101.43.55.223", "ubuntu", "789456asd."

REMOTE = r'''
for i in $(seq 1 15); do
  code=$(curl -s -m 4 -o /dev/null -w '%{http_code}' http://127.0.0.1:6185/ || true)
  if [ "$code" = "200" ]; then echo "WEB_UP_${i}0s"; break; fi
  sleep 10
done
journalctl -u astrbot --since '15:26' --no-pager | grep -E 'astrbot_plugin_naraka \(1\.9' | tail -2
systemctl is-active astrbot
cat > /tmp/z_pages.py <<'PY'
import io, json, sys
from io import BytesIO
sys.path.insert(0, "/home/ubuntu/AstrBot/data/plugins")
from astrbot_plugin_naraka import main
from astrbot_plugin_naraka.delivery import slice_poster
from PIL import Image, ImageDraw

cfg = json.loads(io.open("/home/ubuntu/AstrBot/data/config/astrbot_plugin_naraka_config.json", encoding="utf-8-sig"))
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
print("WIDTHS_OK", all(Image.open(BytesIO(b)).size[0] == 1080 for b in pages))
PY
cd /tmp && /home/ubuntu/AstrBot/.venv/bin/python3 /tmp/z_pages.py 2>&1 | grep -E 'MODE|SPLIT|PAGES|WIDTHS_OK'
rm -f /tmp/z_pages.py
'''


def main(attempts=3):
    for i in range(attempts):
        try:
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect(HOST, 22, USER, PWD, timeout=40, banner_timeout=60,
                      auth_timeout=60, allow_agent=False, look_for_keys=False)
            sftp = c.open_sftp()
            with sftp.open("/tmp/z_check1915.sh", "w") as fh:
                fh.write(REMOTE)
            sftp.close()
            _, out, _ = c.exec_command("bash /tmp/z_check1915.sh 2>&1 | tail -20", timeout=280)
            print(out.read().decode("utf-8", "replace"))
            c.exec_command("rm -f /tmp/z_check1915.sh", timeout=20)
            c.close()
            return
        except Exception as exc:  # noqa: BLE001
            print("retry", i, type(exc).__name__, exc)
            time.sleep(10)


main()
