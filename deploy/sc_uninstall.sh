#!/bin/sh
# ============================================================
# 卸载 ShellCrash 并还原网络（防火墙规则、别名、开机自启）
# 用法：sudo sh sc_uninstall.sh [--yes]
# ============================================================
set -u
CRASHDIR=${CRASHDIR:-/etc/ShellCrash}
[ "$(id -u)" != 0 ] && exec sudo -E sh "$0" "$@"

if [ "${1:-}" != "--yes" ]; then
    echo "将要执行："
    echo "  1) 停止并禁用 shellcrash 服务"
    echo "  2) 删除 nft/iptables 中 ShellCrash 的规则，恢复流量直连"
    echo "  3) 删除 $CRASHDIR、/tmp/ShellCrash、/usr/bin/crash"
    echo "  4) 还原 /etc/profile 中的 crash 别名"
    printf "确认卸载？(yes/no) "
    read -r ok
    [ "$ok" = yes ] || { echo "已取消"; exit 0; }
fi

echo "== 停止服务 =="
systemctl disable --now shellcrash 2>/dev/null
systemctl disable --now shellcrash-lan-forward 2>/dev/null
rm -f /etc/systemd/system/shellcrash-lan-forward.service
systemctl daemon-reload 2>/dev/null
sh "$CRASHDIR"/start.sh stop 2>/dev/null
sh "$CRASHDIR"/starts/fw_stop.sh 2>/dev/null

echo "== 清理残留规则 =="
nft delete table inet shellcrash 2>/dev/null && echo "  nft 表已删除"
# 放行内网转发的规则（Docker 的 FORWARD DROP 修补）
iptables -S FORWARD 2>/dev/null | grep -F 'sc-lan-forward' | sed 's/^-A /-D /' | while read -r r; do
    iptables $r 2>/dev/null && echo "  已删除：$r"
done
for t in nat mangle; do
    iptables -t $t -D PREROUTING -j SHELLCRASH 2>/dev/null
    iptables -t $t -F SHELLCRASH 2>/dev/null && iptables -t $t -X SHELLCRASH 2>/dev/null
done
ip rule del fwmark 0x162 lookup 100 2>/dev/null
ip route flush table 100 2>/dev/null

echo "== 删除文件 =="
rm -rf "$CRASHDIR" /tmp/ShellCrash "$TMPDL" 2>/dev/null
rm -f /usr/bin/crash

echo "== 还原别名 =="
if [ -f /etc/profile.bak.shellcrash ]; then
    cp -a /etc/profile.bak.shellcrash /etc/profile
    echo "  已从备份还原 /etc/profile"
else
    sed -i '/^alias crash=/d; /^export CRASHDIR=/d' /etc/profile 2>/dev/null && echo "  已移除 crash 别名"
fi

echo
echo "卸载完成。当前 shell 执行 unalias crash 或重新登录后生效。"
echo "若原本使用飞牛 clash-for-fnos，请到应用中心重新启动它。"
