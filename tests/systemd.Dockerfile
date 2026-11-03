FROM ubuntu:24.04
ENV container=docker DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y systemd systemd-sysv python3 iproute2 procps
COPY ruter_v2 /opt/ruter-v2/ruter_v2
COPY ruter_v2/route.sh /opt/ruter-v2/route.sh
COPY systemd /etc/systemd/system
COPY tests/fake_core.py /opt/ruter-v2/bin/sing-box
RUN chmod 755 /opt/ruter-v2/route.sh /opt/ruter-v2/bin/sing-box && \
    mkdir -p /etc/ruter-v2 /var/lib/ruter-v2 && \
    printf 'LAN_IF=ens18\nLAN_IP=192.168.1.48\nLAN_CIDR=192.168.1.0/24\n' > /etc/ruter-v2/network.env
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
