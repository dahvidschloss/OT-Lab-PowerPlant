"""
SCADA front-end processor (FEP) for the Cyber Kinetic City.

Adding a protocol:
    1. drivers/<name>.py with a class built from the device's config dict, exposing
           async poll() -> {address: number}       e.g. {'ai:0': 3600.0, 'bi:1': True}
           async write(address, value) -> int      0 = accepted, else the protocol's status code
    2. add it to DRIVERS below
    3. map its points in gateway.json
"""
import argparse
import asyncio
import json
import time
from datetime import datetime
from pymodbus.datastore import ModbusSequentialDataBlock, ModbusSlaveContext, ModbusServerContext
from pymodbus.server import StartAsyncTcpServer
from drivers.dnp3 import Dnp3Driver

DRIVERS = {'dnp3': Dnp3Driver}
# Modbus function code for each register area
FC = {'co': 1, 'di': 2, 'hr': 3, 'ir': 4}  
# no good poll for this long = comms lost
STALE_SECONDS = 3 

def log(message):
    print(f'{datetime.now():%H:%M:%S}  {message}', flush=True)


def area(address):
    #'ir:3' -> ('ir', 3)
    kind, _, index = address.partition(':')
    if kind not in FC or not index.isdigit() or int(index) > 65535:
        raise ValueError(f'bad Modbus address {address!r}; use co/di/hr/ir:<0-65535>')
    return kind, int(index)


def to_register(value, scale=1):
    #Field value -> one signed 16-bit register
    return max(-32768, min(32767, round(value * scale))) & 0xFFFF


def from_register(register, scale=1):
    return (register - 65536 if register > 32767 else register) / scale


class CommandBlock(ModbusSequentialDataBlock):
    #Coils / holding registers
    def __init__(self, size, on_write):
        super().__init__(0, [0] * size)
        self.on_write = on_write

    def setValues(self, address, values):
        super().setValues(address, values)
        self.on_write(address, values if isinstance(values, list) else [values])


class Gateway:
    def __init__(self, config):
        self.devices, self.commands, self.tasks = [], {}, set()
        sizes, used = dict.fromkeys(FC, 1), set()

        def claim(address, owner):
            kind, index = area(address)
            if (kind, index) in used:
                raise ValueError(f'{owner}: Modbus address {address} already in use')
            used.add((kind, index))
            sizes[kind] = max(sizes[kind], index + 1)
            return kind, index

        for cfg in config['devices']:
            if cfg['driver'] not in DRIVERS:
                raise ValueError(f'{cfg["name"]}: unknown driver {cfg["driver"]!r}; have {sorted(DRIVERS)}')
            device = {'cfg': cfg, 
                      'driver': DRIVERS[cfg['driver']](cfg), 
                      'lock': asyncio.Lock(),
                      'seen': -float('inf'), 
                      'ok': None, 
                      'error': '',
                      'comms': claim(cfg['comms'], cfg['name']), 'status': claim(cfg['status'], cfg['name']),
                      'telemetry': []}
            for point in cfg['points']:
                kind, index = claim(point['modbus'], f'{cfg["name"]}.{point["tag"]}')
                if kind in ('co', 'hr'):
                    self.commands[(kind, index)] = (device, point)
                device['telemetry'].append((kind, index, point))
            self.devices.append(device)
        self.store = ModbusSlaveContext(
            di=ModbusSequentialDataBlock(0, [0] * sizes['di']), 
            ir=ModbusSequentialDataBlock(0, [0] * sizes['ir']),
            co=CommandBlock(sizes['co'], lambda a, v: self.on_write('co', a, v)),
            hr=CommandBlock(sizes['hr'], lambda a, v: self.on_write('hr', a, v)),
            zero_mode=True)

    def set(self, kind, index, value):
        block = self.store.store[{'co': 'c', 'di': 'd', 'hr': 'h', 'ir': 'i'}[kind]]
        # base method a readback must not fire a command
        ModbusSequentialDataBlock.setValues(block, index, [value])  

    async def poll(self, device):
        cfg, driver = device['cfg'], device['driver']
        while True:
            try:
                async with device['lock']:
                    values = await asyncio.wait_for(driver.poll(), STALE_SECONDS)
                for kind, index, point in device['telemetry']:
                    if point['source'] in values:
                        value = values[point['source']]
                        self.set(kind, index, int(bool(value)) if kind in ('di', 'co') else to_register(value, point.get('scale', 1)))
                device['seen'], device['error'] = time.monotonic(), ''
            # any driver failure must cost one poll
            except Exception as error:  
                device['error'] = f'{type(error).__name__}: {error}'
            ok = time.monotonic() - device['seen'] < STALE_SECONDS
              # values above freeze at last good poll; this informs HMI they are stale
            self.set(*device['comms'], int(ok))
            if ok != device['ok']:
                log(f'{cfg["name"]}: comms {"OK" if ok else "LOST"}' + (f' ({device["error"]})' if not ok and device['error'] else ''))
                device['ok'] = ok
            await asyncio.sleep(cfg.get('poll_seconds', 1))

    def on_write(self, kind, address, values):
        for offset, register in enumerate(values):
            target = self.commands.get((kind, address + offset))
            if target:
                task = asyncio.get_running_loop().create_task(self.command(kind, *target, register))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)

    async def command(self, kind, device, point, register):
        cfg = device['cfg']
        value = bool(register) if kind == 'co' else from_register(register, point.get('scale', 1))
        try:
            async with device['lock']:
                code = await asyncio.wait_for(device['driver'].write(point['source'], value), STALE_SECONDS)
        except Exception as error:  
            code = -1
            log(f'{cfg["name"]}.{point["tag"]} <- {value}: FAILED {type(error).__name__}: {error}')
        else:
            log(f'{cfg["name"]}.{point["tag"]} <- {value}: {"ACCEPTED" if code == 0 else f"REJECTED (status {code})"}')
        self.set(*device['status'], to_register(code))

    async def run(self, host, port):
        for device in self.devices:
            self.tasks.add(asyncio.create_task(self.poll(device)))
        log(f'FEP: Modbus TCP on {host}:{port}, {len(self.devices)} device(s): ' + ', '.join(f'{d["cfg"]["name"]} ({d["cfg"]["driver"]})' for d in self.devices))
        await StartAsyncTcpServer(context=ModbusServerContext(slaves=self.store, single=True), address=(host, port))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Cyber Kinetic City SCADA front-end processor')
    parser.add_argument('config', nargs='?', default='gateway.json')
    args = parser.parse_args()
    with open(args.config) as f:
        config = json.load(f)
    try:
        asyncio.run(Gateway(config).run(config['modbus']['host'], config['modbus']['port']))
    except KeyboardInterrupt:
        pass
