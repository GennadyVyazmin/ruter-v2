"""Parse VLESS subscriptions and build sing-box-lx configuration.

Only formats explicitly understood here are accepted. In particular, a JSON
subscription is not silently interpreted as a native sing-box configuration.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import re
from urllib.parse import parse_qs, unquote, urlsplit


class SubscriptionError(ValueError):
    pass


def decode_subscription(body: str) -> list[str]:
    body = body.strip().lstrip("\ufeff")
    if not body:
        raise SubscriptionError("Подписка пуста")
    if body.startswith("{") or body.startswith("["):
        raise SubscriptionError("JSON-подписки пока не поддерживаются")
    if "vless://" not in body:
        try:
            compact = re.sub(r"\s+", "", body)
            body = base64.b64decode(compact + "=" * (-len(compact) % 4), validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise SubscriptionError("Подписка не содержит VLESS-ссылок") from exc
    links = [line.strip() for line in body.splitlines() if line.strip().startswith("vless://")]
    if not links:
        raise SubscriptionError("В подписке нет VLESS-ссылок")
    return links


def _first(query: dict[str, list[str]], key: str, default: str = "") -> str:
    return query.get(key, [default])[0]


def parse_vless(link: str, ordinal: int) -> dict:
    parsed = urlsplit(link)
    if parsed.scheme != "vless" or not parsed.username or not parsed.hostname or not parsed.port:
        raise SubscriptionError("Некорректная VLESS-ссылка")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if _first(query, "security") != "reality":
        raise SubscriptionError("Поддерживается только VLESS + REALITY")
    if _first(query, "encryption", "none") != "none":
        raise SubscriptionError("VLESS Encryption пока не поддерживается")
    transport = _first(query, "type", "tcp").lower()
    flow = _first(query, "flow")
    if flow not in ("", "xtls-rprx-vision"):
        raise SubscriptionError("Неизвестный flow")
    if transport != "tcp" and flow:
        raise SubscriptionError("Vision flow поддерживается только для TCP")
    if transport not in ("tcp", "xhttp", "ws", "grpc", "httpupgrade"):
        raise SubscriptionError(f"Транспорт {transport} пока не поддерживается")
    pbk = _first(query, "pbk")
    sni = _first(query, "sni")
    if not pbk or not sni:
        raise SubscriptionError("REALITY требует pbk и sni")
    tag = unquote(parsed.fragment).strip() or f"Server {ordinal}"
    tag = re.sub(r"[\x00-\x1f]", "", tag)[:80]
    outbound = {
        "type": "vless",
        "tag": tag,
        "server": parsed.hostname,
        "server_port": parsed.port,
        "uuid": parsed.username,
        "packet_encoding": "xudp",
        "tls": {
            "enabled": True,
            "server_name": sni,
            "utls": {"enabled": True, "fingerprint": _first(query, "fp", "chrome")},
            "reality": {"enabled": True, "public_key": pbk, "short_id": _first(query, "sid")},
        },
    }
    if flow:
        outbound["flow"] = flow
    if transport == "xhttp":
        extra = {}
        if _first(query, "extra"):
            try:
                extra = json.loads(_first(query, "extra"))
            except (ValueError, TypeError) as exc:
                raise SubscriptionError("Некорректный extra для XHTTP") from exc
            if not isinstance(extra, dict):
                raise SubscriptionError("Некорректный extra для XHTTP")
        transport_config = {
            "type": "xhttp",
            "path": _first(query, "path", "/"),
            "mode": _first(query, "mode", str(extra.get("mode", "stream-up"))),
        }
        if transport_config["mode"] not in ("stream-up", "packet-up", "stream-one", "auto"):
            raise SubscriptionError("Неизвестный режим XHTTP")
        host = _first(query, "host")
        if host:
            transport_config["host"] = [host]
        padding = _first(query, "x_padding_bytes", str(extra.get("xPaddingBytes", "")))
        if padding:
            transport_config["x_padding_bytes"] = padding
        outbound["transport"] = transport_config
    elif transport == "ws":
        ws = {"type": "ws", "path": _first(query, "path", "/")}
        if _first(query, "host"):
            ws["headers"] = {"Host": _first(query, "host")}
        outbound["transport"] = ws
    elif transport == "grpc":
        outbound["transport"] = {"type": "grpc", "service_name": _first(query, "serviceName")}
    elif transport == "httpupgrade":
        upgrade = {"type": "httpupgrade", "path": _first(query, "path", "/")}
        if _first(query, "host"):
            upgrade["host"] = _first(query, "host")
        outbound["transport"] = upgrade
    return outbound


def parse_subscription(body: str) -> tuple[list[dict], list[str]]:
    outbounds: list[dict] = []
    warnings: list[str] = []
    used: set[str] = set()
    for index, link in enumerate(decode_subscription(body), 1):
        try:
            outbound = parse_vless(link, index)
        except (SubscriptionError, ValueError) as exc:
            warnings.append(f"Ссылка {index} пропущена: {exc}")
            continue
        tag = outbound["tag"]
        if tag in used or tag in {"direct", "block", "auto", "select"}:
            tag = f"{tag} #{index}"
            outbound["tag"] = tag
        used.add(tag)
        outbounds.append(outbound)
    if not outbounds:
        raise SubscriptionError("Нет поддерживаемых серверов: " + "; ".join(warnings))
    return outbounds, warnings


def build_config(outbounds: list[dict], lan_ip: str, api_secret: str) -> dict:
    ipaddress.IPv4Address(lan_ip)
    tags = [outbound["tag"] for outbound in outbounds]
    return {
        "log": {"level": "info", "timestamp": True},
        "inbounds": [
            {"type": "tun", "tag": "ruter-v2-in", "interface_name": "ruter-v2-tun",
             "address": ["172.31.252.1/30"], "auto_route": False,
             "strict_route": False, "stack": "system"},
            {"type": "mixed", "tag": "ruter-v2-local", "listen": "127.0.0.1", "listen_port": 2180},
            {"type": "mixed", "tag": "ruter-v2-lan", "listen": lan_ip, "listen_port": 2181},
        ],
        "outbounds": outbounds + [
            {"type": "urltest", "tag": "auto", "outbounds": tags,
             "url": "https://www.gstatic.com/generate_204", "interval": "3m"},
            {"type": "selector", "tag": "select", "outbounds": ["auto"] + tags,
             "default": "auto"},
            {"type": "direct", "tag": "direct"},
            {"type": "block", "tag": "block"},
        ],
        "route": {"auto_detect_interface": True, "rules": [
            {"ip_version": 6, "outbound": "block"},
            {"ip_is_private": True, "outbound": "direct"},
        ], "final": "select"},
        "experimental": {
            "clash_api": {"external_controller": f"{lan_ip}:9190",
                          "external_ui": "/opt/ruter-v2/ui", "secret": api_secret},
            "cache_file": {"enabled": True, "path": "/var/lib/ruter-v2/cache.db"},
        },
    }


def config_json(outbounds: list[dict], lan_ip: str, api_secret: str) -> str:
    return json.dumps(build_config(outbounds, lan_ip, api_secret), ensure_ascii=False, indent=2) + "\n"
