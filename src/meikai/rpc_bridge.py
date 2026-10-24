"""把标准输入输出接到节点套接字。连接保持到任一方关闭。"""

import socket
import sys
import threading

SOCKET = "/run/meikai/node.sock"


def _pull(src):
    # read(n) 会攒满 n 字节。一条事件只有几十字节，会一直停着。
    read_some = getattr(src, "read1", None)
    if read_some is None:
        read_some = src.read
    return read_some(65536)


def _pump(src, dst, stop: threading.Event) -> None:
    try:
        while True:
            chunk = _pull(src)
            if not chunk:
                break
            dst.write(chunk)
            dst.flush()
    except Exception:
        pass
    finally:
        stop.set()


def main() -> None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(SOCKET)
    except OSError:
        return
    # 不带缓冲。带缓冲时会攒满才返回，一条事件就停在这里。
    reader = sock.makefile("rb", buffering=0)
    writer = sock.makefile("wb", buffering=0)
    stop = threading.Event()
    incoming = threading.Thread(target=_pump, args=(sys.stdin.buffer, writer, stop), daemon=True)
    outgoing = threading.Thread(target=_pump, args=(reader, sys.stdout.buffer, stop), daemon=True)
    incoming.start()
    outgoing.start()
    stop.wait()
    try:
        sock.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
