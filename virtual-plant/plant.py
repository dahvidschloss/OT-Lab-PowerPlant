# turbine-generator model
# grid-tied when the breaker is closed and the grid bus answers; islanded (local load) otherwise
import math
from collections import deque
from datetime import datetime, timezone

 # sync-check window (ANSI 25)
SYNC_HZ, SYNC_DEG = 0.1, 10  
# closing further out of phase than this wrecks the generator (Aurora Idaho labs example)
DAMAGE_DEG = 30               


def wrap(deg):
    return (deg + 180) % 360 - 180


class Plant:
    def __init__(self, online=True):
        self.running = self.breaker = online
        self.tripped = self.damaged = False
        self.autosync = self.sync_bypass = False
        self.steam = 54.0 if online else 50.0
        self.load = 40.0
        self.valve = self.steam if online else 0.0
        self.rpm = 3600.0 if online else 0.0
        self.power = self.load if online else 0.0
        # generator phase vs a 60 Hz reference
        self.theta = 0.0  
        self.grid_f, self.grid_theta, self.grid_age = 60.0, 0.0, math.inf
        self.bad_frequency_time = 0.0
        self.reason = ""
        self.events = deque(maxlen=80)
        self.record("Plant online: 60 Hz, breaker closed." if online
                    else
                    "Plant ready. Start turbine, then arm auto-sync to close onto the grid.")

    def record(self, message):
        self.events.appendleft({"time": datetime.now(timezone.utc).isoformat(), "message": message})

    @property
    def frequency(self):
        return self.rpm / 60.0

    @property
    def grid_ok(self):
        return self.grid_age < 1.0

    @property
    def slip(self):
        return wrap(self.theta - self.grid_theta)

    @property
    def in_sync(self):
        return self.grid_ok and abs(self.frequency - self.grid_f) < SYNC_HZ and abs(self.slip) < SYNC_DEG

    def grid_update(self, f, theta):
        self.grid_f, self.grid_theta, self.grid_age = f, theta, 0.0

    def validate(self, kind, index, value):
        if kind == "analog":
            if index not in (0, 1):
                return "NOT_SUPPORTED"
            if not math.isfinite(value) or not 0 <= value <= 100:
                return "OUT_OF_RANGE"
            if index == 0 and self.autosync:
                # armed auto-sync owns the governor
                return "BLOCKED"  
        elif kind == "binary":
            if index not in (0, 1, 2, 3, 4, 5, 6):
                return "NOT_SUPPORTED"
             # maintenance repair
            if index == 6: 
                if not value:
                    return "NOT_SUPPORTED"
                if not self.damaged or self.rpm > 300:
                    return "BLOCKED"
            if index == 0 and value and self.tripped:
                return "BLOCKED"
            if index == 1 and value:
                if self.tripped or not self.running:
                    return "BLOCKED"
                if self.grid_ok and not self.sync_bypass and not self.in_sync:
                    # sync-check relay
                    return "BLOCKED"  
                if not self.grid_ok and not 59 <= self.frequency <= 61:
                    # dead bus
                    return "BLOCKED"  
            if index == 3 and (not value or self.rpm > 300 or self.damaged):
                return "BLOCKED"
            if index == 4 and value and (self.tripped or not self.running or self.breaker or not self.grid_ok):
                return "BLOCKED"
            if index == 2 and not value:
                return "NOT_SUPPORTED"
        else:
            return "NOT_SUPPORTED"
        return "SUCCESS"

    def command(self, kind, index, value):
        status = self.validate(kind, index, value)
        if status != "SUCCESS":
            self.record(f"DNP3 rejected {kind} {index}: {status}")
            return status
        if kind == "analog":
            setattr(self, "steam" if index == 0 else "load", float(value))
        elif index == 0:
            self.running = bool(value)
            if not value:
                self.breaker = self.autosync = False
        elif index == 1:
            if value and self.grid_ok and abs(self.slip) > DAMAGE_DEG:
                self.damaged = True
                self.trip(f"Out-of-phase close ({self.slip:+.0f} deg): generator damaged")
            else:
                self.breaker = bool(value)
        elif index == 2:
            self.trip("Remote trip")
        elif index == 3:
            self.tripped = False
            self.reason = ""
            self.bad_frequency_time = 0
        elif index == 4:
            self.autosync = bool(value)
        elif index == 5:
            self.sync_bypass = bool(value)
        elif index == 6:
            # generator replaced
            self.__init__()  
            self.record("Maintenance: generator replaced, unit back online")
        self.record(f"DNP3 accepted {kind} {index} = {value}")
        return status

    def trip(self, reason):
        self.running = self.breaker = self.autosync = False
        self.tripped = True
        self.reason = reason
        self.record(f"TRIP: {reason}")

    def tick(self, dt):
        dt = max(0, min(dt, 0.5))
        was_ok = self.grid_ok
        self.grid_age += dt
        self.grid_theta = wrap(self.grid_theta + 360 * (self.grid_f - 60) * dt)
        if was_ok and not self.grid_ok and self.breaker:
            self.breaker = False
            self.record("Grid lost: breaker opened (loss of mains)")
        if self.autosync:
             # run slightly fast so the phase sweeps into the window
            self.steam = self.valve_for(self.grid_f + SYNC_HZ / 2) 
        target_valve = self.steam if self.running and not self.tripped else 0
        self.valve += (target_valve - self.valve) * (1 - math.exp(-dt / 1.0))
        if self.breaker and self.grid_ok:
            # The grid holds speed and phase; surplus steam becomes MW exported (54% = 40 MW, same as the islanded balance).
            self.rpm, self.theta = self.grid_f * 60, self.grid_theta
            self.power = (self.valve - 50) * 10
            self.bad_frequency_time = 0
        else:
            target_rpm = max(0, self.valve * 72 - (self.load * 7.2 if self.breaker else 0))
            self.rpm += (target_rpm - self.rpm) * (1 - math.exp(-dt / 2.5))
            self.theta = wrap(self.theta + 360 * (self.frequency - 60) * dt)
            self.power = self.load * (self.frequency / 60) if self.breaker else 0
            if self.breaker and not 57 <= self.frequency <= 63:
                self.bad_frequency_time += dt
                if self.bad_frequency_time >= 3:
                    self.trip("Frequency outside 57–63 Hz for 3 seconds")
            else:
                self.bad_frequency_time = 0
        if not self.tripped and self.frequency > 66:
            self.trip("Overspeed (>66 Hz)")
        if self.autosync and not self.breaker and self.in_sync:
            self.breaker, self.autosync = True, False
            self.record(f"Auto-sync closed breaker ({self.slip:+.1f} deg)")

    @staticmethod
    def valve_for(f):
        return min(100.0, max(0.0, f / 1.2))  # no-load speed: valve% * 72 rpm = valve% * 1.2 Hz

    def snapshot(self):
        return {"running": self.running, "breaker": self.breaker, "tripped": self.tripped,
                "steam": self.steam, "load": self.load, "valve": round(self.valve, 2),
                "rpm": round(self.rpm, 1), "frequency": round(self.frequency, 2),
                "power": round(self.power, 2), "reason": self.reason,
                "grid_ok": self.grid_ok, "grid_f": round(self.grid_f, 3),
                "slip": round(self.slip, 1) if self.grid_ok else 0.0,
                "autosync": self.autosync, "sync_bypass": self.sync_bypass, "damaged": self.damaged,
                "state": "DAMAGED" if self.damaged else "TRIPPED" if self.tripped else "SUPPLYING LOAD" if self.breaker else "RUNNING" if self.running else "STOPPED",
                "events": list(self.events)}
