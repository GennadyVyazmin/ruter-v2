"""Kernel-derived routing lifecycle; never flush tables or execute state files.

Protocol 242 labels new resources. /run state records forwarding provenance,
not activation truth. Legacy exact resources require the old state marker.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time

RUNTIME = Path('/run/ruter-v2-route')
ENV = Path('/etc/ruter-v2/network.env')
LEGACY = Path('/var/lib/ruter-v2/route.state')
TABLE, PROTOCOL = 220, '242'
TUN = 'ruter-v2-tun'


@contextlib.contextmanager
def routing_lock():
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Do not remove the directory or lock inode during service restarts.
    with (RUNTIME / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def network(path=ENV):
    values = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or key not in ('LAN_IF', 'LAN_IP', 'LAN_CIDR') or key in values:
            raise RuntimeError('Некорректный network.env; файл не исполнялся')
        values[key] = value
    if set(values) != {'LAN_IF', 'LAN_IP', 'LAN_CIDR'} or not re.fullmatch(r'[a-zA-Z0-9_.:-]+', values['LAN_IF']):
        raise RuntimeError('Неполные сетевые настройки')
    address = ipaddress.IPv4Address(values['LAN_IP'])
    if address not in ipaddress.IPv4Network(values['LAN_CIDR'], strict=True):
        raise RuntimeError('LAN-IP не входит в подсеть')
    return values


class Linux:
    def run(self, *args):
        result = subprocess.run(args, text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError(f"{' '.join(args[:5])}: {result.stderr.strip()}")
        return result.stdout

    def snapshot(self):
        # Absent table 220 is normal; 'all' avoids ip's exit 2 on a fresh host.
        return (json.loads(self.run('ip', '-N', '-j', '-4', 'rule', 'show')),
                json.loads(self.run('ip', '-N', '-j', '-4', 'route', 'show', 'table', 'all')))

    def ready(self, net):
        links = json.loads(self.run('ip', '-j', 'link', 'show'))
        addresses = json.loads(self.run('ip', '-j', '-4', 'addr', 'show'))
        return (any(x['ifname'] == TUN and 'UP' in x.get('flags', []) for x in links) and
                any(x['ifname'] == net['LAN_IF'] and 'UP' in x.get('flags', []) and
                    any(a.get('local') == net['LAN_IP'] for a in x.get('addr_info', [])) for x in addresses) and
                any(x.get('dev') == net['LAN_IF'] for x in
                    json.loads(self.run('ip', '-j', '-4', 'route', 'show', 'default'))))

    def forwarding(self):
        return int(Path('/proc/sys/net/ipv4/ip_forward').read_text().strip())

    def set_forwarding(self, value):
        self.run('sysctl', '-qw', f'net.ipv4.ip_forward={value}')


class Routing:
    def __init__(self, net, linux=None, runtime=RUNTIME, legacy=LEGACY, boot=None):
        self.net = net
        self.linux = linux or Linux()
        self.runtime, self.legacy = runtime, legacy
        self.boot = boot or Path('/proc/sys/kernel/random/boot_id').read_text().strip()

    def state(self):
        try:
            value = json.loads((self.runtime / 'state.json').read_text())
            return value if value.get('boot') == self.boot and value.get('network') == self.net else {}
        except (OSError, ValueError, AttributeError):
            return {}

    def save(self, state):
        self.runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.state-', dir=self.runtime)
        try:
            with os.fdopen(fd, 'w') as file:
                json.dump(state, file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, self.runtime / 'state.json')
        finally:
            Path(name).unlink(missing_ok=True)

    def legacy_allowed(self):
        # Marker is migration evidence, never proof of activity or pre-boot forwarding.
        try:
            old = self.legacy.read_text().strip() in ('0', '1')
        except OSError:
            old = False
        return old or self.state().get('legacy_adopted', False)

    def specs(self):
        return [
            ('route', {'dst': self.net['LAN_CIDR'], 'dev': self.net['LAN_IF'], 'scope': 'link'}),
            ('route', {'dst': 'default', 'dev': TUN, 'scope': 'link'}),
            ('rule', {'priority': 12200, 'src': self.net['LAN_IP'], 'table': 'main'}),
            ('rule', {'priority': 12210, 'src': self.net['LAN_CIDR'], 'table': TABLE}),
        ]

    @staticmethod
    def equal(kind, actual, spec):
        # Reject all selectors not emitted by us (fwmark, gateway, metric, iif...).
        allowed = set(spec) | {'protocol', 'table', 'flags', 'type'}
        if kind == 'rule':
            allowed.add('srclen')
        if (set(actual) - allowed or set(actual.get('flags', [])) - {'linkdown'} or
                actual.get('type', 'unicast') != 'unicast'):
            return False
        def normalized(key, value):
            if key == 'src' and value:
                return str(ipaddress.ip_network(value, strict=False))
            if key == 'table':
                return '254' if str(value) == 'main' else str(value)
            if key == 'scope':
                return '253' if str(value) == 'link' else str(value)
            return str(value)
        values = dict(actual)
        if kind == 'rule' and 'srclen' in values:
            values['src'] = str(values['src']).split('/')[0] + '/' + str(values['srclen'])
        return all(normalized(k, values.get(k)) == normalized(k, v) for k, v in spec.items())

    def inspect(self):
        rules, routes = self.linux.snapshot()
        resources = {
            'route': [r for r in routes if str(r.get('table', 'main')) == str(TABLE)],
            'rule': [r for r in rules if r.get('priority') in (12200, 12210)],
        }
        owned, conflicts = [], []
        legacy = self.legacy_allowed()
        for kind, entries in resources.items():
            for item in entries:
                shape = next((spec for k, spec in self.specs() if k == kind and self.equal(kind, item, spec)), None)
                protocol = str(item.get('protocol', 'boot'))
                if shape and (protocol == PROTOCOL or legacy and protocol in ('boot', '3')):
                    owned.append((kind, item, shape))
                else:
                    conflicts.append((kind, item))
        present = [any(k == kind and shape == spec for k, _, shape in owned) for kind, spec in self.specs()]
        ready = self.linux.ready(self.net)
        active = all(present) and len(owned) == 4 and not conflicts and ready and self.linux.forwarding() == 1
        return {'status': 'active' if active else 'conflict' if conflicts else 'partial' if owned else 'stopped',
                'ready': ready, 'present': present, 'owned': owned, 'conflicts': conflicts}

    def change(self, action, kind, spec, protocol):
        if kind == 'route':
            args = ['ip', '-4', 'route', action, spec['dst'], 'dev', spec['dev'], 'scope', 'link',
                    'table', str(TABLE), 'proto', protocol]
        else:
            args = ['ip', '-4', 'rule', action, 'pref', str(spec['priority']), 'from', spec['src'],
                    'lookup', str(spec['table']), 'protocol', protocol]
        self.linux.run(*args)

    def start(self, wait=0):
        deadline = time.monotonic() + wait
        while not self.linux.ready(self.net):
            if time.monotonic() >= deadline:
                raise RuntimeError('TUN/LAN/IP или основной шлюз ещё не готовы')
            time.sleep(.2)
        actual = self.inspect()
        if actual['conflicts']:
            raise RuntimeError('Чужие ресурсы в таблице 220/приоритетах 12200,12210; сеть не изменена')
        state = self.state()
        forward = self.linux.forwarding()
        if actual['status'] == 'active' and state:
            return 'active'
        if not state:
            state = {'boot': self.boot, 'network': self.net,
                     'original_forward': forward if not actual['owned'] else None,
                     'legacy_adopted': self.legacy_allowed()}
        # Persist provenance before mutation, including abrupt interruption.
        self.save(state)
        added = []
        try:
            for (kind, spec), present in zip(self.specs(), actual['present']):
                if not present:
                    self.change('add', kind, spec, PROTOCOL)
                    added.append((kind, spec))
            if forward != 1:
                self.linux.set_forwarding(1)
            seen = set()
            for kind, item, spec in actual['owned']:
                key = (kind, json.dumps(spec, sort_keys=True))
                if key in seen:
                    self.change('del', kind, spec, str(item.get('protocol', 'boot')))
                seen.add(key)
            if self.inspect()['status'] != 'active':
                raise RuntimeError('Проверка маршрутов после применения не прошла: ' + json.dumps(self.inspect(), ensure_ascii=False))
        except Exception:
            errors = []
            for kind, spec in reversed(added):
                try:
                    current = self.inspect()
                    if any(k == kind and shape == spec and str(item.get('protocol')) == PROTOCOL
                           for k, item, shape in current['owned']):
                        self.change('del', kind, spec, PROTOCOL)
                except Exception as exc:
                    errors.append(str(exc))
            try:
                if not errors and self.linux.forwarding() != forward:
                    self.linux.set_forwarding(forward)
            except Exception as exc:
                errors.append(str(exc))
            if errors:
                print('Неполный откат: ' + '; '.join(errors), file=sys.stderr)
            raise
        return actual['status']

    def stop(self):
        actual = self.inspect()
        # Remove LAN steering first; no flush, no delete-by-priority alone.
        for kind, item, spec in sorted(actual['owned'], key=lambda x: x[2].get('priority', 0), reverse=True):
            self.change('del', kind, spec, str(item.get('protocol', 'boot')))
        state = self.state()
        if state.get('original_forward') == 0 and not actual['conflicts']:
            if self.linux.forwarding() == 1:
                self.linux.set_forwarding(0)
        (self.runtime / 'state.json').unlink(missing_ok=True)
        self.legacy.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('start', 'stop', 'status', 'watch'))
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('Запустите через sudo')
    routing = Routing(network())
    if args.action == 'watch':
        quitting = False
        def quit_handler(*_):
            nonlocal quitting
            quitting = True
        signal.signal(signal.SIGTERM, quit_handler)
        signal.signal(signal.SIGINT, quit_handler)
        last_error = None
        while not quitting:
            try:
                with routing_lock():
                    if not routing.linux.ready(routing.net):
                        routing.stop()
                        raise RuntimeError('Ожидание TUN/LAN/IP; собственные правила убраны')
                    routing.start()
                last_error = None
            except (OSError, ValueError, RuntimeError) as exc:
                if str(exc) != last_error:
                    print(str(exc), file=sys.stderr, flush=True)
                    last_error = str(exc)
            for _ in range(10):
                if quitting:
                    break
                time.sleep(.2)
        return 0
    with routing_lock():
        if args.action == 'status':
            actual = routing.inspect()
            print(json.dumps({k: actual[k] for k in ('status', 'ready', 'present', 'conflicts')}))
            return 0 if actual['status'] == 'active' else 1
        if args.action == 'start':
            routing.start(wait=15)
        else:
            routing.stop()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
