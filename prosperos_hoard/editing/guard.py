"""Compatibility signatures for the shared request guard."""
from .hoard_link import guard as shared

LOCAL_HOSTS = shared.LOCAL_HOSTS
DEV_ORIGINS = shared.DEV_ORIGINS
FRAME_DESTS = shared.FRAME_DESTS
SAFE_METHODS = shared.SAFE_METHODS
host_of = shared.host_of
parse_allowed_hosts = shared.parse_allowed_hosts

def is_allowed_host(host, allowed_hosts=()):
    return shared.is_allowed_host(host, allowed=allowed_hosts)

def check_request(method, headers, allowed_hosts=()):
    verdict = shared.check_request(method, headers, allowed=allowed_hosts)
    return verdict[1] if verdict else None

def install_guard(app, allowed_hosts=()):
    shared.install_guard(app, port_getter=lambda: app.state.config.port, allowed_env="", allowed_hosts=allowed_hosts)
