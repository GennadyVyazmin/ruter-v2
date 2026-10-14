import base64
import unittest

from ruter_v2.manager import MENU
from ruter_v2.subscription import SubscriptionError, build_config, parse_subscription


TCP = ("vless://12345678-1234-1234-1234-123456789abc@example.com:443"
       "?encryption=none&security=reality&pbk=testpublickey&sni=example.org"
       "&sid=aabb&fp=chrome&flow=xtls-rprx-vision&type=tcp#TCP")
XHTTP = ("vless://12345678-1234-1234-1234-123456789abc@example.net:8443"
         "?encryption=none&security=reality&pbk=testpublickey&sni=example.org"
         "&sid=aabb&fp=chrome&type=xhttp&mode=stream-up&path=%2Fapi%2Fstream"
         "&x_padding_bytes=100-1000#XHTTP")


class SubscriptionTests(unittest.TestCase):
    def test_plain_and_base64(self):
        body = TCP + "\n" + XHTTP
        plain, warnings = parse_subscription(body)
        encoded, _ = parse_subscription(base64.b64encode(body.encode()).decode())
        self.assertEqual(plain, encoded)
        self.assertFalse(warnings)
        self.assertEqual(plain[0]["flow"], "xtls-rprx-vision")
        self.assertEqual(plain[1]["transport"]["mode"], "stream-up")
        self.assertEqual(plain[1]["packet_encoding"], "xudp")

    def test_invalid_link_is_skipped(self):
        servers, warnings = parse_subscription(XHTTP + "\n" + TCP.replace("security=reality", "security=tls"))
        self.assertEqual(len(servers), 1)
        self.assertEqual(len(warnings), 1)

    def test_pqv_does_not_spam_warnings(self):
        servers, warnings = parse_subscription(XHTTP.replace("&type=xhttp", "&pqv=test-value&type=xhttp"))
        self.assertEqual(len(servers), 1)
        self.assertEqual(warnings, [])

    def test_json_rejected_explicitly(self):
        with self.assertRaisesRegex(SubscriptionError, "JSON"):
            parse_subscription('{"outbounds": []}')

    def test_xhttp_extra_and_legacy_transports(self):
        with_extra = XHTTP.replace("&x_padding_bytes=100-1000", "&extra=%7B%22xPaddingBytes%22%3A%22100-1000%22%7D")
        servers, _ = parse_subscription(with_extra)
        self.assertEqual(servers[0]["transport"]["x_padding_bytes"], "100-1000")
        ws, _ = parse_subscription(XHTTP.replace("type=xhttp", "type=ws"))
        grpc, _ = parse_subscription(XHTTP.replace("type=xhttp", "type=grpc"))
        upgrade, _ = parse_subscription(XHTTP.replace("type=xhttp", "type=httpupgrade"))
        self.assertEqual([x[0]["transport"]["type"] for x in (ws, grpc, upgrade)],
                         ["ws", "grpc", "httpupgrade"])

    def test_config_and_menu(self):
        servers, _ = parse_subscription(TCP + "\n" + XHTTP)
        config = build_config(servers, "192.168.88.40", "a-secret")
        self.assertEqual(config["route"]["final"], "select")
        self.assertEqual(config["experimental"]["clash_api"]["external_controller"], "192.168.88.40:9190")
        self.assertEqual([item[2] for item in MENU[:3]], ["install", "update", "change-source"])
        self.assertEqual([item[0] for item in MENU], [str(number) for number in range(1, 10)])
        self.assertEqual(MENU[-1][2], "uninstall")


if __name__ == "__main__":
    unittest.main()
