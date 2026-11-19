import argparse
import asyncio
import time
from datetime import datetime

from student_client import exchange

GROUPS = (('analog', 'AI'), 
          ('binary', 'BI'), 
          ('analog_output', 'AO status'), 
          ('binary_output', 'BO status'))


def points(poll):
    return {f'{label} {index}': value
            for key, label in GROUPS
            for index, value in sorted(poll.get(key, {}).items(), key=lambda item: int(item[0]))}


def summarize(samples):
    lines = []
    for point in samples[0]:
        values = [sample[point] for sample in samples if point in sample]
        changes = sum(a != b for a, b in zip(values, values[1:]))
        if point.startswith(('BI ', 'BO ')):
            behavior = 'toggled' if changes else 'steady binary'
        elif changes:
            behavior = 'changing analog'
        else:
            behavior = 'steady analog'
        lines.append(f'{point:13} {values[0]!s:>8} -> {values[-1]!s:<8} 'f'changes={changes:<2} {behavior}')
    return lines


async def run(args):
    async def poll():
        return points(await exchange(args.host, args.port, outstation=args.outstation))

    print(f'Polling assigned DNP3 outstation {args.outstation} at {args.host}:{args.port}.')
    first = await poll()
    if not first:
        raise RuntimeError('The outstation responded, but this poll contained no points')
    print(f'[{datetime.now():%H:%M:%S}] response received:')
    for point, value in first.items():
        print(f'  {point:13} raw={value}')
    if not args.watch:
        return

    samples = [first]
    end = time.monotonic() + args.watch
    while time.monotonic() < end:
        await asyncio.sleep(min(args.interval, max(0, end - time.monotonic())))
        try:
            current = await poll()
        except Exception as error:
            print(f'[{datetime.now():%H:%M:%S}] poll missed: {type(error).__name__}: {error}')
            continue
        changes = [(key, samples[-1].get(key), value) for key, value in current.items()
                   if samples[-1].get(key) != value]
        print(f'[{datetime.now():%H:%M:%S}] ' +
              (', '.join(f'{key} {old}->{new}' for key, old, new in changes) if changes else 'no point changes'))
        samples.append(current)
    print('\nObserved behavior (a clue, not a point name):')
    for line in summarize(samples):
        print('  ' + line)



if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1', help='assigned lab plant host')
    parser.add_argument('--port', type=int, default=20000)
    parser.add_argument('--outstation', type=int, default=10, help='DNP3 destination address, not a TCP port')
    parser.add_argument('--watch', type=float, default=0, metavar='SECONDS', help='repeat read-only polls for this long')
    parser.add_argument('--interval', type=float, default=2, metavar='SECONDS', help='seconds between watch polls')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 0 <= args.outstation <= 65519 or args.watch < 0 or args.interval <= 0:
        parser.error('port, outstation, watch, or interval is out of range')
    try:
        asyncio.run(run(args))
    except Exception as error:
        parser.exit(1, f'DNP3 poll failed: {error}\nCheck the host, port, outstation address, and lab state.\n')
