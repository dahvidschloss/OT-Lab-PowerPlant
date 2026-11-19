import argparse
import asyncio
import time

from student_client import exchange


async def run(args):
    end = time.monotonic() + args.seconds
    counts = [0, 0]
    print('Repeated class-0 polls may displace the FEP master. Watch ccfep comms and FUXA freshness.')
    print(f'This tool sends no control commands and stops after {args.seconds:g} seconds.')
    async def worker():
        while time.monotonic() < end:
            try:
                await asyncio.wait_for(exchange(args.host, args.port, outstation=args.outstation), 3)
                counts[0] += 1
            except Exception: 
                counts[1] += 1
            await asyncio.sleep(min(args.interval, max(0, end - time.monotonic())))

    tasks = [asyncio.create_task(worker()) for _ in range(args.workers)]
    await asyncio.sleep(args.seconds)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    print(f'Done: {counts[0]} successful polls, {counts[1]} interrupted. Wait for ccfep comms to recover.')
    print('If FUXA never marked values stale, record that result; do not claim it was blinded.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=20000)
    parser.add_argument('--outstation', type=int, default=10)
    parser.add_argument('--seconds', type=float, default=12)
    parser.add_argument('--interval', type=float, default=0.1)
    parser.add_argument('--workers', type=int, default=3, help='concurrent lab master sessions (1–4)')
    args = parser.parse_args()
    if (not 1 <= args.port <= 65535 or 
            not 0 <= args.outstation <= 65519 or
            not 1 <= args.seconds <= 30 or 
            not 0.05 <= args.interval <= 5 or 
            not 1 <= args.workers <= 4):
        parser.error('use an assigned endpoint, 1–30 seconds, a 0.05–5 second interval, and 1–4 workers')
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print('Stopped. Wait for the normal FEP poll to recover.')
