#DNP3 adapters
import math
import time
from collections import deque
from datetime import datetime
from dnp3.database import Database, AnalogInputConfig, BinaryInputConfig, AnalogOutputConfig, BinaryOutputConfig
from dnp3.database.point import EventClass
from dnp3.outstation import Outstation, OutstationConfig, OutstationTcpRunner
from dnp3.outstation.config import UnsolicitedConfig
from dnp3.outstation.handler import DefaultCommandHandler, CommandResult
from dnp3.core.enums import CommandStatus, ControlCode
from dnp3.master import Master, MasterConfig, MasterTcpRunner, DefaultSOEHandler
from dnp3.master.commands import ControlOperation, DirectOperateTask, SelectTask, OperateTask
from dnp3.application.parser import parse_response

POINTS = {('analog', 0): 'AO0 steam_sp', 
          ('analog', 1): 'AO1 load_sp', 
          ('binary', 0): 'CROB0 run',
          ('binary', 1): 'CROB1 breaker', 
          ('binary', 2): 'CROB2 trip', 
          ('binary', 3): 'CROB3 reset',
          ('binary', 4): 'CROB4 autosync',
          ('binary', 5): 'CROB5 bypass',
          ('binary', 6): 'CROB6 repair'}
EFFECTS = {('binary', 0, True): 'turbine start', 
           ('binary', 0, False): 'turbine stop',
           ('binary', 1, True): 'breaker closed', 
           ('binary', 1, False): 'breaker opened',
           ('binary', 2, True): 'plant tripped', 
           ('binary', 3, True): 'trip reset',
           ('binary', 4, True): 'auto-sync armed',
           ('binary', 4, False): 'auto-sync off',
           ('binary', 5, True): 'SYNC CHECK OFF',
           ('binary', 5, False): 'sync check on',
           ('binary', 6, True): 'generator replaced'}
REASONS = {'BLOCKED': 'interlock', 'OUT_OF_RANGE': 'out of range', 'NOT_SUPPORTED': 'not supported'}


class Controls(DefaultCommandHandler):
    def __init__(self, station):
        self.station, self.plant, self.publish = station, station.plant, station.publish

    def _operate(self, kind, index, value):
        #Actuate and log 
        p = self.plant
        old = {('analog', 0): p.steam, 
               ('analog', 1): p.load, 
               ('binary', 0): p.running,
               ('binary', 1): p.breaker, 
               ('binary', 2): p.tripped, 
               ('binary', 4): p.autosync,
               ('binary', 5): p.sync_bypass}.get((kind, index))
        was_damaged = p.damaged
        status = p.command(kind, index, value)
        self.publish()
        tied = p.breaker and p.grid_ok
        if status != 'SUCCESS':
            effect = REASONS.get(status, status)
        elif p.damaged and not was_damaged:
            effect = 'GENERATOR DAMAGED'
        elif kind == 'binary':
            effect = EFFECTS.get((kind, index, value), '')
        elif value == old:
            effect = 'no change'
            # grid holds speed; steam changes export instead
        elif index == 0 and tied:  
            effect = 'MW rising' if value > old else 'MW falling'
        elif not (p.running if index == 0 else p.breaker and not tied):
            effect = 'setpoint only'
        else:  #
            effect = 'rpm rising' if (value > old) == (index == 0) else 'rpm falling'
        self._log(kind, index, value, old, status == 'SUCCESS', effect)
        return status

    def _log(self, kind, index, value, old, accepted, effect):
        fmt = (lambda v: 'ON' if v else 'OFF') if kind == 'binary' else (lambda v: f'{v:g}')
        peer = self.station.runner.peer
        self.station.writes.append((datetime.now().strftime('%H:%M:%S'), peer[0] if peer else '-', POINTS.get((kind, index), f'{kind} {index}'), fmt(value) if old is None else f'{fmt(old)}->{fmt(value)}', 'ACCEPT' if accepted else 'REJECT', effect))

    def _binary(self, index, code, count, on_time, off_time, execute):
        if code not in (ControlCode.LATCH_ON, ControlCode.LATCH_OFF) or count != 1 or on_time or off_time:
            self.plant.record(f'DNP3 rejected binary {index}: unsupported CROB')
            if execute:
                self._log('binary', index, code == ControlCode.LATCH_ON, None, False, 'unsupported CROB')
            return CommandResult.not_supported()
        value = code == ControlCode.LATCH_ON
        if execute:
            return CommandResult(CommandStatus[self._operate('binary', index, value)])
        status = self.plant.validate('binary', index, value)
        self.plant.record(f'DNP3 SELECT binary {index} = {value}: {status} (no actuation)')
        return CommandResult(CommandStatus[status])

    def select_binary_output(self, index, code, count, on_time, off_time):
        return self._binary(index, code, count, on_time, off_time, False)

    def direct_operate_binary_output(self, index, code, count, on_time, off_time):
        return self._binary(index, code, count, on_time, off_time, True)

    def operate_binary_output(self, index, code, count, on_time, off_time, select_sequence):
        return self._binary(index, code, count, on_time, off_time, True)

    def select_analog_output(self, index, value):
        status = self.plant.validate('analog', index, value)
        self.plant.record(f'DNP3 SELECT analog {index} = {value}: {status} (no actuation)')
        return CommandResult(CommandStatus[status])

    def direct_operate_analog_output(self, index, value):
        return CommandResult(CommandStatus[self._operate("analog", index, value)])

    def operate_analog_output(self, index, value, select_sequence):
        return self.direct_operate_analog_output(index, value)


class PlantRunner(OutstationTcpRunner):
    peer = None

    async def _handle_connection(self, channel):
        self.peer = getattr(channel, 'remote_address', None)
        await super()._handle_connection(channel)


class PlantOutstation:
    def __init__(self, plant, host="127.0.0.1", port=20000):
        self.plant = plant
        self.reads, self.last_request, self.writes = 0, None, deque(maxlen=8)
        self.db = Database()
        for i in range(8):
            self.db.add_analog_input(i, AnalogInputConfig(event_class=EventClass.NONE))
        for i in range(5):
            self.db.add_binary_input(i, BinaryInputConfig(event_class=EventClass.NONE))
        for i in range(7):
            self.db.add_binary_output(i, BinaryOutputConfig(event_class=EventClass.NONE))
        for i in range(2):
            self.db.add_analog_output(i, AnalogOutputConfig())
        self.outstation = Outstation(database=self.db, handler=Controls(self), config=OutstationConfig(address=10, master_address=0, time_sync_required=False, unsolicited=UnsolicitedConfig(enabled=False)))
        process = self.outstation.process_request

        def counted(data, **kwargs):
            self.last_request = time.monotonic()
            self.reads += data[1:2] == b'\x01'  
            return process(data, **kwargs)
        self.outstation.process_request = counted
        self.runner = PlantRunner(self.outstation, host=host, port=port)
        self.publish()

    def publish(self):
        p = self.plant
        # Integer-scaled points avoid ambiguity in g30v1 integrity responses
        slip = p.slip if p.grid_ok else 0
        for i, value in enumerate((p.rpm, p.frequency * 100, p.valve * 10, p.power * 100, p.load * 10, p.steam * 10, p.grid_f * 100, slip * 10)):
            self.db.update_analog_input(i, round(value))
        for i, value in enumerate((p.running, p.breaker, p.tripped, p.grid_ok, p.damaged)):
            self.db.update_binary_input(i, value)
        for i, value in enumerate((p.running, p.breaker, p.tripped, False, p.autosync, p.sync_bypass, False)):
            self.db.update_binary_output(i, value)
        self.db.update_analog_output(0, p.steam)
        self.db.update_analog_output(1, p.load)


class CommandMaster(Master):
    def process_response(self, data):
        response = parse_response(data)
        for block in response.objects:
            if block.header.group in (12, 41):
                # verify echo shape
                expected_size = 13 if block.header.group == 12 else 7
                if block.header.qualifier != 0x17 or len(block.data) != expected_size or block.data[0] != 1:
                    raise ValueError("Unexpected DNP3 command response layout")
                self.command_status = CommandStatus(block.data[-1]).name
        return super().process_response(data)


async def exchange(host="127.0.0.1", port=20000, kind=None, index=0, value=0, sbo=False, outstation=10):
    if kind not in (None, "binary", "analog") or type(index) is not int or not 0 <= index <= 255:
        raise ValueError("Invalid command kind or index")
    if kind == "analog" and (not math.isfinite(value) or int(value) != value or not -(2**31) <= value < 2**31):
        raise ValueError("This client uses g41v1: enter a whole-number 32-bit setpoint")
    handler = DefaultSOEHandler()
    master = CommandMaster(handler=handler, config=MasterConfig(address=1, outstation_address=outstation))
    master.command_status = None
    async with MasterTcpRunner(master=master, host=host, port=port) as runner:
        if kind:
            operation = ControlOperation(index=index, is_analog=kind == "analog", analog_value=float(value), control_code=ControlCode.LATCH_ON if value else ControlCode.LATCH_OFF)
            task = SelectTask(operations=[operation]) if sbo else DirectOperateTask(operations=[operation])
            await runner.request(task.build_request(seq=0))
            if sbo and master.command_status == "SUCCESS":
                master.command_status = None
                await runner.request(OperateTask(operations=[operation]).build_request(seq=1))
            if master.command_status is None:
                raise ValueError("DNP3 peer returned no command status")
        await runner.integrity_poll()
        return {"status": master.command_status or "SUCCESS",
                "analog": {str(i): v.value for i, v in handler.analog_inputs.items()},
                "binary": {str(i): v.value for i, v in handler.binary_inputs.items()},
                "binary_output": {str(i): v.value for i, v in handler.binary_outputs.items()},
                "analog_output": {str(i): v.value for i, v in handler.analog_outputs.items()}}
