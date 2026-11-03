"""Staged code-only GitHub updates. No config regeneration or service restart."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from ruter_v2.routing import routing_lock

REPOSITORY = 'https://github.com/GennadyVyazmin/ruter-v2.git'
FILES = {
    'ruter_v2/__init__.py': 'ruter_v2/__init__.py',
    'ruter_v2/subscription.py': 'ruter_v2/subscription.py',
    'ruter_v2/routing.py': 'ruter_v2/routing.py',
    'ruter_v2/updater.py': 'ruter_v2/updater.py',
    'ruter_v2/manager.py': 'ruter_v2/manager.py',
    'ruter_v2/route.sh': 'route.sh',
    'ruter-v2': 'ruter-v2',
    'ruter': 'ruter',
}
UNITS = ('ruter-v2-core.service', 'ruter-v2-route.service')


def atomic_copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.ruter-code-', dir=destination.parent)
    os.close(fd)
    try:
        shutil.copy2(source, name)
        os.replace(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)


def deploy(source, opt, var, unit_dir, runner):
    """Caller holds update + routing locks. Keep code backups even on success."""
    targets = [(source / src, opt / dst) for src, dst in FILES.items()]
    targets += [(source / 'systemd' / name, unit_dir / name) for name in UNITS]
    backup = Path(tempfile.mkdtemp(prefix=time.strftime('code-%Y%m%d-%H%M%S-'), dir=var))
    backup.chmod(0o700)
    previous = []
    for index, (src, dst) in enumerate(targets):
        if not src.is_file() or src.is_symlink() or dst.is_symlink():
            raise RuntimeError('Недопустимый файл обновления: ' + str(src))
        old = backup / str(index) if dst.exists() else None
        if old is not None:
            shutil.copy2(dst, old)
        previous.append((dst, old))
    (backup / 'files.json').write_text(json.dumps([
        {'path': str(dst), 'backup': old.name if old else None} for dst, old in previous], indent=2))
    try:
        for src, dst in targets:
            atomic_copy(src, dst)
        # Existing processes and old oneshot route units keep running unchanged.
        runner('systemctl', 'daemon-reload')
    except Exception:
        for dst, old in reversed(previous):
            if old is None:
                dst.unlink(missing_ok=True)
            else:
                atomic_copy(old, dst)
        runner('systemctl', 'daemon-reload')
        raise
    return backup


def update_project(source_dir=None):
    from ruter_v2.manager import OPT, VAR, ETC, require_root, run
    require_root()
    if not (ETC / 'config.json').is_file():
        raise RuntimeError('Сначала установите Ruter v2')
    VAR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (VAR / 'update.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with tempfile.TemporaryDirectory(prefix='.update-', dir=VAR) as directory:
            source = Path(directory) / 'source'
            if source_dir:
                # Bootstrap already fetched and checked this checkout. Copy a fixed
                # tracked commit instead of trusting unrelated/untracked files.
                remote = run('git', '-C', str(source_dir), 'remote', 'get-url', 'origin', capture=True).stdout.strip()
                if remote != REPOSITORY or run('git', '-C', str(source_dir), 'status', '--porcelain', capture=True).stdout:
                    raise RuntimeError('Нужен чистый официальный репозиторий Ruter v2')
                run('git', 'clone', '--no-hardlinks', str(source_dir), str(source))
            else:
                run('git', 'clone', '--depth', '1', '--branch', 'main', REPOSITORY, str(source))
            revision = run('git', '-C', str(source), 'rev-parse', 'HEAD', capture=True).stdout.strip()
            # Candidate code is executed only from the explicitly trusted project.
            subprocess.run(['python3', '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                           cwd=source, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(source)},
                           check=True)
            for name in ('bootstrap.sh', 'ruter', 'ruter-v2', 'ruter_v2/route.sh'):
                run('bash', '-n', str(source / name))
            run(str(OPT / 'bin/sing-box'), 'check', '-c', str(ETC / 'config.json'))
            with routing_lock():
                backup = deploy(source, OPT, VAR, Path('/etc/systemd/system'), run)
            print('Ruter v2 обновлён из GitHub:', revision)
            print('Резервная копия кода:', backup)
            print('Ядро, подписка, конфиг, порты и выбор автозапуска не изменены. VPN не перезапускался.')
            print('Новые зависимости служб применятся при следующем запуске/перезагрузке. Откройте меню ruter заново.')
