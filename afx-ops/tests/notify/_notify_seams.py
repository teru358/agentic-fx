"""試験の子 process 側で notify の送信に差し込む部品 (名前解決・試験 CA)。

production の送信は system CA と aiohttp 既定の resolver を使い、URL の port は 443 だけ。
この host の 443 には本物の待受があり、非特権では 443 を bind できないので、試験は
resolver の答えに loopback の試験 port を載せる (aiohttp は resolver の答えの port に
connect し、SNI と hostname の検証は URL の host のまま)。443 を答えることは決してしない。
"""
from __future__ import annotations

import asyncio
import os
import socket
import ssl
import struct

PRODUCTION_PORT = 443


def _mapped(port: int) -> int:
    if port == PRODUCTION_PORT or not 1024 <= port <= 65535:
        raise AssertionError("試験の resolver は 443 を答えない")
    return port


def dns_query(name: str, query_id: int) -> bytes:
    labels = b"".join(bytes([len(part)]) + part.encode("ascii") for part in name.split("."))
    return struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0) + labels + b"\0" \
        + struct.pack("!HH", 1, 1)


def parse_query(packet: bytes) -> tuple[int, str, bytes]:
    """(id, name, question 部分の bytes)。"""
    query_id = struct.unpack("!H", packet[:2])[0]
    pos, labels = 12, []
    while packet[pos]:
        length = packet[pos]
        labels.append(packet[pos + 1:pos + 1 + length].decode("ascii"))
        pos += 1 + length
    end = pos + 1 + 4
    return query_id, ".".join(labels), packet[12:end]


def dns_answer(query: bytes, address: str | None) -> bytes:
    query_id, _name, question = parse_query(query)
    if address is None:
        return struct.pack("!HHHHHH", query_id, 0x8183, 1, 0, 0, 0) + question
    return (struct.pack("!HHHHHH", query_id, 0x8180, 1, 1, 0, 0) + question
            + struct.pack("!HHHIH", 0xC00C, 1, 1, 0, 4) + socket.inet_aton(address))


def parse_answer(packet: bytes, query_id: int) -> str:
    got_id, flags, _qd, answers = struct.unpack("!HHHH", packet[:8])
    if got_id != query_id or flags & 0x000F or answers < 1:
        raise OSError(2, "name not resolved")
    return socket.inet_ntoa(packet[-4:])


def udp_resolver_factory(*, dns_port: int, port: int, resolv_conf: str | None = None):
    """UDP DNS (loopback の試験 server) で毎回引く resolver を作る関数。

    resolv_conf があれば、引くたびに glibc と同じくその file を開いて読む (読めなければ
    名前解決の失敗)。
    """
    import aiohttp.abc

    class UdpResolver(aiohttp.abc.AbstractResolver):
        async def resolve(self, host, port_arg=0, family=socket.AF_INET):
            if resolv_conf is not None:
                with open(resolv_conf, encoding="ascii") as conf:
                    conf.read()
            loop = asyncio.get_running_loop()
            query_id = int.from_bytes(os.urandom(2), "big")
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.setblocking(False)
                await loop.sock_sendto(sock, dns_query(host, query_id), ("127.0.0.1", dns_port))
                packet, _ = await asyncio.wait_for(loop.sock_recvfrom(sock, 512), 5)
            finally:
                sock.close()
            address = parse_answer(packet, query_id)
            return [{"hostname": host, "host": address, "port": _mapped(port),
                     "family": socket.AF_INET, "proto": 0, "flags": socket.AI_NUMERICHOST}]

        async def close(self):
            return None

    return UdpResolver


def nss_resolver_factory(*, port: int):
    """aiohttp 既定の ThreadedResolver (getaddrinfo、NSS) で引き、port だけ試験 port にする。"""
    import aiohttp

    class NssResolver(aiohttp.ThreadedResolver):
        async def resolve(self, host, port_arg=0, family=socket.AF_INET):
            found = await super().resolve(host, port_arg, family)
            return [{**info, "port": _mapped(port)} for info in found
                    if info["family"] == socket.AF_INET]

    return NssResolver


def test_ca_context(ca_pem: str) -> ssl.SSLContext:
    """試験 CA だけを信頼する context。file を読まない (隔離後に作れるように)。"""
    return ssl.create_default_context(cadata=ca_pem)


test_ca_context.__test__ = False
