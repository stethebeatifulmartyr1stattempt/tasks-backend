"""Run fixture tests with all external networking disabled."""
from pathlib import Path
import ipaddress
import socket
import sys
import unittest

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / 'tests'))
original_connect = socket.socket.connect
original_connect_ex = socket.socket.connect_ex

def allowed(address):
    if not isinstance(address, tuple):
        raise RuntimeError('Non-loopback networking is disabled in tests')
    host = address[0]
    if host == 'localhost':
        return
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    if not local:
        raise RuntimeError('External networking is disabled in tests')

def connect(self, address):
    allowed(address)
    return original_connect(self, address)

def connect_ex(self, address):
    allowed(address)
    return original_connect_ex(self, address)

socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover(str(root / 'tests')))
raise SystemExit(not result.wasSuccessful())
