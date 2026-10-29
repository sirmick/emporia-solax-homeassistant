"""EV charge policy: decide on/off and current for each charger.

Pure logic, no I/O: poll.py feeds it inverter + charger readings each cycle and
applies the commands it returns, so the rules can be tested with a fake clock.

Rules (all times local):

* Night window (default 00:10-05:00): every charger ON at ``night_current`` so
  a car plugged in during the window starts straight away. Leaving the house
  battery out of it is the inverter's job (TOU "Battery off" slot).
* Any other time is solar-only, battery first:
    - The budget is what the chargers already draw plus the surplus that would
      otherwise leave the site: export, minus grid import, minus battery
      discharge, minus any shortfall below ``battery_reserve_w`` of charge
      while the battery is under ``battery_full_soc``.
    - A stopped charger only starts at SOC >= ``battery_first_soc``, after the
      budget has covered ``min_current`` for ``start_delay_s``, and no sooner
      than ``min_start_interval_s`` after its previous start.
    - Chargers share the budget in priority order (primary first); a charger
      whose car is full or unplugged gets nothing and is switched off.
    - Watts become amps at each car's measured watts per amp (its draw over
      its set current, learnt while it charges), not at the nominal
      ``voltage``: a car offered 6A may really take ~5A, and judging it at
      ``voltage`` would call a balanced site short of the minimum. ``voltage``
      is the fallback until a car has been seen charging.
    - A running charger below ``min_current`` holds at the minimum for
      ``stop_delay_s`` (rides out a cloud or a house load spike), then stops.
      From ``no_grid_after``, or with the battery under ``battery_stop_soc``,
      it stops after ``late_stop_delay_s`` instead: neither the grid nor a
      low battery should top it up. Either way it may start again under the
      normal start rules, so a passing cloud doesn't end the day, while a
      fading sun simply never covers the minimum again.
    - With the battery full and nothing flowing to or from the grid, the
      inverter may be throttling solar (no-export setups). The policy then
      probes upwards ``probe_step_a`` at a time and backs off as soon as import
      or battery discharge shows up.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, fields
from typing import Dict, List, Optional, Tuple

import pytz


@dataclass
class PolicyConfig:
    timezone: str = "UTC"
    night_start: str = "00:10"
    night_end: str = "05:00"
    night_current: int = 40
    battery_first_soc: int = 95  # solar charging starts only at/above this SOC
    battery_stop_soc: int = 90  # ...and running chargers lose their budget below this
    battery_full_soc: int = 99  # below this the battery keeps battery_reserve_w of charge
    battery_reserve_w: int = 1000
    no_grid_after: str = "15:00"  # from here: stop after late_stop_delay_s, not stop_delay_s
    min_current: int = 6
    max_current: int = 30  # solar cap per charger (night uses night_current)
    voltage: int = 240  # watts per amp until a car's real draw has been measured
    margin_w: int = 100  # surplus deadband: hold the current while 0 <= surplus < margin
    avg_window_s: int = 120  # surplus averaging window
    settle_s: int = 60  # after any command, wait this long before adjusting current again
    start_delay_s: int = 300
    stop_delay_s: int = 1800
    late_stop_delay_s: int = 60
    min_start_interval_s: int = 300
    full_after_s: int = 300  # ON + plugged in + drawing under idle_power_w this long = car full
    idle_power_w: int = 100
    probe_step_a: int = 1
    resend_s: int = 300  # an identical command that didn't take is re-sent at most this often

    @classmethod
    def from_dict(cls, d: dict) -> Tuple["PolicyConfig", List[str]]:
        """Build from a config section; returns (config, unknown keys)."""
        names = {f.name for f in fields(cls)}
        known = {k: v for k, v in d.items() if k in names}
        unknown = sorted(k for k in d if k not in names and not k.startswith("_"))
        return cls(**known), unknown


@dataclass
class Flows:
    """Inverter readings. Signs: grid_w +export/-import, battery_w +charging/-discharging."""

    grid_w: float
    battery_w: float
    soc: int


@dataclass
class ChargerReading:
    name: str
    connected: bool  # a car is plugged in
    on: bool
    current: int
    power_w: float
    max_rate: int = 48
    solar_enabled: bool = True  # the HA "Use Excess Solar" switch


@dataclass
class Command:
    name: str
    on: bool
    current: int
    reason: str


@dataclass
class _State:
    last_start: Optional[float] = None
    above_since: Optional[float] = None  # budget has covered min_current since
    below_since: Optional[float] = None  # running below min_current since
    idle_since: Optional[float] = None  # ON + plugged in but not drawing since
    full: bool = False
    w_per_a: Optional[float] = None  # this car's measured draw per amp of set current
    night: bool = False  # currently on because of the night window
    status: str = ""
    sent: Optional[Tuple[bool, int, float]] = None  # last (on, current, time) sent


def _hhmm(s: str) -> datetime.time:
    return datetime.datetime.strptime(s, "%H:%M").time()


def _in_window(t: datetime.time, start: datetime.time, end: datetime.time) -> bool:
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def _mins(seconds: float) -> str:
    return f"{max(0, seconds) / 60:.0f}m"


class ChargePolicy:
    def __init__(self, cfg: PolicyConfig, priority: List[str]):
        """priority: charger names, most important first (unlisted chargers go last)."""
        self.cfg = cfg
        self.tz = pytz.timezone(cfg.timezone)
        self.priority = list(priority)
        self.night_start = _hhmm(cfg.night_start)
        self.night_end = _hhmm(cfg.night_end)
        self.no_grid_after = _hhmm(cfg.no_grid_after)
        self.states: Dict[str, _State] = {}
        self.samples: List[Tuple[float, float]] = []
        self.last_cmd: Optional[float] = None
        self.surplus_w = 0.0  # last averaged surplus, for status/logging
        self.budget_w = 0.0

    # --- inputs -----------------------------------------------------------

    def instant_surplus(self, f: Flows) -> float:
        """Watts that would leave the site (or be throttled away) right now; negative = deficit."""
        c = self.cfg
        if f.soc < c.battery_full_soc:
            battery = min(0.0, f.battery_w - c.battery_reserve_w)
        else:
            battery = min(0.0, f.battery_w)
        return f.grid_w + battery

    def _flat(self, f: Flows) -> bool:
        """Battery full and nothing moving to/from grid or battery: solar may be throttled."""
        c = self.cfg
        return f.soc >= c.battery_full_soc and abs(f.grid_w) < c.margin_w and abs(f.battery_w) < c.margin_w

    def _learn(self, r: ChargerReading, st: _State, t: float):
        """Update the car's watts per amp from a settled reading while it charges.

        Not at night (cars often cap themselves below night_current) and not
        until settle_s after a command (the draw lags the new current). A car
        capping itself in the day reads low, which over-offers amps for a few
        steps until the current comes down to what it really takes.
        """
        c = self.cfg
        if st.night or not r.on or r.current <= 0 or r.power_w < c.idle_power_w:
            return
        if st.sent is not None and t - st.sent[2] < c.settle_s:
            return
        w = min(1.1 * c.voltage, max(0.5 * c.voltage, r.power_w / r.current))
        st.w_per_a = w if st.w_per_a is None else (st.w_per_a + w) / 2

    def w_per_a(self, name: str) -> float:
        st = self.states.get(name)
        return st.w_per_a if st and st.w_per_a else float(self.cfg.voltage)

    def _ordered(self, chargers: List[ChargerReading]) -> List[ChargerReading]:
        rank = {n: i for i, n in enumerate(self.priority)}
        return sorted(chargers, key=lambda r: rank.get(r.name, len(rank)))

    # --- main entry -------------------------------------------------------

    def step(self, now: datetime.datetime, flows: Flows, chargers: List[ChargerReading]) -> List[Command]:
        c = self.cfg
        t = now.timestamp()
        tod = now.astimezone(self.tz).time()
        for r in chargers:
            self.states.setdefault(r.name, _State())

        self.samples.append((t, self.instant_surplus(flows)))
        self.samples = [(st, v) for st, v in self.samples if t - st <= c.avg_window_s]
        self.surplus_w = sum(v for _, v in self.samples) / len(self.samples)

        if _in_window(tod, self.night_start, self.night_end):
            cmds = self._night(chargers)
        else:
            cmds = self._solar(t, tod, flows, self._ordered(chargers))
        cmds = [cmd for cmd in cmds if self._fresh(cmd, t)]
        if cmds:
            self.last_cmd = t
            self.samples = []  # flows are about to change; average afresh
        return cmds

    def _fresh(self, cmd: Command, t: float) -> bool:
        st = self.states[cmd.name]
        if st.sent and st.sent[:2] == (cmd.on, cmd.current) and t - st.sent[2] < self.cfg.resend_s:
            return False
        st.sent = (cmd.on, cmd.current, t)
        return True

    def _night(self, chargers: List[ChargerReading]) -> List[Command]:
        cmds = []
        for r in chargers:
            st = self.states[r.name]
            st.full = False
            st.night = True
            st.above_since = st.below_since = st.idle_since = None
            amps = min(self.cfg.night_current, r.max_rate)
            st.status = f"night {amps}A" if r.connected else "night (not plugged in)"
            if not r.on or r.current != amps:
                cmds.append(Command(r.name, True, amps, "night window"))
        return cmds

    def _solar(self, t: float, tod: datetime.time, f: Flows, chargers: List[ChargerReading]) -> List[Command]:
        c = self.cfg
        late = tod >= self.no_grid_after
        settled = self.last_cmd is None or t - self.last_cmd >= c.settle_s
        cmds: List[Command] = []

        def off(r: ChargerReading, st: _State, reason: str):
            st.above_since = st.below_since = st.idle_since = None
            st.night = False
            if r.on:
                cmds.append(Command(r.name, False, c.min_current, reason))

        # Car-full detection, watts-per-amp learning, unplug resets.
        for r in chargers:
            st = self.states[r.name]
            if not r.connected:
                st.full = False
                st.idle_since = None
                st.w_per_a = None  # the next car may draw differently
                continue
            self._learn(r, st, t)
            if r.on and r.power_w < c.idle_power_w:
                st.idle_since = st.idle_since if st.idle_since is not None else t
                if t - st.idle_since >= c.full_after_s:
                    st.full = True
            else:
                st.idle_since = None

        candidates = []
        for r in chargers:
            st = self.states[r.name]
            if st.night and r.on:
                st.status = "night window over"
                off(r, st, "night window over")
            elif not r.connected:
                st.status = "not plugged in"
                off(r, st, "not plugged in")
            elif not r.solar_enabled:
                st.status = "solar charging disabled"
                off(r, st, "solar charging disabled")
            elif st.full:
                st.status = "car full"
                off(r, st, "car full")
            else:
                st.night = False
                candidates.append(r)

        # Budget: what the running chargers draw plus the averaged surplus, with a
        # deadband so the current holds steady while we're roughly balanced.
        drawing = sum(r.power_w for r in candidates if r.on)
        s = self.surplus_w
        if s < 0:
            budget = drawing + s
        elif s < c.margin_w:
            budget = drawing
        else:
            budget = drawing + s - c.margin_w
        flat = self._flat(f)
        if f.soc < c.battery_stop_soc:
            budget = 0.0
        self.budget_w = max(0.0, budget)  # for display; a deficit shows as the surplus
        avail = max(0.0, budget)  # watts left to hand out, in priority order
        fast_stop = late or f.soc < c.battery_stop_soc
        probed = False

        for r in candidates:
            st = self.states[r.name]
            cap = min(c.max_current, r.max_rate)
            wpa = self.w_per_a(r.name)
            if r.on:
                share = min(cap, avail / wpa)
                if flat and not probed and s >= 0 and f.soc >= c.battery_stop_soc:
                    share = max(share, min(cap, r.current + c.probe_step_a))
                    probed = True
                avail = max(0.0, avail - share * wpa)
                if share >= c.min_current:
                    st.below_since = None
                    amps = max(c.min_current, min(cap, round(share)))
                    st.status = f"solar {amps}A"
                    if amps != r.current and settled:
                        cmds.append(Command(r.name, True, amps, f"solar budget {budget:.0f}W"))
                    continue
                st.below_since = st.below_since if st.below_since is not None else t
                delay = c.late_stop_delay_s if fast_stop else c.stop_delay_s
                left = delay - (t - st.below_since)
                if left <= 0:
                    st.status = "stopped: not enough solar"
                    reason = f"budget {budget:.0f}W below {c.min_current}A"
                    if late:
                        reason += f" (after {c.no_grid_after}, fast stop)"
                    elif fast_stop:
                        reason += f" (battery under {c.battery_stop_soc}%, fast stop)"
                    off(r, st, reason)
                    continue
                st.status = f"holding {c.min_current}A, stop in {_mins(left)}"
                if r.current != c.min_current:
                    cmds.append(Command(r.name, True, c.min_current, "budget below minimum, holding"))
                continue

            # Stopped charger: may it start?
            st.below_since = None
            since_start = None if st.last_start is None else t - st.last_start
            if f.soc < c.battery_first_soc:
                st.above_since = None
                st.status = f"waiting: battery {f.soc}% < {c.battery_first_soc}%"
                continue
            if since_start is not None and since_start < c.min_start_interval_s:
                st.above_since = None
                st.status = f"waiting: next start in {_mins(c.min_start_interval_s - since_start)}"
                continue
            share = min(cap, avail / wpa)
            if flat and not probed and s >= 0:
                share = max(share, c.min_current)
                probed = True
            if share < c.min_current:
                st.above_since = None
                st.status = f"waiting: surplus {max(0, budget) / 1000:.1f}kW"
                continue
            avail = max(0.0, avail - share * wpa)
            st.above_since = st.above_since if st.above_since is not None else t
            left = c.start_delay_s - (t - st.above_since)
            if left > 0:
                st.status = f"starting in {_mins(left)}"
                continue
            st.above_since = None
            st.last_start = t
            st.status = f"solar {c.min_current}A"
            cmds.append(
                Command(
                    r.name, True, c.min_current, f"start: solar budget {budget:.0f}W" + (" (probe)" if flat else "")
                )
            )
        return cmds

    def status(self, name: str) -> str:
        st = self.states.get(name)
        return st.status if st else ""
