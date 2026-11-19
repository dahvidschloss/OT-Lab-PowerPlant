
import argparse
import asyncio
import math

from student_client import exchange

GROUPS = {'analog': 'ai', 
          'binary': 'bi', 
          'analog_output': 'ao', 
          'binary_output': 'crob'}


def differences(before, after):
    return [(f'{prefix}:{index}', old, value)
            for group, prefix in GROUPS.items()
            for index, value in after.get(group, {}).items()
            if (old := before.get(group, {}).get(index)) != value]


async def run(args):
    async def poll(**command):
        return await exchange(args.host, args.port, outstation=args.outstation, **command)

    point = f'{"CROB" if args.kind == "binary" else "AO"}{args.index}'
    method = 'SELECT then OPERATE' if args.sbo else 'Direct Operate'
    before = await poll()
    print(f'Request: {method} {point} = {args.value:g}.')
    result = await poll(kind=args.kind, index=args.index,
                        value=bool(args.value) if args.kind == 'binary' else args.value, sbo=args.sbo)
    status = result['status']
    print(f'DNP3 command status: {status}')
    if status == 'SUCCESS':
        print('The outstation accepted the operation.')
    elif status == 'BLOCKED':
        print('Interlock rejected the operation.')
    elif status == 'OUT_OF_RANGE':
        print('The setpoint was outside the range allowed by this outstation.')
    else:
        print('The outstation did not accept this operation.')
    print(f'Waiting {args.settle:g}s so plant can respond...')
    await asyncio.sleep(args.settle)
    after = await poll()
    changes = differences(before, after)
    if changes:
        print('Readback changes:')
        for address, old, value in changes:
            print(f'  {address:8} {old} -> {value} (raw)')
    else:
        print('No changed readback points in this interval.')
    return status


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=20000)
    parser.add_argument('--outstation', type=int, default=10)
    parser.add_argument('--kind', choices=('binary', 'analog'), required=True)
    parser.add_argument('--index', type=int, required=True)
    parser.add_argument('--value', type=float, required=True)
    parser.add_argument('--sbo', action='store_true', help='SELECT before OPERATE')
    parser.add_argument('--settle', type=float, default=2, help='seconds before the final readback poll')
    args = parser.parse_args()
    if (not 1 <= args.port <= 65535 or 
            not 0 <= args.outstation <= 65519 or
            not 0 <= args.index <= 255 or 
            not 0 <= args.settle <= 30 or
            not math.isfinite(args.value) or 
            not args.value.is_integer() or
            (args.kind == 'binary' and args.value not in (0, 1))):
        parser.error('port, outstation, index, value, or settle is out of range')
    try:
        status = asyncio.run(run(args))
    except Exception as error:
        parser.exit(1, f'DNP3 exchange failed: {error}\nCommand outcome may be unknown\n')
    raise SystemExit(0 if status == 'SUCCESS' else 1)
