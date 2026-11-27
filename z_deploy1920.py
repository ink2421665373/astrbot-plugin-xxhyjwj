"""1.9.20 部署：上传 → 编译校验 → 打印 schema 键数（重启单独执行）。"""
import paramiko

HOST, USER, PWD = "101.43.55.223", "ubuntu", "789456asd."
PLUGIN = "/home/ubuntu/AstrBot/data/plugins/astrbot_plugin_naraka"
FILES = [
    "main.py",
    "_conf_schema.json",
    "metadata.yaml",
    "README.md",
    "CHANGELOG.md",
    "plugin-market.json",
    "tests/test_mapping_refresh.py",
]


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, 22, USER, PWD, timeout=40, banner_timeout=60,
              auth_timeout=60, allow_agent=False, look_for_keys=False)
    sftp = c.open_sftp()
    for name in FILES:
        sftp.put(name, f"{PLUGIN}/{name}")
        print("PUT", name)
    sftp.close()
    cmd = (
        "cd /home/ubuntu/AstrBot/data/plugins && "
        "/home/ubuntu/AstrBot/.venv/bin/python3 -m py_compile astrbot_plugin_naraka/main.py && echo COMPILE_OK; "
        f"grep -n '^version:' {PLUGIN}/metadata.yaml; "
        "/home/ubuntu/AstrBot/.venv/bin/python3 -c \"import io,json;"
        f"d=json.load(io.open('{PLUGIN}/_conf_schema.json',encoding='utf-8-sig'));"
        "print('SCHEMA_KEYS', len(d), 'AUTO_REG', d.get('nickname_auto_register',{}).get('default'))\""
    )
    _, out, err = c.exec_command(cmd, timeout=90)
    print(out.read().decode("utf-8", "replace"))
    e = err.read().decode("utf-8", "replace").strip()
    if e:
        print("ERR", e[:400])
    c.close()


main()
