#!/bin/sh
# ============================================================
# ShellCrash 局域网网关模式 · 一键部署脚本（飞牛 fnOS / Debian 系）
# 用法：
#   sh sc_deploy.sh --check          # 只体检，不改任何东西
#   LAN_CIDR=192.168.1.0/24 SUB_URL="https://..." sh sc_deploy.sh
# 说明：非 root 会自动 sudo 提权
# ============================================================
set -u

CRASHDIR=/etc/ShellCrash
ALIAS=crash
TMPDL=/tmp/sc_deploy_dl

LAN_CIDR="${LAN_CIDR:-}"
SUB_URL="${SUB_URL:-}"
SUB_NAME="${SUB_NAME:-main}"
DNS_MOD="${DNS_MOD:-fake-ip}"
AREA="${AREA:-1}"          # 1=仅代理局域网客户端  2=局域网+NAS本机
CORE_VER="${CORE_VER:-}"   # 留空=自动查最新，可钉死如 v1.19.34

MODE=deploy
case "${1:-}" in
    --check|-c) MODE=check ;;
    -h|--help)
        sed -n '2,12p' "$0"; exit 0 ;;
    "") ;;
    *) echo "未知参数：$1（可用 --check / --help）"; exit 1 ;;
esac

# 颜色：仅在真实终端输出。被图形工具/脚本以 SSH exec 方式调用时（非 tty）自动关闭，
# 避免日志里出现 [36m 这类转义残留。也可用 NO_COLOR=1 强制关闭。
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ -z "${SC_NO_COLOR:-}" ]; then
    c_g() { printf '\033[32m%s\033[0m\n' "$*"; }
    c_y() { printf '\033[33m%s\033[0m\n' "$*"; }
    c_r() { printf '\033[31m%s\033[0m\n' "$*"; }
    c_b() { printf '\033[36m%s\033[0m\n' "$*"; }
    step() { printf '\n\033[36m==== %s ====\033[0m\n' "$*"; }
else
    c_g() { printf '%s\n' "$*"; }
    c_y() { printf '%s\n' "$*"; }
    c_r() { printf '%s\n' "$*"; }
    c_b() { printf '%s\n' "$*"; }
    step() { printf '\n==== %s ====\n' "$*"; }
fi
die() { c_r "[失败] $*"; exit 1; }

[ "$MODE" = deploy ] && [ "$(id -u)" != 0 ] && exec sudo -E sh "$0" "$@"

# ---------- 0. 环境体检 ----------
ARCH=""; CPUCORE=""; AVX2=no; LAN_DEV=""; LAN_GW=""; TOOLS_OK=1

# 把 192.168.1.100/24 换算成网络地址 192.168.1.0/24
to_netaddr() {
    echo "$1" | awk -F'[./]' 'NF==5{
        p=$5+0; out=""
        for(i=1;i<=4;i++){
            v=$i+0; bits=p-(i-1)*8
            if(bits>=8) m=v
            else if(bits<=0) m=0
            else { step=2^(8-bits); m=int(v/step)*step }
            out=out (i>1?".":"") m
        }
        print out "/" p
    }'
}

detect_env() {
    step "环境检测"
    [ -f /etc/os-release ] && . /etc/os-release && echo "系统：$PRETTY_NAME"
    echo "内核：$(uname -r)    架构：$(uname -m)"

    case "$(uname -m)" in
        x86_64|amd64)
            ARCH=amd64; CPUCORE=amd64
            grep -qw avx2 /proc/cpuinfo && AVX2=yes
            echo "CPU 指令集：AVX2=$AVX2（$([ "$AVX2" = yes ] && echo '用标准版内核' || echo '需 compatible 内核')）"
            ;;
        aarch64|arm64) ARCH=arm64; CPUCORE=arm64 ;;
        armv7l) ARCH=armv7; CPUCORE=armv7 ;;
        *) die "暂不支持的架构：$(uname -m)" ;;
    esac

    echo -n "依赖工具："
    for t in curl tar gzip nft ss ip; do
        if command -v "$t" >/dev/null 2>&1 || [ -x "/usr/sbin/$t" ] || [ -x "/sbin/$t" ]; then
            echo -n "$t=ok "
        else
            echo -n "$t=缺失 "; TOOLS_OK=0
        fi
    done
    echo
    [ "$TOOLS_OK" = 0 ] && c_y "提示：Debian 系执行 apt install -y curl tar gzip nftables iproute2"

    # 默认路由 & 局域网网段
    LAN_GW=$(ip route 2>/dev/null | awk '/^default/{print $3; exit}')
    LAN_DEV=$(ip route 2>/dev/null | awk '/^default/{print $5; exit}')
    AUTO_CIDR=""
    [ -n "$LAN_DEV" ] && AUTO_CIDR=$(ip -o -4 addr show dev "$LAN_DEV" 2>/dev/null | awk '{print $4; exit}')
    echo "默认网关：${LAN_GW:-未知}    出口网卡：${LAN_DEV:-未知}    本机地址：${AUTO_CIDR:-未知}"
    case "$LAN_DEV" in
        *ovs*) c_y "注意：网卡名含 ovs —— ShellCrash 自动探测会跳过此类网卡，必须用 cust_host_ipv4 手动指定（本脚本已自动处理）" ;;
    esac
    [ -z "$LAN_CIDR" ] && LAN_CIDR=$(to_netaddr "$AUTO_CIDR")
    [ -z "$LAN_CIDR" ] && die "无法自动识别局域网网段，请用 LAN_CIDR=192.168.x.0/24 指定"
    echo "将用于代理的局域网网段：$LAN_CIDR"

    echo "IP 转发：$(cat /proc/sys/net/ipv4/ip_forward)（0 表示未开启，脚本会打开）"
    if iptables -S FORWARD 2>/dev/null | grep -q '^-P FORWARD DROP'; then
        if iptables -S FORWARD 2>/dev/null | grep -q 'sc-lan-forward'; then
            echo "内网转发：已放行（sc-lan-forward 规则存在）"
        else
            c_y "iptables FORWARD 策略是 DROP（Docker 写的）—— 客户机改网关后将访问不了其它内网网段（如 192.168.0.x），部署时脚本会自动放行"
        fi
    fi

    # 端口占用（把同一个代理内核占用的多个端口合并成一条，避免刷屏）
    PROXY_PORTS=""; PROXY_NAME=""; PROXY_PID=""
    for p in 53 7890 7891 7892 7893 9999; do
        if ss -H -ltnu 2>/dev/null | grep -qE "[:.]$p[[:space:]]"; then
            who=$(ss -ltnup 2>/dev/null | grep -E "[:.]$p[[:space:]]" | grep -o 'users:.*' | head -1 | sed 's/[[:space:]]*$//')
            pname=$(echo "$who" | sed -n 's/.*(("\([^"]*\)".*/\1/p')
            ppid=$(echo "$who" | sed -n 's/.*pid=\([0-9]*\).*/\1/p')
            case "$pname" in
                CrashCore|mihomo|clash|Clash|Meta|clash-meta|sing-box)
                    PROXY_PORTS="$PROXY_PORTS $p"
                    [ -z "$PROXY_NAME" ] && PROXY_NAME="$pname"
                    [ -z "$PROXY_PID" ] && PROXY_PID="$ppid"
                    ;;
                "")
                    c_y "端口 $p 已被占用（进程名需 root 才能看到，用 sudo 执行本脚本即可显示）" ;;
                *)
                    c_y "端口 $p 已被占用 → $pname${ppid:+（pid=$ppid）}" ;;
            esac
            case "$p" in
                53|7890|7892|7893) PORT_CONFLICT=1 ;;
            esac
        fi
    done
    if [ -n "$PROXY_PORTS" ]; then
        c_y "本机已有代理内核在运行：$PROXY_NAME${PROXY_PID:+（pid=$PROXY_PID）}，占用端口${PROXY_PORTS}"
        c_y "  → 说明这台 NAS 已经装过代理，继续部署会接管并重启它；只想检查请直接结束"
    fi

    # 已有代理类进程
    for name in mihomo clash Meta clash-meta sing-box; do
        n=$(ps -eo comm 2>/dev/null | grep -cx "$name")
        [ "$n" -gt 0 ] && c_y "检测到已运行进程：$name（$n 个）"
    done
}

# ---------- 1. 下载 ShellCrash ----------
fetch_sc() {
    step "下载 ShellCrash"
    mkdir -p "$TMPDL"
    PKG="$TMPDL/ShellCrash.tar.gz"
    SRC=""
    for m in "https://fastly.jsdelivr.net/gh/juewuy/ShellCrash@master" \
             "https://cdn.jsdelivr.net/gh/juewuy/ShellCrash@master" \
             "https://ghfast.top/https://raw.githubusercontent.com/juewuy/ShellCrash/master" \
             "https://ghproxy.net/https://raw.githubusercontent.com/juewuy/ShellCrash/master" \
             "https://raw.githubusercontent.com/juewuy/ShellCrash/master"; do
        echo "尝试：$m"
        curl -sLk -m 60 -o "$PKG" "$m/ShellCrash.tar.gz" 2>/dev/null
        if tar -tzf "$PKG" >/dev/null 2>&1; then
            SRC="$m"; c_g "下载成功（$m）"; break
        fi
    done
    [ -n "$SRC" ] || die "ShellCrash 所有下载源均失败，请检查网络或手动上传安装包"
}

# ---------- 2. 安装 ShellCrash ----------
install_sc() {
    step "安装 ShellCrash 到 $CRASHDIR"
    rm -rf "$TMPDL/pkg"; mkdir -p "$TMPDL/pkg"
    tar -zxf "$PKG" -C "$TMPDL/pkg" || die "解压失败"
    [ -f "$TMPDL/pkg/version" ] || die "安装包结构异常"

    mkdir -p "$CRASHDIR"
    cp -a "$TMPDL/pkg/." "$CRASHDIR"/ || die "复制到 $CRASHDIR 失败"
    rm -f "$CRASHDIR"/menus/set_crashdir.sh

    version=$(cat "$CRASHDIR/version")
    CRASHDIR="$CRASHDIR" my_alias="$ALIAS" language=chs url="$SRC" release_type=master version="$version" \
        sh -c '. "$CRASHDIR"/init.sh' || die "init.sh 执行失败"
    c_g "ShellCrash $version 已安装"
}

# ---------- 3. 安装 mihomo 内核 ----------
install_core() {
    step "安装 mihomo 内核（$ARCH）"
    if [ -z "$CORE_VER" ]; then
        CORE_VER=$(curl -sLk -m 20 https://api.github.com/repos/MetaCubeX/mihomo/releases/latest 2>/dev/null \
            | grep -o '"tag_name": *"[^"]*"' | head -1 | cut -d'"' -f4)
    fi
    [ -n "$CORE_VER" ] || CORE_VER=v1.19.34
    echo "版本：$CORE_VER"

    case "$ARCH" in
        amd64) [ "$AVX2" = yes ] && F="mihomo-linux-amd64-$CORE_VER.gz" || F="mihomo-linux-amd64-compatible-$CORE_VER.gz" ;;
        arm64) F="mihomo-linux-arm64-$CORE_VER.gz" ;;
        armv7) F="mihomo-linux-armv7-$CORE_VER.gz" ;;
    esac
    U="https://github.com/MetaCubeX/mihomo/releases/download/$CORE_VER/$F"
    echo "文件：$F"

    CORE="$TMPDL/core.gz"; rm -f "$CORE"
    for p in "" "https://ghfast.top/" "https://gh-proxy.com/"; do
        echo "尝试：${p:-直连}${U}"
        curl -sLk -m 300 -o "$CORE" "${p}${U}" 2>/dev/null
        if gzip -t "$CORE" 2>/dev/null && [ "$(wc -c <"$CORE")" -gt 1000000 ]; then
            c_g "内核下载成功（$(du -h "$CORE" | cut -f1)）"; break
        fi
        rm -f "$CORE"
    done
    [ -f "$CORE" ] || die "内核下载失败，可手动下载后放到 $CRASHDIR/CrashCore.gz"

    rm -f "$CRASHDIR"/CrashCore.gz "$CRASHDIR"/CrashCore.tar.gz
    cp -f "$CORE" "$CRASHDIR"/CrashCore.gz
    chmod 644 "$CRASHDIR"/CrashCore.gz
}

# ---------- 4. 写入配置 ----------
setkey() { # $1=key $2=value
    CFG="$CRASHDIR/configs/ShellCrash.cfg"
    [ -f "$CFG" ] || : >"$CFG"
    if grep -q "^$1=" "$CFG"; then sed -i "s|^$1=.*|$1=$2|" "$CFG"; else echo "$1=$2" >>"$CFG"; fi
}

write_config() {
    step "写入 ShellCrash 设置"
    setkey crashcore meta
    setkey cpucore "$CPUCORE"
    setkey redir_mod Tproxy
    setkey firewall_mod nftables
    setkey firewall_area "$AREA"
    setkey dns_mod "$DNS_MOD"
    setkey ipv6_redir OFF
    setkey common_ports OFF
    setkey replace_default_host_ipv4 ON
    setkey cust_host_ipv4 "$LAN_CIDR"
    setkey skip_cert ON
    setkey userguide 1          # 跳过新手引导（否则会重置模式为 Mix）

    # 基础配置：来自订阅
    if [ -n "$SUB_URL" ]; then
        step "导入订阅"
        mkdir -p "$CRASHDIR"/yamls
        SUBF="$TMPDL/sub.yaml"
        curl -sLk -m 60 -A "clash.meta" -o "$SUBF" "$SUB_URL" || die "订阅下载失败"
        grep -q '^proxies:' "$SUBF" || die "订阅里没有 proxies: 段，可能不是 Clash 格式（需先用订阅转换）"
        sed -n '/^proxies:/,$p' "$SUBF" >"$CRASHDIR"/yamls/config.yaml
        mkdir -p "$CRASHDIR"/providers
        cp -f "$SUBF" "$CRASHDIR"/providers/"$SUB_NAME"
        echo "$SUB_NAME $SUB_URL 3 12 clash.meta" >"$CRASHDIR"/configs/providers.cfg
        c_g "订阅已导入：$(grep -c '^  - name:' "$CRASHDIR"/yamls/config.yaml) 个节点"
    else
        c_y "未提供 SUB_URL：装完后请运行 crash → 6) 配置文件管理 → 导入订阅"
    fi
}

# ---------- 4.5 放行局域网 → 其它内网的转发 ----------
# 飞牛装了 Docker，Docker 会把 iptables 的 FORWARD 策略设成 DROP。
# 这样一来：客户机把网关指向 NAS 后，所有「客户机 → 其它网段（192.168.x/10.x/172.16-31.x）」
# 的包在 NAS 上被静默丢弃，表现为能上外网、但访问不了另一个网段的服务器。
allow_lan_forward() {
    step "放行局域网到内网的转发（修 Docker 的 FORWARD DROP）"
    command -v iptables >/dev/null 2>&1 || { c_y "没有 iptables，跳过"; return 0; }

    if ! iptables -S FORWARD 2>/dev/null | grep -q '^-P FORWARD DROP'; then
        c_g "FORWARD 策略不是 DROP，无需修改"
        return 0
    fi

    cat >"$CRASHDIR"/lan_forward.sh <<'EOF'
#!/bin/sh
# 放行「局域网客户端 → 其它内网网段」的转发。
# 背景：Docker 把 FORWARD 策略设为 DROP，导致网关指向本机的客户机访问不了其它网段。
# 幂等：每次先按注释标签清掉自己加的规则，再加回去。
set -u
LAN_CIDR="${1:-}"
[ -z "$LAN_CIDR" ] && [ -f /etc/ShellCrash/configs/ShellCrash.cfg ] && \
    LAN_CIDR=$(awk -F= '/^cust_host_ipv4=/{print $2}' /etc/ShellCrash/configs/ShellCrash.cfg)
[ -n "$LAN_CIDR" ] || exit 0
command -v iptables >/dev/null 2>&1 || exit 0

iptables -S FORWARD 2>/dev/null | grep -F 'sc-lan-forward' | sed 's/^-A /-D /' | while read -r r; do
    iptables $r 2>/dev/null || true
done

# 回程包（内网服务器 → 客户机）走 ESTABLISHED/RELATED，必须放行
iptables -I FORWARD 1 -m comment --comment sc-lan-forward \
    -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT

# 只放行到内网网段的新连接：192.168/16、10/8、172.16/12、100.64/10（Tailscale 等）
for net in 192.168.0.0/16 10.0.0.0/8 172.16.0.0/12 100.64.0.0/10; do
    iptables -I FORWARD 1 -m comment --comment sc-lan-forward -s "$LAN_CIDR" -d "$net" -j ACCEPT
done
EOF
    chmod 755 "$CRASHDIR"/lan_forward.sh
    sh "$CRASHDIR"/lan_forward.sh "$LAN_CIDR" || { c_y "规则写入失败（不影响代理，仅内网互访）"; return 0; }

    # 开机自动重建（iptables 规则重启即丢）
    if command -v systemctl >/dev/null 2>&1; then
        cat >/etc/systemd/system/shellcrash-lan-forward.service <<'EOF'
[Unit]
Description=ShellCrash: 放行局域网到内网的转发（修补 Docker 的 FORWARD DROP）
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh /etc/ShellCrash/lan_forward.sh

[Install]
WantedBy=multi-user.target
EOF
        systemctl daemon-reload 2>/dev/null
        systemctl enable shellcrash-lan-forward >/dev/null 2>&1
        echo "开机自启：$(systemctl is-enabled shellcrash-lan-forward 2>/dev/null)"
    fi
    c_g "已放行 $LAN_CIDR → 192.168.0.0/16、10.0.0.0/8、172.16.0.0/12、100.64.0.0/10"
    c_y "  说明：只放行内网网段。客户机访问互联网的流量仍由 ShellCrash 接管，不会绕过代理"
}

# ---------- 5. 修好 crash 命令 ----------
fix_alias() {
    step "修复 crash 命令（别名优先级坑）"
    cat >/usr/bin/crash <<'EOF'
#!/bin/sh
# ShellCrash 入口：自动提权到 root（配置目录只有 root 可写）
[ -z "$CRASHDIR" ] && export CRASHDIR=/etc/ShellCrash
if [ "$(id -u)" != "0" ]; then
    exec sudo -E bash "$CRASHDIR/menu.sh" "$@"
fi
if [ $# -eq 0 ]; then
    exec bash "$CRASHDIR/menu.sh"
fi
exec bash "$CRASHDIR/start.sh" "$@"
EOF
    chmod 755 /usr/bin/crash
    [ -f /etc/profile ] && {
        cp -a /etc/profile /etc/profile.bak.shellcrash 2>/dev/null
        if grep -q '^alias crash=' /etc/profile; then
            sed -i 's#^alias crash=.*#alias crash="sudo -E bash /etc/ShellCrash/menu.sh"#' /etc/profile
        else
            echo 'alias crash="sudo -E bash /etc/ShellCrash/menu.sh"' >>/etc/profile
        fi
    }
    c_g "已安装 /usr/bin/crash（自动 sudo）并修正 /etc/profile 别名"
}

# ---------- 6. 启动 ----------
start_sc() {
    step "启动服务"
    [ "$(cat /proc/sys/net/ipv4/ip_forward)" = 1 ] || {
        sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1
        grep -q '^net.ipv4.ip_forward' /etc/sysctl.conf 2>/dev/null \
            && sed -i 's#^net.ipv4.ip_forward.*#net.ipv4.ip_forward = 1#' /etc/sysctl.conf \
            || echo 'net.ipv4.ip_forward = 1' >>/etc/sysctl.conf
        c_g "已开启 IP 转发"
    }
    bash "$CRASHDIR"/start.sh start
    sleep 3
    if command -v systemctl >/dev/null 2>&1; then
        systemctl enable shellcrash >/dev/null 2>&1
        echo "服务状态：$(systemctl is-active shellcrash) / 开机自启：$(systemctl is-enabled shellcrash 2>/dev/null)"
    fi
    if pidof CrashCore >/dev/null 2>&1 || pgrep -x CrashCore >/dev/null 2>&1; then
        c_g "内核已运行"
    else
        c_r "内核未启动，最近日志："
        tail -20 /tmp/ShellCrash/core_test.log 2>/dev/null
        [ -f /usr/bin/crash ] && c_y "可运行 crash 进菜单手动排查（如内核架构不匹配请换 compatible 版）"
    fi
    ss -tlnp 2>/dev/null | awk 'NR==1||/:7890|:7892|:7893|:9999/' | sed 's/^/  /'
}

# ---------- 主流程 ----------
detect_env
[ "$MODE" = check ] && {
    echo
    c_b "体检完成。部署请执行："
    echo "  LAN_CIDR=${LAN_CIDR} SUB_URL='<你的订阅>' sh $0"
    exit 0
}

[ "${PORT_CONFLICT:-0}" = 1 ] && c_y "警告：关键端口有占用（53/7890/7892/7893），请先停掉旧代理应用（如飞牛应用中心的 clash-for-fnos），否则会互相抢端口"

fetch_sc
install_sc
install_core
write_config
fix_alias
allow_lan_forward
start_sc

step "完成"
cat <<EOF
管理菜单：crash           （需重新登录或先执行 unalias crash）
客户端设置：网关 + DNS 都填本机局域网 IP
内网互访：已放行到 192.168/10/172.16-31 的转发（Docker 的 FORWARD DROP 修补）
验证脚本：sh sc_verify.sh
回滚脚本：sh sc_uninstall.sh
EOF
