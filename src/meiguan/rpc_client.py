"""节点本机转发。"""

import asyncio
import sys

SOCKET = "/run/meiguan/node.sock"


async def main() -> None:
    raw = sys.stdin.buffer.read()
    if not raw.endswith(b"\n"):
        raw += b"\n"
    try:
        reader, writer = await asyncio.open_unix_connection(SOCKET, limit=4 * 1024 * 1024)
        writer.write(raw)
        await writer.drain()
        line = await reader.readline()
        writer.close()
        if line:
            sys.stdout.buffer.write(line if line.endswith(b"\n") else line + b"\n")
            return
    except Exception:
        pass
    sys.stdout.write('{"ok":false,"error":"offline"}\n')


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        sys.stdout.write('{"ok":false,"error":"offline"}\n')
    sys.exit(0)
