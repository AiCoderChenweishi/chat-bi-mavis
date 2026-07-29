"""
ssh_run.py — pexpect 包装的 SSH 命令执行器
用 PROD_SSH_PASSWORD 密码登录
"""
import pexpect
import sys
import os

HOST = sys.argv[1]
CMD = " ".join(sys.argv[2:])

password = os.environ.get("PROD_SSH_PASSWORD")
if not password:
    print("❌ PROD_SSH_PASSWORD 未设", file=sys.stderr)
    sys.exit(1)

ssh_cmd = f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{HOST} {CMD!r}"
child = pexpect.spawn(ssh_cmd, timeout=120, encoding="utf-8")
i = child.expect(["password:", pexpect.EOF, pexpect.TIMEOUT])
if i != 0:
    print(f"❌ SSH 启动失败: {child.before[:200]}", file=sys.stderr)
    sys.exit(1)
child.sendline(password)
child.expect(pexpect.EOF)
print(child.before)
sys.exit(child.exitstatus or 0)
