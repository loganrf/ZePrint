"""A fake Zebra on raw TCP: captures print data and answers ~HS / ~HI."""

from __future__ import annotations

import socket
import threading

HS_READY = (b"\x02030,0,0,1800,000,0,0,0,000,0,0,0\x03\r\n"
            b"\x02000,0,0,0,0,2,4,0,00000000,1,000\x03\r\n"
            b"\x021234,0\x03\r\n")
HS_HEAD_OPEN = HS_READY.replace(b"000,0,0,0,0,2", b"000,0,1,0,0,2")
HI = b"\x02GX430t-300dpi,V61.17.17Z,12,8176KB\x03\r\n"


class FakePrinter:
    def __init__(self, hs: bytes = HS_READY):
        self.hs = hs
        self.received: list[bytes] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self.uri = f"tcp://127.0.0.1:{self.port}"
        self._got = threading.Condition()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        buf = b""
        with conn:
            conn.settimeout(2)
            while True:
                try:
                    chunk = conn.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                if buf.strip() == b"~HS":
                    conn.sendall(self.hs)
                    buf = b""
                elif buf.strip() == b"~HI":
                    conn.sendall(HI)
                    buf = b""
        if buf:
            with self._got:
                self.received.append(buf)
                self._got.notify_all()

    def wait_for(self, n: int = 1, timeout: float = 10) -> list[bytes]:
        with self._got:
            self._got.wait_for(lambda: len(self.received) >= n, timeout)
        return self.received

    def close(self):
        self._sock.close()
