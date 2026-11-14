"""USB test relay: local loopback -> NAS Tailscale IP. No TLS or credentials are terminated here."""
import argparse
import asyncio


async def main(host, remote_port, local_port):
    async def connected(reader, writer):
        remote_writer = None
        tasks = []
        try:
            remote_reader, remote_writer = await asyncio.wait_for(asyncio.open_connection(host, remote_port), 10)
            async def copy(source, target):
                while chunk := await source.read(64 * 1024):
                    target.write(chunk)
                    await target.drain()
            tasks = [asyncio.create_task(copy(reader, remote_writer)), asyncio.create_task(copy(remote_reader, writer))]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, TimeoutError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for connection in (writer, remote_writer):
                if connection:
                    connection.close()
                    try:
                        await connection.wait_closed()
                    except OSError:
                        pass
    server = await asyncio.start_server(connected, '127.0.0.1', local_port)
    print(f'USB relay: 127.0.0.1:{local_port} -> {host}:{remote_port}', flush=True)
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='100.90.158.48')
    parser.add_argument('--remote-port', type=int, default=8765)
    parser.add_argument('--local-port', type=int, default=18765)
    args = parser.parse_args()
    try:
        asyncio.run(main(args.host, args.remote_port, args.local_port))
    except KeyboardInterrupt:
        pass
