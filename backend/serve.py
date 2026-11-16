"""Start the api on one dual-stack socket (IPv6 and IPv4).

`uvicorn --host ::` goes through asyncio, which marks the socket IPv6-only,
so Railway's health probe (IPv4) can never connect while the private
network (IPv6) can. Binding the socket here with IPV6_V6ONLY off serves
both, in one process.
"""
import os
import socket

import uvicorn


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
    )
    uvicorn.Server(config).run(sockets=[sock])


if __name__ == "__main__":
    main()
