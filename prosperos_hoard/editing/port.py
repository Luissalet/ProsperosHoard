"""Compatibility signatures for the shared local port discovery."""
from .hoard_link.net import can_listen, free_port
from .hoard_link import net

def find_available_port(preferred, attempts=100, host="127.0.0.1"):
    return net.find_available_port(preferred, span=max(0, attempts - 1), host=host)
