"""Start the api on one dual-stack socket (IPv6 and IPv4).

`uvicorn --host ::` goes through asyncio, which marks the socket IPv6-only,
so Railway's health probe (IPv4) can never connect while the private
network (IPv6) can. Binding the socket here with IPV6_V6ONLY off serves
both, in one process.
"""
import logging
import os
import re
import socket

import uvicorn


_TOKEN_RE = re.compile(r"([?&]token=)[^&\s\"]+")


def _redact(text: str) -> str:
    return _TOKEN_RE.sub(lambda m: m.group(1) + "REDACTED", text)


class _RedactTokens(logging.Filter):
    """The live-update connection (/api/ws) sends the login token in its
    address; keep it out of the access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_redact(a) if isinstance(a, str) else a for a in record.args)
        if isinstance(record.msg, str):
            record.msg = _redact(record.msg)
        return True


def main() -> None:
    port = int(os.environ.get("PORT") or 8000)
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    sock.bind(("::", port))
    config = uvicorn.Config(
        "server:app",
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level=(os.environ.get("LOG_LEVEL") or "info").lower(),
        # Keep idle connections open longer than Caddy does, so Caddy never
        # reuses a connection the api has just closed (that showed as rare
        # 502s on saves under load).
        timeout_keep_alive=75,
    )
    # uvicorn configures its loggers when Config is created; add the filter after.
    server = uvicorn.Server(config)
    for name in ("uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).addFilter(_RedactTokens())
    server.run(sockets=[sock])


if __name__ == "__main__":
    main()
