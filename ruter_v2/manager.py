#!/usr/bin/env python3
"""Ruter v2 installer, subscription manager and diagnostics for Debian.

Run from the checked-out repository with sudo. Nothing runs automatically on
import or checkout. Installation intentionally refuses an active lab gateway.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import urllib.request

from ruter_v2.subscription import SubscriptionError, config_json, parse_subscription


ROOT = Path(__file__).resolve().parent.parent
ETC = Path("/etc/ruter-v2")
OPT = Path("/opt/ruter-v2")
VAR = Path("/var/lib/ruter-v2")
CORE_VERSION = "1.14.3-lx.20"
CORE_ARCHIVE_SHA256 = "9df2039fe290e56634f5f899f2337f6ce8b5cee22023c3a75e79c8e631c309fe"
CORE_BINARY_SHA256 = "2ee0653b7a6c36f3e08be72e7eb902d2713b8e893b87db5e3e8e538ed04526ba"
CORE_URL = ("https://github.com/Leadaxe/sing-box-lx/releases/download/"
            "v1.14.3-lx.20/sing-box-1.14.3-lx.20-linux-amd64.tar.gz")
UI_COMMIT = "28a9589f6239bbafc24e87bbf5e5b4997fe42e59"


def run(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=check, text=True, capture_output=capture)


def require_root() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("Запустите через sudo")
    if sys.platform != "linux":
        raise RuntimeError("Ruter v2 поддерживает только Linux")


def command_output(*args: str) -> str:
    return run(*args, capture=True).stdout


def active_lab() -> bool:
    return run("systemctl", "is-active", "--quiet", "ruter-xhttp-lab.service", check=False).returncode == 0


def preflight() -> None:
    if active_lab():
        raise RuntimeError("Временный ruter-xhttp-lab работает. Остановите его штатным stop_xhttp_gateway.sh перед установкой v2")
    if service_active("sing-box.service"):
        raise RuntimeError("Существующий sing-box.service работает; установка второго шлюза на этой машине запрещена")
    if Path("/var/lib/ruter-v2/route.state").exists():
        raise RuntimeError("Правила Ruter v2 уже активны. Сначала остановите шлюз")
    for name in ("ruter", "ruter-v2"):
        launcher = Path("/usr/local/bin") / name
        if launcher.exists() or launcher.is_symlink():
            raise RuntimeError(f"{launcher} уже занят; установка не будет его перезаписывать")
    distro = platform.freedesktop_os_release().get("ID")
    if distro not in ("debian", "ubuntu"):
        raise RuntimeError("Поддерживаются Debian и Ubuntu с systemd")
    if os.uname().machine != "x86_64":
        raise RuntimeError("Закреплённое ядро рассчитано на x86_64")
    if not Path("/run/systemd/system").is_dir():
        raise RuntimeError("Не найдена работающая systemd; установка служб невозможна")
    rules = command_output("ip", "-4", "rule", "show")
    # `ip route show table 220` exits with code 2 when the table does not yet
    # exist. Inspect all tables instead: a fresh installation must accept that.
    routes = json.loads(command_output("ip", "-j", "-4", "route", "show", "table", "all"))
    if re.search(r"^(12200|12210):", rules, re.M) or any(str(route.get("table")) == "220" for route in routes):
        raise RuntimeError("Правила/таблица 220 уже заняты; установка остановлена")
    listeners = command_output("ss", "-H", "-lnt")
    for port in (2180, 2181, 9190):
        if re.search(rf":{port}\s", listeners):
            raise RuntimeError(f"Порт {port} уже занят; установка остановлена")


def network_info(interface: str) -> tuple[str, str]:
    if not re.fullmatch(r"[a-zA-Z0-9_.:-]+", interface):
        raise ValueError("Некорректное имя интерфейса")
    addresses = json.loads(command_output("ip", "-j", "-4", "addr", "show", "dev", interface))
    if len(addresses) != 1:
        raise RuntimeError("Сетевой интерфейс не найден")
    entries = [x for x in addresses[0].get("addr_info", []) if x.get("family") == "inet" and x.get("scope") == "global"]
    if len(entries) != 1:
        raise RuntimeError("На интерфейсе должен быть один основной IPv4-адрес")
    entry = entries[0]
    lan_ip = str(ipaddress.IPv4Address(entry["local"]))
    lan_cidr = str(ipaddress.ip_interface(f"{lan_ip}/{entry['prefixlen']}").network)
    if not any(x.get("dev") == interface for x in json.loads(command_output("ip", "-j", "-4", "route", "show", "default"))):
        raise RuntimeError("На выбранном интерфейсе нет основного шлюза")
    return lan_ip, lan_cidr


def select_lan_interface(explicit: str | None) -> str:
    if explicit:
        return explicit
    routes = json.loads(command_output("ip", "-j", "-4", "route", "show", "default"))
    candidates = list(dict.fromkeys(route["dev"] for route in routes if route.get("dev")))
    if not candidates:
        raise RuntimeError("Не найден сетевой интерфейс с IPv4-шлюзом")
    if len(candidates) == 1:
        return candidates[0]
    print("Найдено несколько подключений с IPv4-шлюзом:")
    for number, name in enumerate(candidates, 1):
        print(f"  {number}. {name}")
    choice = input("Выберите номер сетевого подключения: ").strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(candidates):
        raise RuntimeError("Установка отменена: сетевое подключение не выбрано")
    return candidates[int(choice) - 1]


def confirm_network(interface: str, lan_ip: str, lan_cidr: str) -> None:
    print(f"\nНайдены сетевые настройки:\n  Подключение: {interface}\n  IP-адрес: {lan_ip}\n  Подсеть: {lan_cidr}")
    if input("Использовать эти настройки для Ruter v2? [д/Н]: ").strip().lower() not in ("д", "да", "y", "yes"):
        raise RuntimeError("Установка отменена; сетевые настройки не изменены")


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".ruter-v2-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def source_body(source: str) -> str:
    if source.startswith("https://"):
        request = urllib.request.Request(source, headers={"User-Agent": "Ruter-v2/0.1"})
        with urllib.request.urlopen(request, timeout=20) as response:
            data = response.read(4_000_001)
        if len(data) > 4_000_000:
            raise SubscriptionError("Подписка слишком велика")
        return data.decode("utf-8-sig")
    if source.startswith("vless://") or "\nvless://" in source:
        return source
    raise SubscriptionError("Ожидается HTTPS-подписка или VLESS-ссылка")


def checked_config(source: str, lan_ip: str, secret: str, binary: Path) -> tuple[str, list[str], int]:
    servers, warnings = parse_subscription(source_body(source))
    candidate = config_json(servers, lan_ip, secret)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", prefix="ruter-v2-",
                                     dir=VAR, delete=False, encoding="utf-8") as stream:
        stream.write(candidate)
        temp = Path(stream.name)
    try:
        os.chmod(temp, 0o600)
        result = run(str(binary), "check", "-c", str(temp), check=False, capture=True)
        if result.returncode:
            raise RuntimeError("Ядро отклонило конфигурацию: " + (result.stderr or result.stdout)[-600:])
    finally:
        temp.unlink(missing_ok=True)
    return candidate, warnings, len(servers)


def sha256(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def install_core(local_binary: str | None) -> Path:
    destination = OPT / "bin/sing-box"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if local_binary:
        source = Path(local_binary).resolve(strict=True)
        if sha256(source) != CORE_BINARY_SHA256:
            raise RuntimeError("Хеш выбранного ядра не совпадает с проверенным sing-box-lx")
        shutil.copy2(source, destination)
        license_file = source.parent / "LICENSE"
        if license_file.is_file():
            (OPT / "licenses").mkdir(exist_ok=True)
            shutil.copy2(license_file, OPT / "licenses/sing-box-lx-LICENSE")
    else:
        import tarfile
        with tempfile.TemporaryDirectory(prefix=".core-download-", dir=VAR) as temp:
            archive = Path(temp) / "core.tar.gz"
            with urllib.request.urlopen(CORE_URL, timeout=60) as response, archive.open("wb") as out:
                shutil.copyfileobj(response, out)
            if sha256(archive) != CORE_ARCHIVE_SHA256:
                raise RuntimeError("Хеш архива ядра не совпадает")
            with tarfile.open(archive, "r:gz") as tar:
                members = [m for m in tar.getmembers() if m.name.endswith("/sing-box") and m.isfile()]
                if len(members) != 1:
                    raise RuntimeError("В архиве ядра не найден ожидаемый исполняемый файл")
                with tar.extractfile(members[0]) as source, destination.open("wb") as out:
                    shutil.copyfileobj(source, out)
                licenses = [m for m in tar.getmembers() if m.name.endswith("/LICENSE") and m.isfile()]
                if len(licenses) == 1:
                    (OPT / "licenses").mkdir(exist_ok=True)
                    with tar.extractfile(licenses[0]) as source, (OPT / "licenses/sing-box-lx-LICENSE").open("wb") as out:
                        shutil.copyfileobj(source, out)
    if sha256(destination) != CORE_BINARY_SHA256:
        destination.unlink(missing_ok=True)
        raise RuntimeError("Хеш ядра после установки не совпадает")
    destination.chmod(0o755)
    return destination


def install_ui() -> None:
    ui = OPT / "ui"
    if ui.exists():
        return
    temp = OPT / ".ui-new"
    if temp.exists():
        raise RuntimeError("Осталась незавершённая установка интерфейса")
    try:
        run("git", "clone", "--depth", "1", "--branch", "gh-pages",
            "https://github.com/MetaCubeX/metacubexd.git", str(temp))
        if command_output("git", "-C", str(temp), "rev-parse", "HEAD").strip() != UI_COMMIT:
            run("git", "-C", str(temp), "fetch", "--depth", "1", "origin", UI_COMMIT)
            run("git", "-C", str(temp), "checkout", "--detach", UI_COMMIT)
        if command_output("git", "-C", str(temp), "rev-parse", "HEAD").strip() != UI_COMMIT:
            raise RuntimeError("Версия MetaCubeXD не совпадает с закреплённой")
        temp.rename(ui)
    finally:
        if temp.exists():
            shutil.rmtree(temp)


def service_active(name: str) -> bool:
    return run("systemctl", "is-active", "--quiet", name, check=False).returncode == 0


def install(interface: str | None, local_binary: str | None) -> None:
    require_root()
    preflight()
    required = ("git", "curl", "ip", "ss", "python3", "sysctl", "systemctl")
    missing = [name for name in required if shutil.which(name) is None]
    if missing or not Path("/etc/ssl/certs/ca-certificates.crt").is_file():
        raise RuntimeError("Не готовы зависимости Debian. Сначала запустите bootstrap.sh")
    if (ETC / "config.json").exists():
        raise RuntimeError("Ruter v2 уже установлен. Для новой ссылки выберите пункт 3")
    interface = select_lan_interface(interface)
    lan_ip, lan_cidr = network_info(interface)
    confirm_network(interface, lan_ip, lan_cidr)
    source = input("HTTPS-ссылка подписки или одна VLESS-ссылка: ").strip()
    if not source:
        raise RuntimeError("Источник подписки не указан")
    print("Зависимости готовы. Устанавливаются только отдельные файлы и службы Ruter v2; маршруты пока не включаются.")
    ETC.mkdir(mode=0o700, parents=True, exist_ok=True)
    VAR.mkdir(mode=0o700, parents=True, exist_ok=True)
    OPT.mkdir(mode=0o755, parents=True, exist_ok=True)
    binary = install_core(local_binary)
    install_ui()
    secret = secrets.token_urlsafe(32)
    config, warnings, count = checked_config(source, lan_ip, secret, binary)
    shutil.copy2(ROOT / "ruter_v2/route.sh", OPT / "route.sh")
    (OPT / "route.sh").chmod(0o755)
    package = OPT / "ruter_v2"
    package.mkdir(mode=0o755, exist_ok=True)
    for filename in ("__init__.py", "manager.py", "subscription.py"):
        shutil.copy2(ROOT / "ruter_v2" / filename, package / filename)
    shutil.copy2(ROOT / "ruter-v2", OPT / "ruter-v2")
    (OPT / "ruter-v2").chmod(0o755)
    shutil.copy2(ROOT / "ruter", OPT / "ruter")
    (OPT / "ruter").chmod(0o755)
    atomic_write(ETC / "network.env", f"LAN_IF={interface}\nLAN_IP={lan_ip}\nLAN_CIDR={lan_cidr}\n")
    atomic_write(ETC / "source.txt", source + "\n")
    atomic_write(ETC / "api-secret.txt", secret + "\n")
    atomic_write(ETC / "config.json", config)
    for name in ("ruter-v2-core.service", "ruter-v2-route.service"):
        shutil.copy2(ROOT / "systemd" / name, Path("/etc/systemd/system") / name)
    Path("/usr/local/bin/ruter-v2").symlink_to(OPT / "ruter-v2")
    Path("/usr/local/bin/ruter").symlink_to(OPT / "ruter")
    run("systemctl", "daemon-reload")
    print(f"Установлено {count} серверов; службы не запущены.")
    for warning in warnings:
        print("Предупреждение:", warning)
    print(f"MetaCubeXD после запуска: http://{lan_ip}:9190/ui/ (ключ в {ETC / 'api-secret.txt'})")
    print("Для главного меню введите ruter; для запуска выберите пункт «Запустить шлюз».")


def update_subscription() -> None:
    require_root()
    source_file = ETC / "source.txt"
    if not source_file.exists():
        raise RuntimeError("Сначала установите Ruter v2")
    source = source_file.read_text(encoding="utf-8").strip()
    lan_ip = re.search(r"^LAN_IP=(.+)$", (ETC / "network.env").read_text(), re.M).group(1)
    secret = (ETC / "api-secret.txt").read_text().strip()
    config, warnings, count = checked_config(source, lan_ip, secret, OPT / "bin/sing-box")
    old = (ETC / "config.json").read_text(encoding="utf-8")
    was_running = service_active("ruter-v2-core.service")
    atomic_write(ETC / "config.json", config)
    try:
        if was_running:
            run("systemctl", "restart", "ruter-v2-core.service")
            run("systemctl", "start", "ruter-v2-route.service")
    except subprocess.CalledProcessError:
        atomic_write(ETC / "config.json", old)
        if was_running:
            run("systemctl", "restart", "ruter-v2-core.service", check=False)
            run("systemctl", "start", "ruter-v2-route.service", check=False)
        raise RuntimeError("Новый конфиг не запустился; прежний восстановлен")
    print(f"Подписка обновлена из сохранённого источника: {count} серверов")
    for warning in warnings:
        print("Предупреждение:", warning)


def change_source() -> None:
    require_root()
    if not (ETC / "source.txt").exists():
        raise RuntimeError("Сначала установите Ruter v2")
    new_source = input("Вставьте новую HTTPS-ссылку подписки или VLESS-ссылку: ").strip()
    if not new_source:
        raise RuntimeError("Источник пуст")
    lan_ip = re.search(r"^LAN_IP=(.+)$", (ETC / "network.env").read_text(), re.M).group(1)
    secret = (ETC / "api-secret.txt").read_text().strip()
    checked_config(new_source, lan_ip, secret, OPT / "bin/sing-box")
    old_source = (ETC / "source.txt").read_text()
    atomic_write(ETC / "source.txt", new_source + "\n")
    try:
        update_subscription()
    except Exception:
        atomic_write(ETC / "source.txt", old_source)
        raise


def start() -> None:
    require_root()
    if active_lab():
        raise RuntimeError("Временный тестовый шлюз ещё работает")
    if service_active("sing-box.service"):
        raise RuntimeError("Другой sing-box.service работает; второй шлюз не запущен")
    if not (ETC / "config.json").exists():
        raise RuntimeError("Сначала установите Ruter v2")
    run("systemctl", "start", "ruter-v2-core.service")
    try:
        run("systemctl", "start", "ruter-v2-route.service")
    except subprocess.CalledProcessError:
        run("systemctl", "stop", "ruter-v2-core.service", check=False)
        raise
    print("Шлюз Ruter v2 работает")


def stop() -> None:
    require_root()
    run("systemctl", "stop", "ruter-v2-route.service", check=False)
    run("systemctl", "stop", "ruter-v2-core.service", check=False)
    print("Шлюз Ruter v2 остановлен")


def status() -> None:
    print("Ядро:", "работает" if service_active("ruter-v2-core.service") else "остановлено")
    print("Маршруты:", "работают" if service_active("ruter-v2-route.service") else "остановлены")
    enabled = run("systemctl", "is-enabled", "--quiet", "ruter-v2-route.service", check=False).returncode == 0
    print("Автозапуск:", "включён" if enabled else "выключен")
    if (ETC / "config.json").exists():
        config = json.loads((ETC / "config.json").read_text())
        print("Серверов:", sum(x.get("type") == "vless" for x in config.get("outbounds", [])))
        print("Интерфейс:", "ruter-v2-tun")
    print("Временный тестовый шлюз:", "работает" if active_lab() else "остановлен")


def diagnostics() -> None:
    status()
    print("\nПоследние события ядра:")
    run("journalctl", "-u", "ruter-v2-core.service", "-n", "40", "--no-pager", check=False)
    print("\nПоследние события маршрутов:")
    run("journalctl", "-u", "ruter-v2-route.service", "-n", "20", "--no-pager", check=False)


def autostart() -> None:
    require_root()
    if not (ETC / "config.json").exists():
        raise RuntimeError("Сначала установите Ruter v2")
    enabled = run("systemctl", "is-enabled", "--quiet", "ruter-v2-route.service", check=False).returncode == 0
    if enabled:
        run("systemctl", "disable", "ruter-v2-route.service")
        print("Автозапуск выключен; работающий шлюз не остановлен")
    else:
        run("systemctl", "enable", "ruter-v2-route.service")
        print("Автозапуск включён; шлюз запустится при следующей загрузке")


def uninstall() -> None:
    require_root()
    print("Удаление остановит только Ruter v2. Службы старого Ruter не затрагиваются.")
    if input("Введите УДАЛИТЬ для подтверждения: ").strip() != "УДАЛИТЬ":
        print("Отменено")
        return
    stop()
    for name in ("ruter-v2-route.service", "ruter-v2-core.service"):
        run("systemctl", "disable", name, check=False)
        (Path("/etc/systemd/system") / name).unlink(missing_ok=True)
    run("systemctl", "daemon-reload")
    for name in ("ruter", "ruter-v2"):
        launcher = Path("/usr/local/bin") / name
        if launcher.is_symlink() and launcher.resolve(strict=False) == OPT / name:
            launcher.unlink()
    for path in (ETC, OPT, VAR):
        if path.exists():
            shutil.rmtree(path)
    print("Ruter v2 удалён; подписка и настройки v2 удалены. Системные пакеты сохранены.")


MENU = [
    ("1", "Установить Ruter v2", "install"),
    ("2", "Обновить подписку из текущего источника", "update"),
    ("3", "Вставить новую ссылку подписки", "change-source"),
    ("4", "Запустить шлюз", "start"),
    ("5", "Остановить шлюз", "stop"),
    ("6", "Состояние", "status"),
    ("7", "Диагностика", "diagnostics"),
    ("8", "Включить / выключить автозапуск", "autostart"),
    ("9", "Удалить Ruter v2", "uninstall"),
]


def dispatch(action: str, interface: str | None, binary: str | None) -> None:
    actions = {
        "install": lambda: install(interface, binary),
        "update": update_subscription,
        "start": start,
        "stop": stop,
        "status": status,
        "diagnostics": diagnostics,
        "autostart": autostart,
        "change-source": change_source,
        "uninstall": uninstall,
    }
    actions[action]()


def main() -> int:
    parser = argparse.ArgumentParser(description="Ruter v2 Debian test gateway")
    parser.add_argument("action", nargs="?", choices=[x[2] for x in MENU])
    parser.add_argument("--interface", help="LAN-интерфейс (по умолчанию определяется автоматически)")
    parser.add_argument("--core-binary", help="Путь к заранее проверенному sing-box-lx")
    args = parser.parse_args()
    try:
        if args.action:
            dispatch(args.action, args.interface, args.core_binary)
        else:
            while True:
                print("\n=== Ruter v2 ===")
                for number, label, _ in MENU:
                    print(f"{number}. {label}")
                print("0. Выход")
                choice = input("Выбор: ").strip()
                if choice == "0":
                    break
                selected = next((item for item in MENU if item[0] == choice), None)
                if selected is None:
                    print("Неизвестный пункт")
                    continue
                try:
                    dispatch(selected[2], args.interface, args.core_binary)
                except (RuntimeError, SubscriptionError, OSError, subprocess.CalledProcessError) as exc:
                    print("Ошибка:", exc)
    except (RuntimeError, SubscriptionError, OSError, subprocess.CalledProcessError) as exc:
        print("Ошибка:", exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
