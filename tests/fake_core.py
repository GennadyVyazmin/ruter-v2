#!/usr/bin/env python3
"""CI only: real non-persistent TUN lifetime, no VPN or subscription."""
import fcntl
import os
import signal
import struct
import subprocess
import time

fd = os.open('/dev/net/tun', os.O_RDWR)
fcntl.ioctl(fd, 0x400454ca, struct.pack('16sH', b'ruter-v2-tun', 0x1001))
subprocess.run(['ip', 'link', 'set', 'ruter-v2-tun', 'up'], check=True)
signal.signal(signal.SIGTERM, lambda *_: exit(0))
while True:
    time.sleep(1)
