#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建脚本：把 ../deploy/*.sh 内嵌进源码，再用 PyInstaller 打包单文件 exe。

用法：python build.py
产物：../dist/ShellCrashDeployer.exe
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PKG = os.path.join(ROOT, "deploy")
SCRIPTS = ["sc_deploy.sh", "sc_verify.sh", "sc_uninstall.sh"]


def gen_embedded():
    items = []
    for name in SCRIPTS:
        path = os.path.join(PKG, name)
        if not os.path.exists(path):
            raise SystemExit("找不到脚本：%s" % path)
        with open(path, "r", encoding="utf-8") as f:
            body = f.read()
        items.append("    %r: %r," % (name, body))
    out = (
        "# -*- coding: utf-8 -*-\n"
        "# 自动生成，请勿手改。源文件：deploy/*.sh\n"
        "SCRIPTS = {\n" + "\n".join(items) + "\n}\n"
    )
    dest = os.path.join(HERE, "embedded_scripts.py")
    with open(dest, "w", encoding="utf-8") as f:
        f.write(out)
    print("已生成 %s（%d 个脚本，共 %d 字节）" % (dest, len(items), len(out)))


def place(src, dst):
    """把新 exe 放到目标位置。目标正被运行时 Windows 不允许覆盖/删除，
    但允许改名——先把旧文件改名成 .old 再写入，旧进程继续从 .old 运行。"""
    if not os.path.exists(dst):
        os.rename(src, dst)
        return
    try:
        os.replace(src, dst)
        return
    except OSError:
        pass
    old = dst + ".old"
    for i in range(2, 20):
        if not os.path.exists(old):
            break
        try:
            os.remove(old)
            break
        except OSError:
            old = "%s.%d.old" % (dst, i)
    os.rename(dst, old)
    os.rename(src, dst)
    print("提示：旧版本正在运行，已改名为 %s，可稍后手动删除" % os.path.basename(old))


def build():
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--noconsole",
        "--name", "ShellCrashDeployer",
        "--distpath", os.path.join(HERE, "dist"),
        "--workpath", os.path.join(HERE, "build"),
        "--specpath", os.path.join(HERE, "build"),
        "--hidden-import", "paramiko",
        "--collect-submodules", "paramiko",
        os.path.join(HERE, "gui_app.py"),
    ]
    print("执行：", " ".join(cmd))
    subprocess.check_call(cmd, cwd=HERE)
    src = os.path.join(HERE, "dist", "ShellCrashDeployer.exe")
    dst = os.path.join(ROOT, "dist", "ShellCrashDeployer.exe")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(src):
        place(src, dst)
        print("完成：%s（%.1f MB）" % (dst, os.path.getsize(dst) / 1048576))
    else:
        raise SystemExit("未找到产物：%s" % src)


if __name__ == "__main__":
    gen_embedded()
    build()
