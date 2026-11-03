import copy
import json
from pathlib import Path
import tempfile
import unittest

from ruter_v2.routing import Routing, network

NET = {'LAN_IF': 'ens18', 'LAN_IP': '192.168.1.48', 'LAN_CIDR': '192.168.1.0/24'}


class FakeLinux:
    def __init__(self):
        self.rules, self.routes, self.commands = [], [], []
        self.forward, self.is_ready, self.fail_at = 0, True, None

    def snapshot(self):
        return copy.deepcopy((self.rules, self.routes))

    def ready(self, net):
        return self.is_ready

    def forwarding(self):
        return self.forward

    def set_forwarding(self, value):
        self.forward = value

    def run(self, *args):
        self.commands.append(args)
        if self.fail_at == len(self.commands):
            raise RuntimeError('Injected failure')
        kind, action = args[2:4]
        if kind == 'route':
            item = {'dst': args[4], 'dev': args[6], 'scope': 'link', 'table': 220, 'protocol': args[-1]}
            target = self.routes
        else:
            item = {'priority': int(args[5]), 'src': args[7], 'table': args[9], 'protocol': args[-1]}
            target = self.rules
        if action == 'add':
            target.append(item)
        else:
            match = next(x for x in target if Routing.equal(kind, x, {k:v for k,v in item.items() if k != 'protocol'})
                         and str(x.get('protocol', '0' if kind == 'rule' else 'boot')) == item['protocol'])
            target.remove(match)


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.kernel = FakeLinux()
        self.route = Routing(NET, self.kernel, self.path / 'run', self.path / 'old.state', 'boot-A')

    def test_repeated_start_is_noop_and_stop_restores_forwarding(self):
        self.route.start()
        before = copy.deepcopy(self.kernel.commands)
        self.route.start()
        self.assertEqual(before, self.kernel.commands)
        self.assertEqual(self.route.inspect()['status'], 'active')
        self.route.stop()
        self.route.stop()
        self.assertEqual(self.kernel.forward, 0)
        self.assertEqual(self.route.inspect()['status'], 'stopped')

    def test_reboot_and_stale_legacy_state(self):
        self.route.legacy.write_text('0\n')
        self.route.start()
        self.kernel.rules.clear()
        self.kernel.routes.clear()
        self.kernel.forward = 0
        rebooted = Routing(NET, self.kernel, self.route.runtime, self.route.legacy, 'boot-B')
        self.assertEqual(rebooted.state(), {})
        rebooted.start()
        self.assertEqual(rebooted.inspect()['status'], 'active')

    def test_partial_without_state_repairs_missing_only(self):
        self.route.start()
        (self.route.runtime / 'state.json').unlink()
        self.kernel.routes.pop()
        self.assertEqual(self.route.inspect()['status'], 'partial')
        before = len(self.kernel.commands)
        self.route.start()
        self.assertEqual(len(self.kernel.commands) - before, 1)
        self.route.stop()
        # No provenance: do not guess the original global forwarding setting.
        self.assertEqual(self.kernel.forward, 1)

    def test_legacy_active_adoption_never_changes_routes(self):
        self.route.start()
        (self.route.runtime / 'state.json').unlink()
        self.route.legacy.write_text('0\n')
        for item in self.kernel.rules + self.kernel.routes:
            item.pop('protocol')
        before = len(self.kernel.commands)
        self.route.start()
        self.assertEqual(before, len(self.kernel.commands))
        self.assertEqual(self.route.inspect()['status'], 'active')
        self.route.stop()
        self.assertFalse(self.kernel.routes or self.kernel.rules)

    def test_unmarked_legacy_shape_is_not_owned(self):
        self.kernel.rules.append({'priority': 12210, 'src': NET['LAN_CIDR'], 'table': 220})
        with self.assertRaisesRegex(RuntimeError, 'Чужие'):
            self.route.start()
        self.route.stop()
        self.assertEqual(len(self.kernel.rules), 1)

    def test_foreign_table_route_preserved_start_and_stop(self):
        self.route.start()
        foreign = {'dst': '10.5.0.0/16', 'dev': 'foreign0', 'table': 220, 'protocol': 'static'}
        self.kernel.routes.append(foreign)
        with self.assertRaises(RuntimeError):
            self.route.start()
        self.route.stop()
        self.assertEqual(self.kernel.routes, [foreign])
        self.assertEqual(self.kernel.forward, 1)

    def test_same_priority_foreign_selector_is_preserved(self):
        self.route.start()
        self.kernel.rules.append({'priority': 12210, 'src': '10.0.0.0/8', 'table': 221, 'protocol': '242'})
        self.route.stop()
        self.assertEqual(len(self.kernel.rules), 1)
        self.assertEqual(self.kernel.rules[0]['table'], 221)

    def test_failure_each_step_rolls_back_only_new_resources(self):
        for fail_at in range(1, 5):
            with self.subTest(step=fail_at):
                self.kernel = FakeLinux()
                self.route.linux = self.kernel
                self.kernel.fail_at = fail_at
                with self.assertRaisesRegex(RuntimeError, 'Injected'):
                    self.route.start()
                self.assertFalse(self.kernel.rules or self.kernel.routes)
                self.assertEqual(self.kernel.forward, 0)

    def test_failure_preserves_existing_partial_resources(self):
        self.route.start()
        self.kernel.rules.clear()
        before = copy.deepcopy(self.kernel.routes)
        self.kernel.fail_at = len(self.kernel.commands) + 2
        with self.assertRaises(RuntimeError):
            self.route.start()
        self.assertEqual(self.kernel.routes, before)
        self.assertEqual(self.kernel.rules, [])

    def test_duplicate_owned_rules_converge(self):
        self.route.start()
        self.kernel.rules.append(copy.deepcopy(self.kernel.rules[-1]))
        self.route.start()
        self.assertEqual(len(self.kernel.rules), 2)
        self.assertEqual(self.route.inspect()['status'], 'active')

    def test_core_tun_loss_repair_and_forwarding_drift(self):
        self.route.start()
        self.kernel.is_ready = False
        self.kernel.routes = [x for x in self.kernel.routes if x['dev'] != 'ruter-v2-tun']
        self.assertNotEqual(self.route.inspect()['status'], 'active')
        self.route.stop()
        self.kernel.is_ready = True
        self.route.start()
        self.kernel.forward = 0
        self.route.start()
        self.assertEqual(self.route.inspect()['status'], 'active')

    def test_state_is_not_activation_truth(self):
        self.route.save({'boot':'boot-A', 'network': NET, 'original_forward':0})
        self.assertEqual(self.route.inspect()['status'], 'stopped')
        self.route.start()
        self.assertEqual(self.route.inspect()['status'], 'active')

    def test_env_never_executes_shell(self):
        env = self.path / 'network.env'
        env.write_text('LAN_IF=$(touch /tmp/not-run)\nLAN_IP=192.168.1.48\nLAN_CIDR=192.168.1.0/24\n')
        with self.assertRaises(RuntimeError):
            network(env)

    def test_numeric_iproute2_json_and_separate_source_prefix(self):
        self.route.start()
        for item in self.kernel.routes:
            item['scope'] = '253'
            item['table'] = '220'
        rule = self.kernel.rules[-1]
        rule['src'], rule['srclen'], rule['table'] = '192.168.1.0', 24, '220'
        self.assertEqual(self.route.inspect()['status'], 'active')
