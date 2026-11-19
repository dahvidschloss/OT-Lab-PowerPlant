"""
DNP3 master driver.

Addresses: 
ai:<n> analog input, 
bi:<n> binary input (telemetry); 
crob:<n> 
latch on/off, ao:<n> 
g41 setpoint (commands).

"""
from dnp3.core.enums import CommandStatus
from protocol import exchange


class Dnp3Driver:
    def __init__(self, cfg):
        self.host, self.port, self.outstation = cfg['host'], int(cfg.get('port', 20000)), int(cfg.get('outstation', 10))

    async def poll(self):
        result = await exchange(self.host, self.port, outstation=self.outstation)
        return {**{f'ai:{i}': v for i, v in result['analog'].items()},
                **{f'bi:{i}': v for i, v in result['binary'].items()},
                **{f'crob:{i}': v for i, v in result['binary_output'].items()},   # output status = command readback
                **{f'ao:{i}': v for i, v in result['analog_output'].items()}}

    async def write(self, address, value):
        kind, _, index = address.partition(':')
        if kind not in ('crob', 'ao'):
            raise ValueError(f'{address!r} is not a DNP3 command point (crob:<n> or ao:<n>)')
        result = await exchange(self.host, self.port, 'binary' if kind == 'crob' else 'analog', int(index),
                                bool(value) if kind == 'crob' else round(value), outstation=self.outstation)
        return CommandStatus[result['status']].value
