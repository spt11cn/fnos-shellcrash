# fnos-shellcrash · 飞牛 NAS 一键变旁路网关

把一台飞牛 fnOS（或任意 Debian 系）NAS 变成**局域网透明代理网关**：装好 ShellCrash 后，
局域网里的电脑、手机、电视盒子**不装任何客户端**，只要把「网关」和「DNS」指向 NAS，就能走出去。

提供两种用法：

- **图形版（推荐）**：Windows 双击 `dist/ShellCrashDeployer.exe`，填 NAS 地址和密码，点三个按钮。
- **脚本版（SSH 熟手）**：`deploy/` 下三个 `.sh`，`--check` 先体检，再一键部署。

另有配套的 **规则管理器** `dist/ShellCrashRuleManager.exe`：图形化管理自定义策略组和自定义规则
（增删改、自动备份、一键重启内核），详见 [`rule-manager/使用说明.md`](rule-manager/使用说明.md)。两个 exe 互不冲突、都只在关闭网页后自动退出。

> 全流程在飞牛 fnOS（Intel J1900 / Debian 12 / 网卡 `enp1s0-ovs`）上实测通过，包括开机自启、分流、
> 非 443 端口、命名空间端到端验证。下面列出的「坑」都是真踩过的。

---

## 它做了什么

```
客户机（网关 / DNS 指向 NAS）
        │
        ▼
   ┌──────────┐  nft 把 LAN 的 53 转到 1053，TCP/UDP 流量 TPROXY 到 7893
   │   NAS    │────────────────────────────────────────────────┐
   │ShellCrash│                                                ▼
   │ + mihomo │                                        订阅节点 / 直连
   └──────────┘
```

- 自动识别系统、CPU 是否支持 AVX2（J1900 / N5105 这类没有，必须用 `amd64-compatible` 内核）
- 自动处理名字带 `ovs` 的网卡（ShellCrash 自动探测网段时会跳过它）
- 写死关键配置：`redir_mod=Tproxy`、`dns_mod=fake-ip`、`common_ports=OFF`、`userguide=1`
- 给普通用户补一个会自己 sudo 的 `crash` 命令，并把 `/etc/profile` 里会被覆盖的别名一起改掉
- 放行「客户机 → 其它内网网段」的转发（飞牛的 Docker 会把 iptables `FORWARD` 设成 DROP），
  并对跨网段流量做 SNAT（否则服务器回包找不到路，SMB 共享文件夹打不开），注册开机服务重建规则
- 注册开机自启

## 快速开始 · 图形版

1. 取 `ShellCrashDeployer.exe`（单文件、免安装，Windows 10/11）：直接下载仓库里的
   [`dist/ShellCrashDeployer.exe`](dist/ShellCrashDeployer.exe)，或到
   [Releases](https://github.com/spt11cn/fnos-shellcrash/releases) 页面取最新版（当前 **v1.6**）。

   校验（可选）：`certutil -hashfile ShellCrashDeployer.exe SHA256` 应为
   `189ae7f1cdfa36d5ea50404e340e6095a15c95b74a9676d96f85426a26372d5f`（14,919,883 字节）。
   配套的 `ShellCrashRuleManager.exe` SHA256：
   `7008f8a868b7ed9c3dd5625184cb92aef234ffb172eeead1a3138789ea85ed9f`（14,909,334 字节）。
2. 双击运行，浏览器自动打开 `http://127.0.0.1:8799`。
3. 依次点：**扫描局域网 → 选中带 `飞牛 fnOS` 标签的设备 → 填用户名密码 → ① 环境体检 → ② 一键部署 → ③ 验证连通**。

细节见 [`gui/使用说明.md`](gui/使用说明.md)。

程序只监听 `127.0.0.1`；密码默认不落盘，勾选「记住密码」后才会用 Windows DPAPI 加密存到
`%APPDATA%\ShellCrashDeployer\config.json`，换电脑或换 Windows 账号都解不开。

## 快速开始 · 脚本版

```bash
# 1) 只体检，不改任何东西（新机器务必先跑这一步）
scp deploy/sc_deploy.sh deploy/sc_verify.sh deploy/sc_uninstall.sh 用户@NAS_IP:/tmp/
ssh 用户@NAS_IP 'sudo sh /tmp/sc_deploy.sh --check'

# 2) 正式部署（网段必填，订阅可留空，装完在菜单里导入）
ssh 用户@NAS_IP "sudo LAN_CIDR=192.168.1.0/24 SUB_URL='https://你的订阅' sh /tmp/sc_deploy.sh"

# 3) 验证（在 NAS 上建临时 netns 模拟一台客户机，测完自动清理）
ssh 用户@NAS_IP 'sudo sh /tmp/sc_verify.sh'
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `LAN_CIDR` | 自动探测默认路由网段 | 要走代理的局域网网段，如 `192.168.1.0/24` |
| `SUB_URL` | 空 | Clash 格式订阅地址 |
| `SUB_NAME` | `main` | 订阅在 `providers/` 里的名字 |
| `DNS_MOD` | `fake-ip` | 可选 `mix`（兼容性更好） |
| `AREA` | `1` | `1`=只代理局域网客户端；`2`=局域网 + NAS 本机 |
| `CORE_VER` | 最新 | 钉死 mihomo 版本，如 `v1.19.34` |

完整手册（手动分步、客户端设置、端口表、故障速查）见 [`deploy/README.md`](deploy/README.md)。

## 客户端怎么设置

| 设备 | 操作 |
|---|---|
| 路由器（最省事） | LAN/DHCP 里把「网关」和「DNS」都下发成 NAS 的 IP，全家设备免设置 |
| Windows | 网络适配器 → IPv4 属性 → 网关和 DNS 填 NAS IP |
| macOS | 系统设置 → 网络 → 详细信息 → TCP/IP（网关）+ DNS |
| iPhone / iPad | Wi-Fi → 当前网络 → 配置 IP → 手动 → 路由器填 NAS IP |
| Android | Wi-Fi → 修改网络 → 高级 → 网关 + DNS1 |

> DNS 不填也会被 nft 劫持到 NAS 的 1053，但填上更稳。

## 踩过的坑（这也是这个仓库存在的理由）

| 坑 | 现象 | 处理 |
|---|---|---|
| CPU 无 AVX2 | `can only be run on AMD64 processors with v3 microarchitecture` | 换 `amd64-compatible` 内核，脚本自动判断 |
| 网卡名含 `ovs` | 防火墙规则网段错误、客户机完全不通 | 用 `cust_host_ipv4` 手动指定网段 |
| 普通用户跑 `crash` | `touch: cannot touch '/tmp/ShellCrash/crash_start_time': Permission denied`，跟着一堆 polkit 鉴权弹窗 | ShellCrash 必须 root 运行；装一个自动 sudo 的 `/usr/bin/crash` 包装 |
| `/etc/profile` 别名优先于命令 | 明明有 `/usr/bin/crash`，还是以普通用户身份跑了菜单 | 别名改成 `alias crash="sudo -E bash /etc/ShellCrash/menu.sh"` |
| 新手引导重置配置 | 进菜单选了「推荐配置」，`Tproxy` 被改回 `Mix`、`fake-ip` 变 `mix`，代理直接失效 | 配置写 `userguide=1` 跳过引导 |
| 只有网页能走代理 | 非 80/443 端口的应用连不上 | `common_ports=OFF` |
| 端口被抢 | 飞牛应用中心的 `clash-for-fnos` 占着 `53`、`7890` | 先在应用中心停掉并关掉自启 |
| 验证偶发 `000` | 固定 IP 反复测试，被 conntrack 残留连接拖累 | 验证脚本每次随机 IP 新建 netns，带 3 次重试 |
| **Docker 的 `FORWARD DROP`** | 客户机能上外网，但访问**另一个网段**的服务器（如 `192.168.0.6`）全部超时，`ping` 100% 丢包 | 飞牛装了 Docker 会把 iptables `FORWARD` 策略设成 DROP，NAS 不再转发任何「跨网段」流量。部署脚本自动插 `sc-lan-forward` 规则放行到 `192.168/16`、`10/8`、`172.16/12`、`100.64/10`，并注册开机服务重建 |
| **跨网段缺 SNAT** | `ping` 通，但**共享文件夹打不开**、445 连不上、SSH 卡在握手 | 客户机源地址是 `192.168.x`，服务器回包要经另一台路由器绕回，而它通常没有这条路由。脚本加 `nat POSTROUTING MASQUERADE`，把源改写成本机地址，回程才对称 |

## 目录结构

```
deploy/            三个可独立使用的 .sh 脚本 + 完整部署手册
gui/               部署器源码（Python 标准库 + paramiko，打包成单文件 exe）
  gui_app.py       本地 HTTP 服务 + 页面 + SSH 执行 + ANSI 日志解析
  build.py         把 deploy/*.sh 内嵌进源码后打包 exe
rule-manager/      规则管理器源码（自定义策略组 / 自定义规则的图形化管理）
  gui_app.py       内嵌远程 agent，读写 yamls/proxy-groups.yaml 与 rules.yaml
  build.py         打包成单文件 exe
dist/              打包好的 Windows 单文件 exe
  ShellCrashDeployer.exe    部署器
  ShellCrashRuleManager.exe 规则管理器
```

## 自己构建

```bash
pip install -r gui/requirements.txt
cd gui && python build.py     # 读 ../deploy/*.sh，重新生成 embedded_scripts.py，产出 ../dist/ShellCrashDeployer.exe
```

## 卸载

```bash
sudo sh /tmp/sc_uninstall.sh          # 交互确认
sudo sh /tmp/sc_uninstall.sh --yes    # 免确认
```

停止服务、清 nft 规则、删 `/etc/ShellCrash`、还原别名与自启。之后 `unalias crash` 或重新登录。

## 免责声明

仅供个人学习与自用网络环境，请遵守当地法律法规与你的网络服务条款。ShellCrash / mihomo
版权归各自作者所有，本仓库仅提供部署封装。

## License

MIT
