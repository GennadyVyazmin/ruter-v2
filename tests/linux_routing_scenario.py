"""Run ONLY in an isolated network namespace: sudo unshare --net python3 ... .

Uses dummy links instead of a live VPN. Real kernel/iproute2 semantics are
exercised, including resource loss; this is NOT a VM power-cycle test.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ruter_v2.routing import Linux, Routing


def main():
    # Refuse the initial host namespace, including root's host network.
    if os.readlink('/proc/self/ns/net') == os.readlink('/proc/1/ns/net'):
        raise RuntimeError('Use sudo unshare --net; refusing the host network')
    linux = Linux()
    linux.run('ip', 'link', 'add', 'ens18', 'type', 'dummy')
    linux.run('ip', 'link', 'set', 'ens18', 'up')
    linux.run('ip', 'addr', 'add', '192.168.1.48/24', 'dev', 'ens18')
    linux.run('ip', 'route', 'add', 'default', 'via', '192.168.1.1', 'dev', 'ens18')
    linux.run('ip', 'link', 'add', 'ruter-v2-tun', 'type', 'dummy')
    linux.run('ip', 'link', 'set', 'ruter-v2-tun', 'up')
    net = {'LAN_IF':'ens18', 'LAN_IP':'192.168.1.48', 'LAN_CIDR':'192.168.1.0/24'}
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        route = Routing(net, linux, root / 'run', root / 'legacy', 'boot-1')
        route.legacy.write_text('0\n')
        route.start()
        assert route.inspect()['status'] == 'active', route.inspect()
        before = linux.snapshot()
        route.start()
        assert linux.snapshot() == before, 'Repeated start changed the kernel'
        linux.run('ip', 'link', 'del', 'ruter-v2-tun')
        assert route.inspect()['status'] == 'partial'
        linux.run('ip', 'link', 'add', 'ruter-v2-tun', 'type', 'dummy')
        linux.run('ip', 'link', 'set', 'ruter-v2-tun', 'up')
        route.start()
        assert route.inspect()['status'] == 'active'
        # Emulate kernel routing loss across reboot, keeping old state on disk.
        route.stop()
        route.legacy.write_text('0\n')
        rebooted = Routing(net, linux, root / 'run', route.legacy, 'boot-2')
        rebooted.start()
        assert rebooted.inspect()['status'] == 'active'
        linux.run('ip', 'route', 'add', '10.9.0.0/16', 'dev', 'ens18', 'table', '220', 'proto', 'static')
        assert rebooted.inspect()['status'] == 'conflict'
        rebooted.stop()
        rules, routes = linux.snapshot()
        assert any(r.get('dst') == '10.9.0.0/16' and r.get('table') == 220 for r in routes)
        assert not any(r.get('priority') in (12200, 12210) for r in rules)
    print('Real Linux namespace: stale state / repeat / TUN loss / recovery / foreign resource preservation PASS')


if __name__ == '__main__':
    main()
