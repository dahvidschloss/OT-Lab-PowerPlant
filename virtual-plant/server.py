import argparse
import asyncio
import contextlib
import json
import logging
import math
import os
import time
from plant import Plant
from protocol import PlantOutstation
from terminal import TerminalDisplay


class GridLink(asyncio.DatagramProtocol):

    def __init__(self, plant):
        self.plant = plant

    def datagram_received(self, data, addr):
        try:
            reply = json.loads(data)
            f, theta = float(reply['f']), float(reply['theta'])
        except (ValueError, KeyError, TypeError, IndexError):
            return
        if math.isfinite(f) and math.isfinite(theta) and 55 < f < 65:
            self.plant.grid_update(f, theta)


async def serve(args):
    plant = Plant()
    station = PlantOutstation(plant, args.dnp_host, args.dnp_port)
    display = TerminalDisplay(station, f'{args.dnp_host}:{args.dnp_port}', quiet=args.quiet)
    grid = None
    if args.grid:
        host, port = args.grid.rsplit(':', 1)
        grid, _ = await asyncio.get_running_loop().create_datagram_endpoint(lambda: GridLink(plant), remote_addr=(host, int(port)))
    feedback = None
    if args.feedback:  # one-way truth feed to the physical model (ESP32)
        host, port = args.feedback.rsplit(':', 1)
        feedback, _ = await asyncio.get_running_loop().create_datagram_endpoint(asyncio.DatagramProtocol, remote_addr=(host, int(port)))

    async def tick():
        previous = time.monotonic()
        while True:
            now = time.monotonic()
            plant.tick(now - previous)
            station.publish()
            if grid:
                grid.sendto(json.dumps({'id': 'ccpp', 'breaker': plant.breaker, 'mw': round(plant.power, 1),
                                        'damaged': plant.damaged}).encode())
            if feedback:
                feedback.sendto(json.dumps({'rpm': round(plant.rpm), 'valve': round(plant.valve, 1), 
                                            'running': plant.running,
                                            'breaker': plant.breaker, 'tripped': plant.tripped,
                                            'damaged': plant.damaged}).encode())
            display.draw(plant.snapshot())
            previous = now
            await asyncio.sleep(0.1)

    dnp_task = asyncio.create_task(station.runner.run())
    tick_task = None
    try:
        for _ in range(100):
            if dnp_task.done():
                await dnp_task
                raise RuntimeError("DNP3 listener stopped during startup")
            if station.runner.is_running:
                break
            await asyncio.sleep(.02)
        else:
            raise RuntimeError("DNP3 listener did not start")
        tick_task = asyncio.create_task(tick())
        print(f"DNP3: {args.dnp_host}:{args.dnp_port}, outstation 10 | Ctrl+C stops", flush=True)
        await asyncio.gather(dnp_task, tick_task)
    finally:
        if tick_task:
            tick_task.cancel()
        for endpoint in (grid, feedback):
            if endpoint:
                endpoint.close()
        await station.runner.stop()
        dnp_task.cancel()
        for task in (tick_task, dnp_task):
            if task:
                with contextlib.suppress(asyncio.CancelledError):
                    await task


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HAL virtual power plant")
    parser.add_argument('--quiet', action='store_true', help='Disable the live terminal display')
    parser.add_argument("--dnp-host", default="127.0.0.1", choices=["127.0.0.1", "0.0.0.0"])
    parser.add_argument("--dnp-port", type=int, default=20000)
    parser.add_argument("--grid", help="grid bus host:port (UDP); omit to run islanded")
    parser.add_argument("--feedback", default=os.environ.get("FEEDBACK") or None,
                        help="physical model host:port (UDP, 10 Hz truth feed); default $FEEDBACK, omit for none")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    if not args.quiet:
        logging.getLogger("dnp3").setLevel(logging.CRITICAL)
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass
