#!/usr/bin/env bash
set -Eeuo pipefail
[[ ${GITHUB_ACTIONS:-} == true ]] || { echo 'CI only: requires a disposable GitHub runner'; exit 1; }
image=ruter-lifecycle-ci
container="ruter-lifecycle-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
cleanup() {
  docker logs "$container" 2>/dev/null || true
  docker exec "$container" journalctl --no-pager -u ruter-v2-core -u ruter-v2-route -n 100 2>/dev/null || true
  docker rm -f "$container" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker build -f tests/systemd.Dockerfile -t "$image" .
docker run -d --name "$container" --privileged --cgroupns=host \
  --tmpfs /run --tmpfs /run/lock --volume /sys/fs/cgroup:/sys/fs/cgroup:rw "$image"
for _ in {1..60}; do
  if docker exec "$container" systemctl list-units >/dev/null 2>&1; then break; fi
  sleep 1
done
prepare_network() {
  docker exec "$container" bash -ec '
    mkdir -p /dev/net
    test -e /dev/net/tun || mknod /dev/net/tun c 10 200
    ip link add ens18 type dummy
    ip link set ens18 up
    ip addr add 192.168.1.48/24 dev ens18
    ip route add default via 192.168.1.1 dev ens18 metric 123
  '
}
wait_routes() {
  for _ in {1..45}; do
    if docker exec "$container" /opt/ruter-v2/route.sh status; then return 0; fi
    sleep 1
  done
  return 1
}
prepare_network
docker exec "$container" systemctl enable ruter-v2-route.service
docker exec "$container" systemctl start ruter-v2-route.service
wait_routes
# Exercise actual file deployment/daemon-reload while core + route watcher run.
before_pid=$(docker exec "$container" systemctl show ruter-v2-core.service -p MainPID --value)
docker exec -i "$container" python3 - <<'PY'
import sys
from pathlib import Path
import subprocess
sys.path.insert(0, '/opt/ruter-v2')
from ruter_v2.updater import deploy
from ruter_v2.routing import routing_lock
config = Path('/etc/ruter-v2/config.json')
config.write_text('{"existing-user-configuration":true}')
before = config.read_bytes()
with routing_lock():
    deploy(Path('/usr/local/share/ruter-ci-source'), Path('/opt/ruter-v2'), Path('/var/lib/ruter-v2'),
           Path('/etc/systemd/system'), lambda *args: subprocess.run(args, check=True))
assert config.read_bytes() == before
PY
after_pid=$(docker exec "$container" systemctl show ruter-v2-core.service -p MainPID --value)
[[ $before_pid == "$after_pid" && $after_pid != 0 ]]
wait_routes
docker exec "$container" systemctl kill --kill-whom=main -s SIGKILL ruter-v2-core.service
sleep 5
docker exec "$container" systemctl is-active --quiet ruter-v2-core.service
wait_routes
docker exec "$container" systemctl kill --kill-whom=main -s SIGKILL ruter-v2-route.service
sleep 5
docker exec "$container" systemctl is-active --quiet ruter-v2-route.service
wait_routes
# Simulate abrupt machine/container loss with persistent old marker but empty kernel.
docker exec "$container" bash -ec 'printf "0\n" > /var/lib/ruter-v2/route.state'
docker kill --signal KILL "$container"
docker start "$container"
for _ in {1..60}; do
  if docker exec "$container" systemctl list-units >/dev/null 2>&1; then break; fi
  sleep 1
done
prepare_network
wait_routes
docker exec "$container" systemctl is-enabled --quiet ruter-v2-route.service
docker exec "$container" systemctl stop ruter-v2-core.service
if docker exec "$container" /opt/ruter-v2/route.sh status; then
  echo 'Routes survived explicit core stop'; exit 1
fi
docker exec "$container" systemctl start ruter-v2-core.service
wait_routes
echo 'systemd: core crash, watcher crash, explicit stop/start, abrupt reboot and autostart PASS'
