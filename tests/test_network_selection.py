import unittest
from unittest.mock import patch

from ruter_v2.manager import confirm_network, select_lan_interface


class NetworkSelectionTests(unittest.TestCase):
    @patch("ruter_v2.manager.command_output", return_value='[{"dev":"ens18","gateway":"192.168.88.1"}]')
    def test_single_default_interface_is_selected(self, _output):
        self.assertEqual(select_lan_interface(None), "ens18")

    @patch("ruter_v2.manager.input", return_value="2")
    @patch("ruter_v2.manager.command_output", return_value='[{"dev":"ens18"},{"dev":"wlan0"}]')
    def test_multiple_interfaces_require_choice(self, _output, _input):
        self.assertEqual(select_lan_interface(None), "wlan0")

    @patch("ruter_v2.manager.input", return_value="")
    def test_empty_confirmation_cancels(self, _input):
        with self.assertRaisesRegex(RuntimeError, "отменена"):
            confirm_network("ens18", "192.168.88.40", "192.168.88.0/24")

    @patch("ruter_v2.manager.input", return_value="да")
    def test_yes_confirms(self, _input):
        confirm_network("ens18", "192.168.88.40", "192.168.88.0/24")
