#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ShellCrash 飞牛 NAS 部署器
------------------------------------------------------------
图形化一键部署 ShellCrash 局域网网关模式：
  · 扫描局域网，自动识别飞牛 fnOS 设备
  · SSH 连接后执行 体检 / 部署 / 验证 / 卸载
  · 实时日志

技术栈：Python 标准库 http.server + paramiko，单文件 exe。
"""

import base64
import json
import os
import queue
import re
import secrets
import shlex
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import paramiko
except Exception:  # pragma: no cover
    paramiko = None

try:
    from embedded_scripts import SCRIPTS
except Exception:  # 开发态直接读目录
    SCRIPTS = {}
    _here = os.path.dirname(os.path.abspath(__file__))
    _pkg = os.path.join(os.path.dirname(_here), "ShellCrash-NAS部署包")
    for _n in ("sc_deploy.sh", "sc_verify.sh", "sc_uninstall.sh"):
        _p = os.path.join(_pkg, _n)
        if os.path.exists(_p):
            with open(_p, "r", encoding="utf-8") as f:
                SCRIPTS[_n] = f.read()

APP_NAME = "ShellCrash 飞牛 NAS 部署器"
VERSION = "1.4"
TOKEN = secrets.token_urlsafe(18)

RUNS = {}
RUNS_LOCK = threading.Lock()
STATE = {"last_ping": time.time(), "httpd": None, "port": 0}


def any_run_active():
    with RUNS_LOCK:
        return any(not r.done for r in RUNS.values())


def shutdown_app(delay=0.4):
    def _do():
        time.sleep(delay)
        try:
            if STATE.get("httpd"):
                STATE["httpd"].shutdown()
        except Exception:
            pass
        os._exit(0)
    threading.Thread(target=_do, daemon=True).start()


def watchdog(idle=180):
    """页面关闭后（超过 idle 秒没心跳且无任务在跑）自动退出，避免留下后台进程。"""
    while True:
        time.sleep(15)
        if STATE["httpd"] is None:
            continue
        if any_run_active():
            STATE["last_ping"] = time.time()   # 有任务在跑就不算闲置
            continue
        if time.time() - STATE["last_ping"] > idle:
            shutdown_app(0)


# ============================================================
# ANSI 转义处理（远端脚本可能输出颜色码，浏览器会把 \x1b 显示成方块）
# ============================================================
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
ANSI_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# sudo 自己打到 stderr 的密码提示，密码是程序自动填的，不必显示
SUDO_PROMPT_RE = re.compile(r"^\[sudo\][^:]*:\s*$|^\[sudo\].*password.*:\s*$|^Password:\s*$")


def parse_ansi(text):
    """剥离 ANSI 转义序列，并用其中的颜色码推断日志级别。返回 (纯文本, 级别或 None)。"""
    level = None
    if "\x1b" in text:
        for codes in ANSI_SGR_RE.findall(text):
            for c in codes.split(";"):
                c = c.strip()
                if c in ("31", "91"):
                    level = "err"
                elif c in ("33", "93"):
                    level = "warn"
                elif c in ("36", "96", "34", "94"):
                    level = "head"
                elif c in ("32", "92"):
                    level = "ok"
        text = ANSI_RE.sub("", text)
    if CTRL_RE.search(text):
        text = CTRL_RE.sub("", text)
    return text, level


# ============================================================
# 工具函数
# ============================================================
def local_ipv4():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 53))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def guess_prefix():
    ip = local_ipv4()
    return ".".join(ip.split(".")[:3])


def local_subnets():
    """返回本机所有（含非默认路由网卡）的 /24 前缀，如 ['192.168.11', '192.168.1']"""
    prefixes = []
    try:
        if os.name == "nt":
            out = subprocess_quiet(["ipconfig"])
            mas = {}
            cur = None
            for line in out.splitlines():
                m = re.search(r"(\d+\.\d+\.\d+\.\d+)\s*$", line)
                if not m:
                    continue
                if "IPv4" in line or "IP Address" in line:
                    cur = m.group(1)
                elif "子网掩码" in line or "Subnet Mask" in line:
                    if cur:
                        mask = m.group(1)
                        # 只取 /24 及更小的常见情况
                        if mask == "255.255.255.0":
                            prefixes.append(".".join(cur.split(".")[:3]))
                        cur = None
        else:
            out = subprocess_quiet(["ip", "-4", "addr"])
            for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", out):
                ip, bits = m.group(1), int(m.group(2))
                if bits >= 24:
                    prefixes.append(".".join(ip.split(".")[:3]))
    except Exception:
        pass
    prefixes.append(guess_prefix())
    seen, out = set(), []
    for p in prefixes:
        if p and p not in seen and not p.startswith("127."):
            seen.add(p)
            out.append(p)
    return out


def subprocess_quiet(args):
    import subprocess
    try:
        r = subprocess.run(args, capture_output=True, timeout=8)
        raw = r.stdout or b""
        for enc in ("utf-8", "gbk", "mbcs"):
            try:
                return raw.decode(enc)
            except Exception:
                continue
        return raw.decode("utf-8", "ignore")
    except Exception:
        return ""


def http_title(ip, port=5666, timeout=0.7):
    try:
        req = urllib.request.Request("http://%s:%d/" % (ip, port), headers={"User-Agent": "sc-deployer"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            html = r.read(6000).decode("utf-8", "ignore")
        m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
        return (m.group(1).strip() if m else ""), html
    except Exception:
        return "", ""


def probe(ip):
    info = {"ip": ip, "ssh": False, "fnos": False, "title": "", "banner": ""}
    title, html = http_title(ip)
    if title:
        info["title"] = title
        low = (title + html[:2000]).lower()
        if "飞牛" in low or "fnos" in low or "trim" in low:
            info["fnos"] = True
    try:
        s = socket.create_connection((ip, 22), timeout=0.5)
        s.settimeout(1.2)
        data = b""
        while b"\n" not in data and len(data) < 256:
            chunk = s.recv(64)
            if not chunk:
                break
            data += chunk
        s.close()
        banner = data.split(b"\n")[0].decode("ascii", "ignore").strip()
        if banner.startswith("SSH-"):
            info["ssh"] = True
            info["banner"] = banner
    except Exception:
        pass
    return info


def scan_subnet(prefix, timeout_total=40):
    hosts = ["%s.%d" % (prefix, i) for i in range(1, 255)]
    out = []
    with ThreadPoolExecutor(max_workers=96) as ex:
        for r in ex.map(probe, hosts):
            if r["fnos"] or r["ssh"]:
                out.append(r)
    out.sort(key=lambda x: (not x["fnos"], int(x["ip"].split(".")[-1])))
    return out


# ============================================================
# 任务运行器
# ============================================================
class Run:
    def __init__(self):
        self.id = secrets.token_hex(6)
        self.lines = []
        self.done = False
        self.code = None
        self.lock = threading.Lock()
        self.chan = None
        self.stop_flag = False

    def add(self, text, level="out"):
        if not text:
            return
        clean, ansi_level = parse_ansi(str(text))
        if not clean.strip():
            return
        if SUDO_PROMPT_RE.match(clean.strip()):
            return
        if ansi_level:
            level = ansi_level
        with self.lock:
            self.lines.append({"t": clean, "l": level})

    def log(self, text, level="out"):
        for line in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            self.add(line, level)

    def snapshot(self, frm=0):
        with self.lock:
            return list(self.lines[frm:]), self.done, self.code


def level_of(line):
    if line.startswith("====") or line.startswith("[") and "]" in line and "失败" in line:
        return "head"
    low = line
    if "失败" in low or "错误" in low or "error" in low.lower() or "denied" in low.lower():
        return "err"
    if "警告" in low or "注意" in low or "提示" in low:
        return "warn"
    if low.startswith("== "):
        return "head"
    return "out"


def upload_script(client, name, content, run):
    data = content.encode("utf-8")
    b64 = base64.b64encode(data).decode("ascii")
    cmd = "printf %s '{}' | base64 -d > /tmp/{} && chmod +x /tmp/{}".format(b64, name, name)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=60)
    err = stderr.read().decode("utf-8", "replace").strip()
    if stdout.channel.recv_exit_status() != 0:
        raise RuntimeError("上传脚本失败: %s" % (err or "未知错误"))


def build_command(action, cfg):
    q = shlex.quote
    if action == "check":
        # 体检本身只读，但用 root 才能看到端口占用者的进程名，信息更完整
        return "sudo -S -E sh /tmp/sc_deploy.sh --check", None
    if action == "deploy":
        env = ""
        if cfg.get("lan_cidr"):
            env += "LAN_CIDR=%s " % q(cfg["lan_cidr"])
        if cfg.get("sub_url"):
            env += "SUB_URL=%s " % q(cfg["sub_url"])
        if cfg.get("area"):
            env += "AREA=%s " % q(str(cfg["area"]))
        if cfg.get("dns_mod"):
            env += "DNS_MOD=%s " % q(cfg["dns_mod"])
        return "env %ssudo -S -E sh /tmp/sc_deploy.sh" % env, None
    if action == "verify":
        arg = q(cfg["lan_cidr"]) if cfg.get("lan_cidr") else ""
        return "sudo -S -E sh /tmp/sc_verify.sh %s" % arg, None
    if action == "uninstall":
        return "sudo -S -E sh /tmp/sc_uninstall.sh --yes", None
    raise ValueError("未知操作: %s" % action)


def exec_stream(client, cmd, sudo_pwd, run):
    transport = client.get_transport()
    chan = transport.open_session()
    chan.set_combine_stderr(True)
    run.chan = chan
    chan.exec_command(cmd)
    if sudo_pwd:
        try:
            chan.sendall((sudo_pwd + "\n").encode())
        except Exception:
            pass
    buf = b""
    chan.settimeout(1.0)
    while True:
        if run.stop_flag:
            try:
                chan.close()
            except Exception:
                pass
            run.log("已请求中断", "warn")
            break
        try:
            data = chan.recv(4096)
            if not data:
                if chan.exit_status_ready():
                    break
                continue
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                s = line.decode("utf-8", "replace").rstrip()
                run.add(s, level_of(s))
        except socket.timeout:
            if chan.exit_status_ready():
                break
        except Exception as exc:
            run.log("读取输出异常: %s" % exc, "err")
            break
    if buf:
        s = buf.decode("utf-8", "replace").rstrip()
        run.add(s, level_of(s))
    try:
        code = chan.recv_exit_status()
    except Exception:
        code = -1
    return code


def do_run(run, action, cfg):
    client = None
    try:
        if paramiko is None:
            run.log("缺少 paramiko 组件，无法连接。", "err")
            run.done = True
            run.code = -1
            return
        run.log("连接 %s@%s:22 ..." % (cfg["user"], cfg["host"]))
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            cfg["host"], 22, cfg["user"], cfg["pwd"],
            timeout=12, allow_agent=False, look_for_keys=False,
        )
        run.log("已连接，远端：%s" % client.exec_command("uname -sr")[1].read().decode().strip(), "ok")
        if cfg.get("remember"):
            cfg_remember(cfg["pwd"], True)   # 密码已验证有效才保存

        if action in ("check", "deploy"):
            upload_script(client, "sc_deploy.sh", SCRIPTS["sc_deploy.sh"], run)
        if action == "verify":
            upload_script(client, "sc_verify.sh", SCRIPTS["sc_verify.sh"], run)
        if action == "uninstall":
            upload_script(client, "sc_uninstall.sh", SCRIPTS["sc_uninstall.sh"], run)
        run.log("脚本已就绪，开始执行（首次会等 sudo 验证）...")

        cmd, _ = build_command(action, cfg)
        code = exec_stream(client, cmd, cfg.get("sudo_pwd") or cfg["pwd"], run)
        run.log("执行结束，退出码 %s" % code, "ok" if code == 0 else "err")
        run.code = code
    except paramiko.AuthenticationException:
        run.log("登录被拒绝：用户名或密码不对。", "err")
        run.log("当前尝试的是 %s@%s" % (cfg["user"], cfg["host"]), "warn")
        run.log("请检查：① 密码大小写/输入法；② 用户名是否正确；③ 该账号能否 SSH 登录（飞牛「设置→SSH」里是否只允许了别的账号）。", "warn")
        run.log("小技巧：勾选「记住密码」后本次登录成功会加密保存，下次自动填好，避免重复输入出错。", "warn")
        run.code = -2
    except paramiko.BadHostKeyException:
        run.log("主机密钥不匹配：%s 的 SSH 指纹变了，可能重装过系统。" % cfg["host"], "err")
        run.log("确认是本机后，删除 C:\\Users\\<你>\\.ssh\\known_hosts 里对应行再试。", "warn")
        run.code = -2
    except (socket.timeout, TimeoutError):
        run.log("连接超时：12 秒内没连上 %s:22。" % cfg["host"], "err")
        run.log("请检查 NAS 是否开机、IP 是否正确、22 端口是否放通。", "warn")
        run.code = -2
    except Exception as exc:
        name = type(exc).__name__
        if "NoValidConnections" in name or "Connection refused" in str(exc):
            run.log("无法连接 %s:22 —— 端口没开或 SSH 服务没启动。" % cfg["host"], "err")
            run.log("到飞牛「设置 → SSH」确认已开启，并确认 IP 填对了。", "warn")
        elif "Unable to connect" in str(exc) or "timed out" in str(exc).lower():
            run.log("网络不通：%s 拒绝或没有响应。" % cfg["host"], "err")
            run.log("先用 ping 确认能通，再确认 22 端口。", "warn")
        else:
            run.log("异常：%s" % exc, "err")
            run.log(traceback.format_exc(limit=2), "err")
        run.code = -1
    finally:
        run.done = True
        if client:
            try:
                client.close()
            except Exception:
                pass


def start_run(action, cfg):
    run = Run()
    with RUNS_LOCK:
        RUNS[run.id] = run
    threading.Thread(target=do_run, args=(run, action, cfg), daemon=True).start()
    return run.id


# ============================================================
# 前端页面
# ============================================================
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>__APP_NAME__</title>
<style>
  :root{
    --bg:#f5f7fa; --card:#ffffff; --line:#e3e8ef; --txt:#1f2937; --sub:#6b7280;
    --brand:#2563eb; --brand-d:#1d4ed8; --ok:#059669; --warn:#b45309; --err:#dc2626;
    --code-bg:#0f172a;
  }
  *{box-sizing:border-box;}
  body{margin:0;background:var(--bg);color:var(--txt);
    font:14px/1.6 "Microsoft YaHei","PingFang SC",system-ui,sans-serif;}
  header{background:var(--card);border-bottom:1px solid var(--line);padding:14px 22px;
    display:flex;align-items:center;gap:14px;}
  header h1{font-size:16px;margin:0;font-weight:600;}
  header .ver{font-size:12px;color:var(--sub);}
  .wrap{display:grid;grid-template-columns:380px 1fr;gap:16px;padding:16px 22px 22px;max-width:1360px;}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:14px;}
  .card h2{font-size:13px;margin:0 0 10px;color:var(--sub);font-weight:600;letter-spacing:.4px;}
  label{display:block;font-size:12px;color:var(--sub);margin:8px 0 4px;}
  input,select{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:7px;
    font-size:13px;background:#fff;color:var(--txt);font-family:inherit;}
  input:focus,select:focus{outline:none;border-color:var(--brand);box-shadow:0 0 0 3px rgba(37,99,235,.12);}
  .row{display:flex;gap:8px;}
  .row>*{flex:1;}
  button{cursor:pointer;border:1px solid var(--line);background:#fff;color:var(--txt);
    padding:8px 12px;border-radius:7px;font-size:13px;font-family:inherit;transition:.15s;}
  button:hover{border-color:#c8d3e0;background:#f8fafc;}
  button.primary{background:var(--brand);border-color:var(--brand);color:#fff;}
  button.primary:hover{background:var(--brand-d);}
  button.danger{color:var(--err);border-color:#f3c9c9;}
  button.danger:hover{background:#fef2f2;}
  button:disabled{opacity:.5;cursor:not-allowed;}
  .hosts{margin-top:10px;max-height:188px;overflow:auto;border:1px solid var(--line);border-radius:8px;}
  .host{display:flex;align-items:center;gap:9px;padding:7px 10px;border-bottom:1px solid #f1f5f9;cursor:pointer;}
  .host:last-child{border-bottom:none;}
  .host:hover{background:#f8fafc;}
  .host.sel{background:#eff6ff;}
  .host .ip{font-family:Consolas,monospace;font-size:13px;}
  .tag{font-size:11px;padding:1px 6px;border-radius:20px;border:1px solid var(--line);color:var(--sub);}
  .tag.fnos{background:#ecfdf5;border-color:#a7f3d0;color:#047857;}
  .tag.ssh{background:#eff6ff;border-color:#bfdbfe;color:#1d4ed8;}
  .hint{font-size:12px;color:var(--sub);margin-top:6px;}
  .actions{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px;}
  #log{background:var(--code-bg);color:#d7e3f4;border-radius:10px;padding:12px 14px;
    font:12.5px/1.55 Consolas,"Cascadia Mono",monospace;height:calc(100vh - 240px);
    min-height:340px;overflow:auto;white-space:pre-wrap;word-break:break-all;}
  #log .head{color:#7dd3fc;font-weight:600;}
  #log .ok{color:#6ee7b7;}
  #log .warn{color:#fcd34d;}
  #log .err{color:#fca5a5;}
  .status{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--sub);margin-top:8px;}
  .dot{width:8px;height:8px;border-radius:50%;background:#cbd5e1;}
  .dot.on{background:var(--ok);box-shadow:0 0 0 3px rgba(5,150,105,.18);}
  .dot.busy{background:#f59e0b;box-shadow:0 0 0 3px rgba(245,158,11,.18);}
  .dot.err{background:var(--err);}
  .toast{position:fixed;right:20px;bottom:20px;background:#111827;color:#fff;padding:10px 16px;
    border-radius:8px;font-size:13px;opacity:0;transform:translateY(8px);transition:.2s;pointer-events:none;}
  .toast.show{opacity:1;transform:none;}
</style>
</head>
<body>
<header>
  <h1>__APP_NAME__</h1>
  <span class="ver">v__VERSION__ · 局域网网关模式</span>
  <button id="btnExit" title="退出程序（关闭后台进程）"
    style="margin-left:auto;background:#fff;border:1px solid var(--line);color:var(--sub);
    padding:5px 12px;border-radius:7px;font-size:13px;cursor:pointer;">退出程序</button>
</header>

<div class="wrap">
  <div>
    <div class="card">
      <h2>1. 选择飞牛 NAS</h2>
      <div class="row">
        <input id="prefix" placeholder="192.168.1" style="flex:2">
        <button id="btnScan" style="flex:1">扫描局域网</button>
      </div>
      <div class="hint" id="subnetHint">留空则扫描本机所有网段；NAS 不在同一网段时，改成它的前三位</div>
      <div class="hosts" id="hosts">
        <div class="hint" style="padding:12px">点「扫描局域网」自动发现，或直接在下方手填 IP</div>
      </div>
      <label>NAS 地址</label>
      <input id="host" placeholder="192.168.1.100">
    </div>

    <div class="card">
      <h2>2. 登录凭据</h2>
      <label>SSH 用户名</label>
      <input id="user" placeholder="登录飞牛的用户名">
      <label>SSH 密码</label>
      <input id="pwd" type="password" autocomplete="new-password" placeholder="登录密码（不会明文保存）">
      <label>sudo 密码（留空 = 同上）</label>
      <input id="sudo" type="password" autocomplete="new-password" placeholder="一般是同一个密码">
      <div class="hint" style="margin:6px 0 0;display:flex;align-items:center;gap:6px">
        <input type="checkbox" id="remember" style="width:auto;margin:0">
        <label for="remember" style="margin:0;font-weight:400;cursor:pointer">记住密码（本机 DPAPI 加密，仅当前 Windows 账号可解）</label>
      </div>
    </div>

    <div class="card">
      <h2>3. 部署参数</h2>
      <label>局域网网段（要走代理的网段）</label>
      <input id="cidr" placeholder="192.168.1.0/24">
      <label>订阅地址（可留空，装完在菜单里导入）</label>
      <input id="sub" placeholder="https://...">
      <div class="row">
        <div>
          <label>代理范围</label>
          <select id="area">
            <option value="1">仅局域网客户端</option>
            <option value="2">局域网 + NAS 本机</option>
          </select>
        </div>
        <div>
          <label>DNS 模式</label>
          <select id="dns">
            <option value="fake-ip">fake-ip（推荐）</option>
            <option value="mix">mix（兼容性更好）</option>
          </select>
        </div>
      </div>
    </div>
  </div>

  <div>
    <div class="card" style="margin-bottom:12px">
      <h2>4. 执行</h2>
      <div class="actions">
        <button id="btnCheck">① 环境体检</button>
        <button id="btnDeploy" class="primary">② 一键部署</button>
        <button id="btnVerify">③ 验证连通</button>
        <button id="btnUninstall" class="danger">卸载回滚</button>
        <button id="btnClear">清空日志</button>
      </div>
      <div class="status"><span class="dot" id="dot"></span><span id="statusTxt">就绪</span></div>
    </div>
    <div id="log"></div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
const TOKEN = "__TOKEN__";
let curRun = null, pollTimer = null, lastIdx = 0;
const logEl = document.getElementById('log');

function toast(msg){
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(()=>t.classList.remove('show'), 2200);
}
function setStatus(text, cls){
  document.getElementById('statusTxt').textContent = text;
  const d = document.getElementById('dot');
  d.className = 'dot' + (cls ? ' ' + cls : '');
}
function append(item){
  const sp = document.createElement('span');
  sp.className = item.l || 'out';
  sp.textContent = item.t + '\n';
  logEl.appendChild(sp);
  logEl.scrollTop = logEl.scrollHeight;
}
async function api(path, body){
  const r = await fetch(path, {
    method: body ? 'POST' : 'GET',
    headers: {'Content-Type':'application/json','X-Token':TOKEN},
    body: body ? JSON.stringify(body) : undefined
  });
  const j = await r.json();
  if (j.error) throw new Error(j.error);
  return j;
}
function cfg(){
  return {
    host: document.getElementById('host').value.trim(),
    user: document.getElementById('user').value.trim(),
    pwd: document.getElementById('pwd').value,
    sudo_pwd: document.getElementById('sudo').value,
    lan_cidr: document.getElementById('cidr').value.trim(),
    sub_url: document.getElementById('sub').value.trim(),
    area: document.getElementById('area').value,
    dns_mod: document.getElementById('dns').value,
    remember: document.getElementById('remember').checked
  };
}

// ---- 手填 IP 时自动同步网段 ----
document.getElementById('host').addEventListener('input', e => {
  const m = e.target.value.trim().match(/^(\d{1,3}\.\d{1,3}\.\d{1,3})\.\d{1,3}$/);
  if(!m) return;
  document.getElementById('prefix').value = m[1];
  const cidr = document.getElementById('cidr');
  if(!cidr.value.trim() || cidr.value.trim() !== m[1] + '.0/24'){
    cidr.value = m[1] + '.0/24';
  }
});

// ---- 扫描 ----
document.getElementById('btnScan').onclick = async () => {
  const pref = document.getElementById('prefix').value.trim();
  const box = document.getElementById('hosts');
  box.innerHTML = '<div class="hint" style="padding:12px">扫描中，约 10~20 秒…</div>';
  document.getElementById('btnScan').disabled = true;
  try{
    const j = await api('/api/scan', {prefix: pref});
    box.innerHTML = '';
    if(!j.hosts.length){
      box.innerHTML = '<div class="hint" style="padding:12px">没发现设备，检查是否同网段，或直接手填 IP</div>';
    }
    j.hosts.forEach(h => {
      const d = document.createElement('div');
      d.className = 'host';
      const tags = [];
      if(h.fnos) tags.push('<span class="tag fnos">飞牛 fnOS</span>');
      tags.push(h.ssh ? '<span class="tag ssh">SSH 可用</span>' : '<span class="tag">无 SSH</span>');
      d.innerHTML = '<span class="ip">' + h.ip + '</span>' + tags.join('');
      d.onclick = () => {
        document.querySelectorAll('.host').forEach(x=>x.classList.remove('sel'));
        d.classList.add('sel');
        document.getElementById('host').value = h.ip;
        const p = h.ip.split('.').slice(0,3).join('.');
        if(!document.getElementById('cidr').value.trim())
          document.getElementById('cidr').value = p + '.0/24';
        setStatus('已选择 ' + h.ip, '');
      };
      box.appendChild(d);
      if(h.fnos && !document.getElementById('host').value){
        d.onclick();
      }
    });
  }catch(e){ box.innerHTML = '<div class="hint" style="padding:12px">扫描失败：' + e.message + '</div>'; }
  document.getElementById('btnScan').disabled = false;
};

// ---- 执行 ----
function busy(on, text){
  ['btnCheck','btnDeploy','btnVerify','btnUninstall'].forEach(id=>{
    document.getElementById(id).disabled = on;
  });
  setStatus(text, on ? 'busy' : '');
}
async function start(action){
  const c = cfg();
  if(!c.host){ toast('先选或填 NAS 地址'); return; }
  if(!c.user){ toast('请填写 SSH 用户名'); return; }
  if(!c.pwd){ toast('请填写 SSH 密码（程序不保存明文，重启后要重新输入）'); document.getElementById('pwd').focus(); return; }
  if(action === 'deploy' && !c.lan_cidr){ toast('部署需要填局域网网段'); return; }
  if(action === 'uninstall' && !confirm('确定卸载 ShellCrash 并还原网络？')) return;
  logEl.innerHTML = '';
  lastIdx = 0;
  busy(true, '执行中…');
  try{
    const j = await api('/api/run', Object.assign({action: action}, c));
    curRun = j.id;
    poll();
  }catch(e){
    toast('启动失败：' + e.message);
    busy(false, '就绪');
  }
}
function poll(){
  clearTimeout(pollTimer);
  if(!curRun) return;
  api('/api/log?id=' + curRun + '&from=' + lastIdx).then(j=>{
    j.lines.forEach(append);
    lastIdx += j.lines.length;
    if(j.done){
      const ok = j.code === 0;
      let text = '执行完成';
      if(!ok){
        text = j.code === -2 ? '登录/连接失败，见日志' : ('执行结束，退出码 ' + j.code);
      }
      setStatus(text, ok ? 'on' : 'err');
      busy(false, ok ? '执行完成' : text);
      curRun = null;
      return;
    }
    pollTimer = setTimeout(poll, 400);
  }).catch(()=>{ pollTimer = setTimeout(poll, 800); });
}
document.getElementById('btnCheck').onclick = ()=>start('check');
document.getElementById('btnDeploy').onclick = ()=>start('deploy');
document.getElementById('btnVerify').onclick = ()=>start('verify');
document.getElementById('btnUninstall').onclick = ()=>start('uninstall');
document.getElementById('btnClear').onclick = ()=>{ logEl.innerHTML=''; lastIdx=0; };

// ---- 退出程序 / 心跳 ----
document.getElementById('btnExit').onclick = async () => {
  if(curRun){ toast('任务正在执行，等它跑完再退出'); return; }
  if(!confirm('退出程序？\n\n退出后这个页面会失效，需要重新双击 exe 才能再用。')) return;
  try{ await api('/api/exit', {}); }catch(e){}
  document.body.innerHTML =
    '<div style="display:flex;align-items:center;justify-content:center;height:100vh;' +
    'font:15px/1.7 &quot;Microsoft YaHei&quot;,sans-serif;color:#374151;text-align:center">' +
    '<div>程序已退出，可以关闭这个标签页了。<br>' +
    '<span style="color:#9ca3af;font-size:13px">下次使用请重新双击 ShellCrashDeployer.exe</span></div></div>';
};
setInterval(()=>{ fetch('/api/ping').catch(()=>{}); }, 10000);

// ---- 初始化 ----
(async ()=>{
  try{
    const j = await api('/api/state');
    document.getElementById('prefix').value = j.prefix;
    document.getElementById('cidr').value = j.prefix + '.0/24';
    if(j.subnets && j.subnets.length > 1){
      document.getElementById('subnetHint').textContent =
        '本机所在网段：' + j.subnets.join(' / ') + '，留空则全部扫描';
    }
    document.getElementById('host').value = j.cfg.host || '';
    document.getElementById('user').value = j.cfg.user || '';
    document.getElementById('sub').value = j.cfg.sub_url || '';
    if(j.cfg.lan_cidr) document.getElementById('cidr').value = j.cfg.lan_cidr;
    if(j.pwd){
      document.getElementById('pwd').value = j.pwd;
      document.getElementById('remember').checked = true;
    }
    if(j.cfg.host){
      const m = j.cfg.host.match(/^(\d{1,3}\.\d{1,3}\.\d{1,3})\.\d{1,3}$/);
      if(m) document.getElementById('prefix').value = m[1];
    }
    setStatus('就绪 · 本机 ' + j.local_ip, '');
  }catch(e){}
})();
</script>
</body>
</html>
"""


# ============================================================
# 配置持久化
# ============================================================
def cfg_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "ShellCrashDeployer")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "config.json")


def cfg_load():
    try:
        with open(cfg_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ---- 密码加密存储（Windows DPAPI，只有当前 Windows 用户能解密） ----
def _dpapi(data, encrypt):
    if os.name != "nt":
        raise OSError("非 Windows 环境")
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI 失败，错误码 %s" % ctypes.get_last_error())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def pwd_seal(pwd):
    try:
        return base64.b64encode(_dpapi(pwd.encode("utf-8"), True)).decode("ascii")
    except Exception:
        return ""


def pwd_open(token):
    if not token:
        return ""
    try:
        return _dpapi(base64.b64decode(token), False).decode("utf-8")
    except Exception:
        return ""


def cfg_save(data):
    try:
        old = cfg_load()
        old.update({k: v for k, v in data.items() if k in ("host", "user", "sub_url", "lan_cidr")})
        with open(cfg_path(), "w", encoding="utf-8") as f:
            json.dump(old, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def cfg_remember(pwd, on):
    """记住/忘记密码。只在登录成功后才保存，避免把打错的密码存下来。"""
    try:
        old = cfg_load()
        if on and pwd:
            sealed = pwd_seal(pwd)
            if sealed:
                old["pwd_enc"] = sealed
        else:
            old.pop("pwd_enc", None)
        with open(cfg_path(), "w", encoding="utf-8") as f:
            json.dump(old, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ============================================================
# HTTP 服务
# ============================================================
class Handler(BaseHTTPRequestHandler):
    server_version = "SCDeployer/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _auth(self):
        return self.headers.get("X-Token") == TOKEN

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return {}

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = (PAGE.replace("__APP_NAME__", APP_NAME)
                        .replace("__VERSION__", VERSION)
                        .replace("__TOKEN__", TOKEN))
            return self._send(200, html, "text/html; charset=utf-8")
        if self.path.startswith("/api/ping"):
            # 心跳：不需要 token，同时用于「重复双击就切到已有窗口」的探测
            STATE["last_ping"] = time.time()
            return self._send(200, {"app": APP_NAME, "version": VERSION,
                                    "port": STATE.get("port"),
                                    "busy": any_run_active()})
        if not self._auth():
            return self._send(403, {"error": "无权限"})
        if self.path.startswith("/api/log"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            rid = (q.get("id") or [""])[0]
            frm = int((q.get("from") or ["0"])[0] or 0)
            run = RUNS.get(rid)
            if not run:
                return self._send(404, {"error": "任务不存在"})
            lines, done, code = run.snapshot(frm)
            return self._send(200, {"lines": lines, "done": done, "code": code})
        if self.path.startswith("/api/state"):
            saved = cfg_load()
            saved.pop("pwd_enc", None)          # 密文不直接给前端
            return self._send(200, {
                "prefix": guess_prefix(),
                "local_ip": local_ipv4(),
                "subnets": local_subnets(),
                "cfg": saved,
                "pwd": pwd_open(cfg_load().get("pwd_enc", "")),
                "version": VERSION,
            })
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth():
            return self._send(403, {"error": "无权限"})
        body = self._body()
        if self.path.startswith("/api/scan"):
            prefix = (body.get("prefix") or "").strip()
            if prefix:
                if not re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}", prefix):
                    return self._send(400, {"error": "网段格式应为 192.168.1"})
                prefixes = [prefix]
            else:
                prefixes = local_subnets()[:3]
            try:
                seen, hosts = set(), []
                for p in prefixes:
                    for h in scan_subnet(p):
                        if h["ip"] not in seen:
                            seen.add(h["ip"])
                            hosts.append(h)
                return self._send(200, {"hosts": hosts})
            except Exception as exc:
                return self._send(500, {"error": str(exc)})
        if self.path.startswith("/api/run"):
            action = body.get("action")
            if action not in ("check", "deploy", "verify", "uninstall"):
                return self._send(400, {"error": "未知操作"})
            if not body.get("host") or not body.get("user") or not body.get("pwd"):
                return self._send(400, {"error": "缺少连接信息（用户名或密码为空）"})
            cfg_save(body)
            if not body.get("remember"):
                cfg_remember("", False)      # 取消勾选就立即清掉已存的密码
            rid = start_run(action, body)
            return self._send(200, {"id": rid})
        if self.path.startswith("/api/exit"):
            if any_run_active():
                return self._send(409, {"error": "还有任务在执行，等它跑完再退出"})
            self._send(200, {"ok": True})
            shutdown_app(0.6)
            return
        return self._send(404, {"error": "not found"})


def serve(port, open_browser=True):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    STATE["httpd"] = httpd
    STATE["port"] = port
    STATE["last_ping"] = time.time()
    url = "http://127.0.0.1:%d/" % port
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    threading.Thread(target=watchdog, daemon=True).start()
    print("%s v%s 已启动：%s" % (APP_NAME, VERSION, url))
    httpd.serve_forever()


def find_running(port=8799, tries=6):
    """探测已在运行的实例。返回 (端口, 是不是本版本)。
    旧版没有 /api/ping，会返回 403/404，那种情况视为「旧实例占用」。"""
    for p in range(port, port + tries):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % p, timeout=1.0) as r:
                j = json.loads(r.read().decode("utf-8"))
            if j.get("app") == APP_NAME:
                return p, True
        except urllib.error.HTTPError:
            return p, False          # 有服务在listen，但不是本版本
        except Exception:
            continue
    return None, None


# ============================================================
# 命令行模式（自测 / 无界面使用）
# ============================================================
def cli():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--cli", action="store_true")
    p.add_argument("--action", default="check", choices=["check", "deploy", "verify", "uninstall"])
    p.add_argument("--host")
    p.add_argument("--user")
    p.add_argument("--pwd")
    p.add_argument("--sudo-pwd", dest="sudo_pwd")
    p.add_argument("--cidr")
    p.add_argument("--sub")
    p.add_argument("--scan", help="扫描网段前缀，如 192.168.1")
    p.add_argument("--port", type=int, default=8799)
    p.add_argument("--serve", action="store_true", help="仅启动界面服务")
    p.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    a = p.parse_args()
    if a.serve:
        return serve(a.port, open_browser=not a.no_open)
    if a.scan:
        for h in scan_subnet(a.scan):
            print(h["ip"], "fnos" if h["fnos"] else "", h["title"], h["banner"])
        return
    if a.cli and a.host:
        run = Run()
        do_run(run, a.action, {
            "host": a.host, "user": a.user, "pwd": a.pwd,
            "sudo_pwd": a.sudo_pwd or a.pwd,
            "lan_cidr": a.cidr, "sub_url": a.sub, "area": "1", "dns_mod": "fake-ip",
        })
        lines, done, code = run.snapshot(0)
        for l in lines:
            print(l["t"])
        print("[exit]", code)
        return
    port = a.port
    for i in range(20):
        try:
            return serve(port + i)
        except OSError:
            continue
    raise SystemExit("端口都被占用")


def main():
    if len(sys.argv) > 1:
        return cli()
    port = 8799
    old, ours = find_running(port)
    if old and ours:
        # 已在运行：直接打开已有界面，不再起第二个进程
        webbrowser.open("http://127.0.0.1:%d/" % old)
        return
    if old and not ours:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                "检测到一个旧版程序还在后台运行（占用端口 %d，旧版没有「退出程序」按钮）。\n\n"
                "请按 Ctrl+Shift+Esc 打开任务管理器，在「详细信息」里找到\n"
                "ShellCrashDeployer.exe，全部结束掉，然后重新双击本程序。\n\n"
                "（也可以继续用旧版页面：http://127.0.0.1:%d/）" % (old, old),
                APP_NAME, 0x40)
        except Exception:
            pass
        return
    for i in range(20):
        try:
            return serve(port + i)
        except OSError:
            continue
    # 全部端口占用时弹窗提示（无控制台模式下）
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, "端口 8799-8818 均被占用，无法启动界面。", APP_NAME, 0x10)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        err = traceback.format_exc()
        try:
            path = os.path.join(os.environ.get("TEMP", "."), "ShellCrashDeployer-error.log")
            with open(path, "w", encoding="utf-8") as f:
                f.write(err)
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, "启动失败：\n\n" + err[-800:], APP_NAME, 0x10)
        except Exception:
            print(err)
