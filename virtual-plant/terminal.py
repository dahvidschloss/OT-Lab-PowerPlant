import os
import sys
import time

INNER = 74  # printable width between the box borders
BOX_UNICODE = {"tl": "┌", 
               "tr": "┐", 
               "bl": "└", 
               "br": "┘", 
               "h": "─", 
               "v": "│", 
               "ml": "├", 
               "mr": "┤",
               "tee": "├", 
               "end": "└", 
               "sep": "│", 
               "full": "█", 
               "empty": "·"}
# Redirected output (docker logs, files) may not be UTF-8 on Windows.
BOX_ASCII = {"tl": "+", 
             "tr": "+", 
             "bl": "+", 
             "br": "+", 
             "h": "-", 
             "v": "|", 
             "ml": "+", 
             "mr": "+",
             "tee": "|-", 
             "end": "|_", 
             "sep": "|", 
             "full": "#", 
             "empty": "."}
C = {"red": "\033[31m\033[1m", 
     "yellow": "\033[33m", 
     "green": "\033[32m", 
     "dim": "\033[2m", 
     "bold": "\033[1m"}


def frame(state, writes, reads, poll_age, endpoint='', color=False):
    b = BOX_UNICODE if color else BOX_ASCII

    def paint(text, c):
        return f'{C[c]}{text}\033[0m' if color and c and text else text

    def line(*parts):
        plain = ''.join(text for text, _ in parts)
        return b["v"] + ''.join(paint(text, c) for text, c in parts) + ' ' * max(0, INNER - len(plain)) + b["v"]

    def rule(left, title, right):
        head = left + b["h"] * 2 + (f' {title} ' if title else '')
        return left + b["h"] * 2 + (f' {paint(title, "bold")} ' if title else '') + b["h"] * max(0, INNER + 1 - len(head)) + right

    def row(tree, label, value, c='', note=''):
        return line((f'  {b[tree]:<2} {label:<12} {b["sep"]} ', ''), (f'{value:<28}' if note else value, c), (note, 'dim'))

    blank = line()
    if state['damaged']:
        status, status_c = 'DAMAGED', 'red'
    elif state['tripped']:
        status, status_c = 'TRIPPED', 'red'
    elif not state['running']:
        status, status_c = 'STOPPED', 'dim'
    elif not state['breaker'] and state['frequency'] < 59:
        status, status_c = 'STARTING', 'yellow'
    else:
        status, status_c = 'RUNNING', 'green'
    if poll_age is None or poll_age > 5:
        poll, poll_c = ('never' if poll_age is None else f'{poll_age:.1f} s ago') + '  STALE', 'red'
    else:
        poll, poll_c = f'{poll_age:.1f} s ago', 'green'
    tied = state['breaker'] and state['grid_ok']
    off_band = state['breaker'] and not tied and not 57 <= state['frequency'] <= 63
    if tied:
        slip, slip_c = 'locked to grid', 'green'
    elif state['grid_ok']:
        df = state['frequency'] - state['grid_f']
        slip = f'{df:+6.2f} Hz {state["slip"]:+5.0f} deg'
        slip_c = 'green' if abs(df) < 0.1 and abs(state['slip']) < 10 else ''
    else:
        slip, slip_c = '-', 'dim'
    filled = round(state['valve'] / 5)
    bar = b["full"] * filled + b["empty"] * (20 - filled)

    lines = [
        rule(b["tl"], 'VIRTUAL POWER PLANT', b["tr"]), blank,
        row('tee', 'state', status, status_c),
        row('tee', 'last poll', poll, poll_c, 'stale after 5 s'),
        row('tee', 'reads', f'{reads:,}'),
        row('end', 'outstation', f'10 @ {endpoint}' if endpoint else '10'),
        blank, rule(b["ml"], 'TURBINE / GENERATOR', b["mr"]), blank,
        row('tee', 'valve', f'{bar} {state["valve"]:3.0f} %', '', f'command {state["steam"]:.0f} %'),
        row('tee', 'speed', f'{state["rpm"]:6.0f} rpm'),
        row('tee', 'frequency', f'{state["frequency"]:6.2f} Hz', 'red' if off_band else '', 'trip >66, 57-63 on load'),
        row('tee', 'output', f'{state["power"]:6.1f} MW', '', 'export to grid' if tied else f'local load {state["load"]:.0f} %'),
        row('end', 'breaker', 'CLOSED' if state['breaker'] else 'OPEN', 'green' if state['breaker'] else 'dim'),
        blank, rule(b["ml"], 'GRID TIE', b["mr"]), blank,
        row('tee', 'grid', f'{state["grid_f"]:6.2f} Hz' if state['grid_ok'] else 'not connected', 'green' if state['grid_ok'] else 'dim', 'connected' if state['grid_ok'] else 'islanded'),
        row('tee', 'slip', slip, slip_c, 'window 0.10 Hz, 10 deg'),
        row('tee', 'auto-sync', 'ARMED' if state['autosync'] else 'off', 'yellow' if state['autosync'] else 'dim'),
        row('end', 'sync check', 'BYPASSED' if state['sync_bypass'] else 'active', 'red' if state['sync_bypass'] else 'dim'), blank, rule(b["ml"], 'PROTECTION', b["mr"]), blank,
        row('end' if not off_band else 'tee', 'trip', state['reason'] if state['tripped'] else 'none', 'red' if state['tripped'] else 'dim'),
    ]
    if off_band:
        lines.append(row('end', 'alarm', 'frequency out of band', 'red', 'trips after 3 s'))
    lines += [blank, rule(b["ml"], 'DNP3 WRITES', b["mr"]), blank, line((f' {"time":<8} {"source":<15} {"point":<14} {"value":<8} {"result":<6} effect', 'dim'))]
    for stamp, ip, point, change, verdict, effect in writes:
        lines.append(line((f' {stamp:<8} {ip[:15]:<15} {point:<14} {change:<8} ', ''), (f'{verdict:<6}', 'red' if verdict == 'REJECT' else 'green'), (f' {effect}', '')))
    if not writes:
        lines.append(line((' none yet', 'dim')))
    lines += [blank, b["bl"] + b["h"] * INNER + b["br"]]
    return '\n'.join(lines)


class TerminalDisplay:
    def __init__(self, station, endpoint='', quiet=False, stream=None):
        self.station, self.endpoint, self.quiet = station, endpoint, quiet
        self.stream = stream or sys.stdout
        self.animated = self.stream.isatty()
        self.last_draw = float('-inf')
        self.last_text, self.last_write = None, float('-inf')
        if self.animated and not quiet:
            # enables ANSI escape handling in the Windows console
            os.system('')  
            self.stream.write('\033[2J')

    def draw(self, state, force=False):
        if self.quiet:
            return
        now = time.monotonic()
        if not force and now - self.last_draw < (0.5 if self.animated else 5):
            return
        self.last_draw = now
        last = self.station.last_request
        text = frame(state, self.station.writes, self.station.reads, None if last is None else now - last, self.endpoint, self.animated)
        # Repaint unchanged frames every 2 s anyway
        if text == self.last_text and now - self.last_write < 2:
            return
        self.last_text, self.last_write = text, now
        if self.animated:
            # ?2026 = synchronized update 
            # ?25 = hide cursor while drawing
            self.stream.write('\033[?2026h\033[?25l\033[H' + ''.join(line + '\033[K\n' for line in text.splitlines()) + '\033[J\033[?25h\033[?2026l')
        else:
            self.stream.write(text + '\n\n')
        self.stream.flush()
