# 部署手册 · ShellCrash 局域网网关模式

> 目标：在 NAS 上装 ShellCrash，局域网内的电脑/手机 **只改网关和 DNS** 就能科学上网，不用装任何客户端。
> 本文所有内容均来自 2026-11-18 在飞牛 fnOS（J1900 / Debian 12 / 网卡 enp1s0-ovs）上的实际部署与踩坑记录。

---

## 一、包内文件

| 文件 | 作用 |
|---|---|
| `sc_deploy.sh` | 一键部署（体检 → 下载 → 安装 → 配内核 → 写配置 → 启动） |
| `sc_verify.sh` | 端到端验证（在 NAS 上建临时网络命名空间模拟客户机） |
| `sc_uninstall.sh` | 卸载并还原网络 |

### 已验证环境（2026-11-18 实测）

- 系统：飞牛 fnOS / Debian 12 bookworm，内核 6.18.18，x86_64
- 机型 CPU：Intel J1900（**无 AVX2**），网卡 `enp1s0-ovs`
- ShellCrash 1.9.5beta4 + Mihomo Meta v1.19.34（amd64-compatible）
- 下载源实测（该网络环境）：
  - ✅ `fastly.jsdelivr.net`、`cdn.jsdelivr.net`（安装包，176K）
  - ✅ `ghfast.top`、`ghproxy.net`（安装包 + GitHub 内核加速）
  - ❌ `raw.githubusercontent.com`、`gh-proxy.com`（安装包拉取失败，但内核直连 GitHub 可通）
  - 脚本已按「按顺序回退」处理，无需手工切换

## 二、部署前必须确认的 3 件事

1. **NAS 上是否已有别的代理在跑**。飞牛应用中心那个 `clash-for-fnos` 会占 `53`、`7890`，两个一起跑会互相抢端口。先在应用中心停掉它，并关掉它的开机自启。
2. **NAS 的 CPU 是否支持 AVX2**。mihomo 官方标准版内核需要 AVX2，J1900/N5105 这类低端 U 没有，必须用 `amd64-compatible` 版（脚本会自动判断）。
3. **NAS 的网卡名**。如果叫 `enp1s0-ovs` 这种含 `ovs` 的名字，ShellCrash 自动探测网段时会跳过它 —— 必须手动指定网段（脚本通过 `cust_host_ipv4` 自动处理）。

## 三、一键部署

把三个脚本传到 NAS（例如 `/tmp`），执行：

```bash
# 1) 先体检，不改任何东西
sudo sh /tmp/sc_deploy.sh --check

# 2) 正式部署（网段必填，订阅可选）
sudo LAN_CIDR=192.168.1.0/24 \
     SUB_URL='https://你的订阅地址' \
     sh /tmp/sc_deploy.sh
```

> 说明：`--check` 模式已在真机实测（环境识别、AVX2 判断、ovs 网卡识别、网段换算、端口占用检测均正确）。部署流程是把本次人工成功步骤固化成脚本，**建议任何新机器都先 `--check` 看一遍再正式跑**。

### 可用参数

| 变量 | 默认 | 说明 |
|---|---|---|
| `LAN_CIDR` | 自动探测默认路由网段 | 要走代理的局域网网段，如 `192.168.1.0/24` |
| `SUB_URL` | 空 | Clash 格式订阅地址；留空则装完在菜单里手动导入 |
| `SUB_NAME` | `main` | 订阅在 `providers/` 里的名字 |
| `DNS_MOD` | `fake-ip` | 可选 `mix`（fake-ip + redir-host 混合，兼容性更好） |
| `AREA` | `1` | `1`=只代理局域网客户端；`2`=局域网 + NAS 本机 |
| `CORE_VER` | 最新 | 钉死内核版本，如 `v1.19.34` |

## 四、手动分步（脚本不适用时）

```bash
# 1. 装 ShellCrash（非交互）
curl -sLk -o /tmp/ShellCrash.tar.gz \
  https://fastly.jsdelivr.net/gh/juewuy/ShellCrash@master/ShellCrash.tar.gz
mkdir -p /tmp/scprobe && tar -zxf /tmp/ShellCrash.tar.gz -C /tmp/scprobe
sudo cp -a /tmp/scprobe/. /etc/ShellCrash/
sudo sh -c 'CRASHDIR=/etc/ShellCrash my_alias=crash language=chs \
  url=https://fastly.jsdelivr.net/gh/juewuy/ShellCrash@master release_type=master \
  . /etc/ShellCrash/init.sh'

# 2. 装内核（无 AVX2 用 compatible 版）
curl -sLk -o /tmp/core.gz \
  https://gh-proxy.com/https://github.com/MetaCubeX/mihomo/releases/download/v1.19.34/mihomo-linux-amd64-compatible-v1.19.34.gz
sudo cp /tmp/core.gz /etc/ShellCrash/CrashCore.gz

# 3. 关键设置
sudo sed -i 's|^redir_mod=.*|redir_mod=Tproxy|; s|^dns_mod=.*|dns_mod=fake-ip|' \
  /etc/ShellCrash/configs/ShellCrash.cfg
echo 'cust_host_ipv4=192.168.1.0/24' | sudo tee -a /etc/ShellCrash/configs/ShellCrash.cfg

# 4. 启动
sudo /etc/ShellCrash/start.sh start
sudo systemctl enable shellcrash
```

## 五、客户端怎么设置

| 设备 | 操作 |
|---|---|
| Windows | 网络适配器 → IPv4 属性 → 网关和 DNS 都填 NAS 的 IP |
| macOS | 系统设置 → 网络 → 详细信息 → TCP/IP（网关）+ DNS |
| iPhone/iPad | Wi-Fi → 当前网络 → 配置 IP → 手动 → 路由器填 NAS IP |
| Android | Wi-Fi → 修改网络 → 高级 → 网关 + DNS1 |
| 路由器（最省事） | LAN/DHCP 里把「网关」和「DNS」下发为 NAS IP，全家设备免设置 |

> DNS 其实不填也会被劫持（nft 会把 LAN 的 53 端口转到内部 1053），但填 NAS 最稳。

## 六、验证

```bash
sudo sh /tmp/sc_verify.sh                 # 自动读配置里的网段
sudo sh /tmp/sc_verify.sh 192.168.1.0/24  # 或手动指定
```

共 6 项：DNS、透明路径 google、本机 7890 对照、出口 IP（参考）、国内直连、非 443 端口。

判定标准：

- 第 2、3 步 `google=204` → 透明代理链路与节点都正常
- `baidu` 耗时 <0.5s → 国内流量走直连，分流正常
- `httpbin=200` → 非 80/443 端口也走代理（`common_ports=OFF` 生效）
- 出口 IP 显示本机公网 IP → 该站点命中直连规则，属正常，不是故障

> 脚本每次用随机 IP 建命名空间并带 3 次重试 —— 固定 IP 反复测试会被 conntrack 残留连接拖累，出现偶发 `000`。

不建命名空间的手工验证：在客户机上 `curl -I https://www.google.com`（不设任何代理）应返回 200/204。

## 七、实际操作中踩到的坑（重要）

| 坑 | 现象 | 处理 |
|---|---|---|
| **CPU 无 AVX2** | 内核报 `can only be run on AMD64 processors with v3 microarchitecture` | 换 `amd64-compatible` 版内核 |
| **网卡名含 ovs** | 防火墙规则网段错误、客户机不通 | 用 `cust_host_ipv4` 手动指定网段 |
| **普通用户跑 crash** | `touch: cannot touch '/tmp/ShellCrash/crash_start_time': Permission denied`、`sed ... Permission denied` | ShellCrash 必须 root 运行，见下条 |
| **/etc/profile 的别名优先于命令** | 明明装了 `/usr/bin/crash` 却仍以普通用户身份跑菜单 | 别名改成 `alias crash="sudo -E bash /etc/ShellCrash/menu.sh"`，或 `unalias crash` |
| **新手引导会重置配置** | 进菜单选了「1) 推荐配置」，`Tproxy` 被改回 `Mix`、`fake-ip` 被改成 `mix`，代理失效 | 在 `ShellCrash.cfg` 写 `userguide=1` 跳过引导 |
| **非 80/443 端口不走代理** | 只有网页能用，其他端口服务连不上 | `common_ports=OFF`（默认的「只代理常用端口」要关掉） |

## 八、日常维护

```bash
crash          # 主菜单（会自动 sudo，无参数进菜单）
crash restart  # 重启服务
crash stop     # 停止服务（此时改过网关的电脑会断网）
```

菜单速查：

- `1` 启动/重启服务
- `2` 功能设置（改模式、劫持范围、DNS 模式）
- `6` 配置文件管理（**更新订阅 / 换节点**）
- `7` 访问与控制（面板端口、密钥）
- `8` 工具与优化（清缓存、重装内核）

服务层命令：

```bash
sudo systemctl status shellcrash
sudo systemctl restart shellcrash
journalctl -u shellcrash -n 50
```

## 九、端口占用

| 端口 | 用途 |
|---|---|
| 7890 | 混合代理（HTTP/SOCKS5），给局域网设备手动设代理用 |
| 7892 | redir 端口 |
| 7893 | TProxy 端口（网关模式核心） |
| 1053 | 内部 DNS（LAN 的 53 由 nft 重定向到这里） |
| 9999 | 面板（可选，默认只监听本机） |

## 十、回滚

```bash
sudo sh /tmp/sc_uninstall.sh          # 交互确认
sudo sh /tmp/sc_uninstall.sh --yes    # 免确认
```

卸载后：`unalias crash` 或重新登录；若原来用飞牛 `clash-for-fnos`，去应用中心重新启动它。

## 十一、故障速查

| 症状 | 排查方向 |
|---|---|
| 客户机改了网关后完全没网 | NAS 关机 / shellcrash 没起来 / 客户机与 NAS 不在同网段；先把网关改回路由器应急 |
| 能上国内网、不能上外网 | 内核没起来（`crash` 看状态）/ 订阅节点全挂 / `redir_mod` 被改成 Mix |
| 网页能用，其他应用不行 | `common_ports` 还是 `ON` |
| DNS 解析失败 | 53 被别的服务占用；`ss -ulnp \| grep :53` 查 |
| 只有 443 能用，80 不行 | 检查 nft 表 `nft list table inet shellcrash` 是否只有 tcp 规则 |
| 重启 NAS 后失效 | `systemctl is-enabled shellcrash`；飞牛应用中心是否把 clash-for-fnos 又拉起来了 |
