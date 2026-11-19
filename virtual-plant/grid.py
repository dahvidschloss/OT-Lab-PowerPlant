"""
Grid bus for the Cyber Kinetic City

Wire format (UDP JSON, one datagram each way per plant tick):
    
    plant -> grid  {"id": "ccpp", "breaker": true, "mw": 40.0, "damaged": false}
    grid -> plant  {"f": 60.012, "theta": -12.4}    
    theta = grid phase vs a 60 Hz reference in degrees

"""
import argparse
import asyncio
import json
import math
import time
from datetime import datetime


def log(message):
    print(f'{datetime.now():%H:%M:%S}  {message}', flush=True)


class Grid(asyncio.DatagramProtocol):
    def __init__(self):
        self.start = self.last = time.monotonic()
        self.f, self.theta = 60.0, 0.0
        # id -> (breaker, mw, damaged, last seen)
        self.plants = {}  

    def advance(self):
        now = time.monotonic()
        # Real grids wander a few hundredths of a hertz, so a plant must track it rather than sit at 60.000.
        self.f = 60 + 0.02 * math.sin(2 * math.pi * (now - self.start) / 90)
        self.theta = (self.theta + 360 * (self.f - 60) * (now - self.last) + 180) % 360 - 180
        self.last = now

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            report = json.loads(data)
            plant, breaker, mw, damaged = str(report['id'])[:16], report['breaker'], float(report['mw']), report['damaged']
        except (ValueError, KeyError, TypeError, IndexError):
            return
        if not (isinstance(breaker, bool) and isinstance(damaged, bool) and math.isfinite(mw)):
            return
        old = self.plants.get(plant)
        if old is None or old[0] != breaker or old[2] != damaged:
            log(f'{plant} {addr[0]}: breaker {"CLOSED" if breaker else "OPEN"}' + ('  GENERATOR DAMAGED' if damaged else ''))
        self.plants[plant] = (breaker, mw, damaged, time.monotonic())
        self.advance()
        self.transport.sendto(json.dumps({'f': round(self.f, 4), 'theta': round(self.theta, 2)}).encode(), addr)


async def main(args):
    grid = Grid()
    await asyncio.get_running_loop().create_datagram_endpoint(lambda: grid, local_addr=(args.host, args.port))
    log(f'grid bus listening on {args.host}:{args.port}/udp')
    while True:
        await asyncio.sleep(5)
        grid.advance()
        now = time.monotonic()
        tied = [f'{k} {mw:+.1f} MW' for k, (breaker, mw, _, seen) in grid.plants.items() if breaker and now - seen < 2]
        log(f'{grid.f:.3f} Hz  ' + (', '.join(tied) if tied else 'no plants tied'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Cyber Kinetic City grid bus')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=20100)
    try:
        asyncio.run(main(parser.parse_args()))
    except KeyboardInterrupt:
        pass
