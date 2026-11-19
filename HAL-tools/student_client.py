import math

from dnp3.application.parser import parse_response
from dnp3.core.enums import CommandStatus, ControlCode
from dnp3.master import DefaultSOEHandler, Master, MasterConfig, MasterTcpRunner
from dnp3.master.commands import ControlOperation, DirectOperateTask, OperateTask, SelectTask


class CommandMaster(Master):
    def process_response(self, data):
        response = parse_response(data)
        for block in response.objects:
            if block.header.group in (12, 41):
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
            operation = ControlOperation(index=index, 
                                         is_analog=kind == "analog", 
                                         analog_value=float(value),
                                         control_code=ControlCode.LATCH_ON if value else ControlCode.LATCH_OFF)
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
