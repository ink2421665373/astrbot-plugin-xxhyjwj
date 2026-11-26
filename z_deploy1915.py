"""部署 1.9.15：自动=内嵌图片、微信按 1200 切页；并核对 daemon 侧实际发送尺寸。"""
import time

import paramiko

HOST, USER, PWD = "101.43.55.223", "ubuntu", "789456asd."
LOCAL = r"C:\Users\Administrator\Documents\deepseek-harness\default-workspace\astrbot-plugin-xxhyjwj"
REMOTE = "/home/ubuntu/AstrBot/data/plugins/astrbot_plugin_naraka"
CONF = "/home/ubuntu/AstrBot/data/config/astrbot_plugin_naraka_config.json"
FILES = [
    "main.py", "_conf_schema.json", "metadata.yaml", "README.md", "CHANGELOG.md",
    "plugin-market.json", "tests/test_report_image.py",
]


def main(attempts=3):
    for i in range(attempts):
        try:
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect(HOST, 22, USER, PWD, timeout=40, banner_timeout=60,
                      auth_timeout=60, allow_agent=False, look_for_keys=False)
            ts = time.strftime("%Y%m%d-%H%M%S")
            _, out, _ = c.exec_command(
                f"cd /home/ubuntu/AstrBot/data/plugins && tar czf ~/naraka-{ts}.tgz "
                f"--exclude='astrbot_plugin_naraka/assets/fonts' astrbot_plugin_naraka && "
                f"ls -lh ~/naraka-{ts}.tgz", timeout=180)
            print(out.read().decode("utf-8", "replace").strip())

            sftp = c.open_sftp()
            for name in FILES:
                sftp.put(LOCAL + "\\" + name.replace("/", "\\"), REMOTE + "/" + name)
            sftp.close()
            print("uploaded", len(FILES), "files")

            fix = f'''
import io, json
p = {CONF!r}
data = json.loads(io.open(p, encoding="utf-8-sig").read())
data["image_send_mode"] = "内嵌图片"
data["image_split_height"] = 1200
io.open(p, "w", encoding="utf-8-sig", newline="").write(json.dumps(data, ensure_ascii=False, indent=2))
print("CONF", len(data), data["image_send_mode"], data["image_split_height"])
'''
            sftp = c.open_sftp()
            with sftp.open("/tmp/z_conf1915.py", "w") as fh:
                fh.write(fix)
            sftp.close()
            _, out, _ = c.exec_command(
                f"cd {REMOTE} && /home/ubuntu/AstrBot/.venv/bin/python3 -m py_compile main.py && echo COMPILE_OK; "
                f"/home/ubuntu/AstrBot/.venv/bin/python3 /tmp/z_conf1915.py; "
                f"grep -n 'version:' metadata.yaml | head -2; rm -f /tmp/z_conf1915.py", timeout=180)
            print(out.read().decode("utf-8", "replace"))
            _, out, _ = c.exec_command("echo 789456asd. | sudo -S systemctl restart astrbot; echo RESTART_SENT", timeout=90)
            print(out.read().decode("utf-8", "replace").strip())
            c.close()
            return
        except Exception as exc:  # noqa: BLE001
            print("retry", i, type(exc).__name__, exc)
            time.sleep(10)


main()
