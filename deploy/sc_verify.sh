#!/bin/sh
# ============================================================
# 验证「局域网电脑把网关+DNS 指向 NAS」能否走代理
# 原理：在 NAS 上建临时网络命名空间模拟一台客户机，测完自动清理
# 用法：sudo sh sc_verify.sh [局域网网段] [内网服务器IP]
#   例：sudo sh sc_verify.sh 192.168.1.0/24 192.168.0.6
# ============================================================
set -u
CRASHDIR=${CRASHDIR:-/etc/ShellCrash}
[ "$(id -u)" != 0 ] && exec sudo -E sh "$0" "$@"

LAN_CIDR="${1:-}"
[ -z "$LAN_CIDR" ] && [ -f "$CRASHDIR/configs/ShellCrash.cfg" ] && \
    LAN_CIDR=$(awk -F= '/^cust_host_ipv4=/{print $2}' "$CRASHDIR/configs/ShellCrash.cfg")
[ -z "$LAN_CIDR" ] && { echo "请指定网段，例：sh $0 192.168.1.0/24"; exit 1; }

BASE=$(echo "$LAN_CIDR" | cut -d/ -f1 | awk -F. '{print $1"."$2"."$3}')
NAS_IP=$(ip -4 addr | awk -v P="$BASE." '$0 ~ "inet "P {gsub(/\/.*/,"",$2); print $2; exit}')
# 随机末位，避免与上次测试的 conntrack 残留撞车
R=$(( ($$ % 8) + 240 ))          # 240~247
CLI=$BASE.$R
GW=$BASE.$((R + 5))              # 245~252
NS=sc_verify

cleanup() {
    ip netns del $NS 2>/dev/null
    ip link del scveth0 2>/dev/null
    rm -rf /etc/netns/$NS 2>/dev/null
}
trap cleanup EXIT INT TERM

echo "NAS 地址：$NAS_IP   模拟客户机：$CLI   模拟网关：$GW"
cleanup

ip netns add $NS || { echo "创建 netns 失败（容器环境可能不支持）"; exit 1; }
ip link add scveth0 type veth peer name scveth1 || { echo "创建 veth 失败"; exit 1; }
ip link set scveth1 netns $NS
ip addr add $GW/24 dev scveth0
ip link set scveth0 up
ip netns exec $NS ip link set lo up
ip netns exec $NS ip link set scveth1 up
ip netns exec $NS ip addr add $CLI/32 dev scveth1
ip netns exec $NS ip route add $GW/32 dev scveth1
ip netns exec $NS ip route add default via $GW
# netns 内用 NAS 做 DNS（宿主的 resolv.conf 若指向 127.0.0.1 在命名空间里无效）
mkdir -p /etc/netns/$NS
echo "nameserver $NAS_IP" > /etc/netns/$NS/resolv.conf
sleep 1

echo "=== 1. DNS（问 $NAS_IP） ==="
ip netns exec $NS nslookup www.youtube.com "$NAS_IP" 2>&1 | tail -4

echo "=== 2. 纯网关访问 google（无任何代理设置） ==="
try=0; code=000
while [ "$try" -lt 3 ]; do
    try=$((try + 1))
    code=$(ip netns exec $NS timeout 25 curl -4 -s -o /dev/null -w '%{http_code}' https://www.google.com/generate_204)
    [ "$code" = 204 ] && break
    sleep 2
done
echo "  google=$code（尝试 $try 次）"

echo "=== 3. NAS 本机代理端口 7890（对照：验节点是否可用） ==="
curl -4 -s -x "http://127.0.0.1:7890" -o /dev/null -m 20 \
    -w "  google(经7890)=%{http_code}\n" https://www.google.com/generate_204 \
    || echo "  google(经7890)=失败（节点不通或端口未监听）"

echo "=== 4. 出口 IP（参考值：部分站点命中直连规则会显示本机公网 IP） ==="
for api in https://api.ipify.org https://ipinfo.io/ip; do
    r=$(ip netns exec $NS timeout 12 curl -4 -s "$api" 2>/dev/null | tr -d '\n' | head -c 60)
    printf '  %-24s → %s\n' "$api" "${r:-取不到（接口被墙/超时，不影响判定）}"
done

echo "=== 5. 国内站点应直连（未绕代理） ==="
ip netns exec $NS timeout 25 curl -4 -s -o /dev/null -w "  baidu=%{http_code} 耗时=%{time_total}s\n" https://www.baidu.com

echo "=== 6. 非 80/443 端口 ==="
ip netns exec $NS timeout 25 curl -4 -s -o /dev/null -w "  httpbin=%{http_code}\n" http://httpbin.org/get

echo "=== 7. 内网互访（跨网段访问其它内网服务器）==="
FW=$(iptables -S FORWARD 2>/dev/null | head -1)
if echo "$FW" | grep -q 'FORWARD DROP'; then
    if iptables -S FORWARD 2>/dev/null | grep -q 'sc-lan-forward'; then
        echo "  FORWARD 策略 DROP，但已放行内网网段 → 正常"
    else
        echo "  ⚠ FORWARD 策略 DROP 且没有放行规则（Docker 装的）"
        echo "    → 客户机改网关后能上外网，但访问 192.168.0.x 之类的其它网段会超时"
        echo "    → 修复：sh /etc/ShellCrash/lan_forward.sh   （或重跑 sc_deploy.sh）"
    fi
else
    echo "  FORWARD 策略非 DROP → 正常"
fi
if iptables -t nat -S POSTROUTING 2>/dev/null | grep -q 'sc-lan-forward-snat'; then
    echo "  跨网段 SNAT：已启用（服务器看到的源是本机地址，回程对称）"
else
    echo "  ⚠ 跨网段 SNAT 未启用"
    echo "    → 症状：能 ping 通，但 SMB 共享文件夹（445）、SSH 等连接超时"
    echo "    → 原因：服务器回包要绕回客户机所在网段，另一侧路由器通常没有这条路由"
    echo "    → 修复：sh /etc/ShellCrash/lan_forward.sh   （v1.5 起部署脚本会自动加）"
fi

INTRANET="${2:-}"
if [ -n "$INTRANET" ]; then
    echo "  实测 $INTRANET（客户机视角）："
    if ip netns exec $NS timeout 10 ping -c 3 -W 2 "$INTRANET" >/tmp/sc_ping.txt 2>&1; then
        echo "    ping：$(tail -2 /tmp/sc_ping.txt | head -1)"
    else
        echo "    ⚠ ping 不通"
    fi
    for p in 445 139; do
        if ip netns exec $NS timeout 6 bash -c "exec 3<>/dev/tcp/$INTRANET/$p" 2>/dev/null; then
            echo "    端口 $p：open"
        else
            echo "    端口 $p：不通"
        fi
    done
    rm -f /tmp/sc_ping.txt
fi

echo
echo "判定标准："
echo "  google=204（第2、3步都是）  → 透明代理与代理端口都通"
echo "  baidu 耗时 < 0.5s           → 国内流量未绕代理，分流正常"
echo "  出口 IP 显示本机公网 IP     → 该站点命中直连规则，属正常"
echo "  httpbin=200                 → 非 80/443 端口也走代理（common_ports=OFF 生效）"
echo "  第 7 步无 ⚠                 → 客户机能访问其它内网网段"
