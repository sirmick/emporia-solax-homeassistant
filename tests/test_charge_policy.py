"""Scenario tests for charge_policy against a small simulated site.

The simulated inverter runs self-use: solar feeds the house + cars first, then
the battery (up to its charge rate), then export, unless ``no_export`` is set,
in which case it throttles solar instead. Deficits come from the battery (down
to its floor), then the grid.
"""

import datetime
import os
import sys

import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from charge_policy import ChargePolicy, ChargerReading, Flows, PolicyConfig  # noqa: E402

TZ = pytz.timezone("America/Los_Angeles")
STEP = 10


class Car:
    def __init__(self, plugged=True, full=False, w_per_a=240.0):
        self.plugged = plugged
        self.full = full
        self.w_per_a = w_per_a  # what it really draws per amp offered


class Site:
    def __init__(self, solar=0.0, house=1000.0, soc=96.0, no_export=False, battery_rate=7600.0):
        self.solar = solar
        self.house = house
        self.soc = soc
        self.no_export = no_export
        self.battery_rate = battery_rate
        self.cars = {"Front": Car(), "Back": Car()}
        self.chargers = {n: {"on": False, "current": 6} for n in self.cars}
        self.import_wh = 0.0
        self.battery_out_wh = 0.0

    def draw(self, name):
        ch, car = self.chargers[name], self.cars[name]
        return ch["current"] * car.w_per_a if ch["on"] and car.plugged and not car.full else 0.0

    def flows(self):
        load = self.house + sum(self.draw(n) for n in self.cars)
        spare = self.solar - load
        if spare >= 0:
            room = self.battery_rate if self.soc < 100 else 0.0
            battery = min(spare, room)
            grid = 0.0 if self.no_export else spare - battery
        else:
            battery = -min(-spare, self.battery_rate) if self.soc > 10 else 0.0
            grid = spare - battery
        return grid, battery

    def readings(self):
        return [
            ChargerReading(n, self.cars[n].plugged, self.chargers[n]["on"], self.chargers[n]["current"], self.draw(n))
            for n in self.cars
        ]

    def apply(self, cmds):
        for c in cmds:
            self.chargers[c.name] = {"on": c.on, "current": c.current}


def at(hhmm, day=16):
    h, m = map(int, hhmm.split(":"))
    return TZ.localize(datetime.datetime(2026, 10, day, h, m))


def run(site, policy, start, minutes, log=None):
    """Advance the simulation; returns the list of commands issued."""
    issued = []
    now = start
    for _ in range(int(minutes * 60 / STEP)):
        grid, battery = site.flows()
        if grid < 0:
            site.import_wh += -grid * STEP / 3600
        if battery < 0:
            site.battery_out_wh += -battery * STEP / 3600
        cmds = policy.step(now, Flows(grid, battery, int(site.soc)), site.readings())
        site.apply(cmds)
        issued += [(now, c) for c in cmds]
        if log is not None:
            log.append((now, grid, battery, dict((n, dict(v)) for n, v in site.chargers.items())))
        now += datetime.timedelta(seconds=STEP)
    return issued, now


def make(**over):
    cfg = PolicyConfig(timezone="America/Los_Angeles", **over)
    return ChargePolicy(cfg, ["Front"])


def starts(issued, name):
    return [t for t, c in issued if c.name == name and c.reason.startswith("start")]


def test_night_window_turns_everything_on_including_unplugged():
    site = Site(solar=0, soc=50)
    site.cars["Back"].plugged = False
    p = make()
    run(site, p, at("00:20"), 1)
    assert site.chargers["Front"] == {"on": True, "current": 40}
    assert site.chargers["Back"] == {"on": True, "current": 40}


def test_night_window_end_switches_off_immediately():
    site = Site(solar=0, soc=50)
    p = make()
    run(site, p, at("04:55"), 5.5)
    assert not site.chargers["Front"]["on"] and not site.chargers["Back"]["on"]


def test_night_window_car_refusing_charge_shows_full_not_unplugged():
    p = make()
    readings = [
        ChargerReading("Front", True, True, 40, 0.0, refusing=True),
        ChargerReading("Back", False, True, 40, 0.0),
    ]
    assert p.step(at("02:30"), Flows(0.0, 0.0, 50), readings) == []
    assert p.status("Front") == "night (car full)"
    assert p.status("Back") == "night (not plugged in)"


def test_battery_first_soc_gate():
    site = Site(solar=8000, house=1000, soc=90)
    site.battery_rate = 0  # pretend full-rate limited, so there is export
    p = make()
    issued, _ = run(site, p, at("12:00"), 20)
    assert not starts(issued, "Front")
    assert "battery 90%" in p.status("Front")


def test_no_start_while_battery_is_absorbing_the_surplus():
    site = Site(solar=4000, house=1000, soc=96)  # battery takes the 3kW, nothing exported
    p = make()
    issued, _ = run(site, p, at("12:00"), 20)
    assert not starts(issued, "Front") and not starts(issued, "Back")


def test_front_takes_priority_and_back_gets_the_rest():
    site = Site(solar=11000, house=1000, soc=100)  # 10kW spare once the battery is full
    p = make()
    issued, _ = run(site, p, at("11:00"), 40)
    assert site.chargers["Front"]["on"] and site.chargers["Front"]["current"] == 30
    assert site.chargers["Back"]["on"]
    # 10kW - 7.2kW front - margin leaves ~11A for the back
    assert 9 <= site.chargers["Back"]["current"] <= 12
    grid, _ = site.flows()
    assert grid > -300  # not importing


def test_start_waits_for_sustained_surplus():
    site = Site(solar=3500, house=1000, soc=100)
    p = make()
    issued, now = run(site, p, at("11:00"), 4.5)
    assert not starts(issued, "Front")
    issued, _ = run(site, p, now, 2)
    assert starts(issued, "Front")


def test_not_enough_for_minimum_never_starts():
    site = Site(solar=2200, house=1000, soc=100)  # 1.2kW spare < 6A
    p = make()
    issued, _ = run(site, p, at("11:00"), 60)
    assert not starts(issued, "Front") and not starts(issued, "Back")


def test_cloud_before_cutoff_holds_30min_then_stops_and_restarts_after_5min():
    site = Site(solar=5000, house=1000, soc=100)
    p = make()
    site.cars["Back"].plugged = False
    issued, now = run(site, p, at("11:00"), 15)
    assert site.chargers["Front"]["on"]
    site.solar = 1500  # cloud
    issued, now = run(site, p, now, 25)
    assert site.chargers["Front"] == {"on": True, "current": 6}  # holding the minimum
    issued, now = run(site, p, now, 6)
    assert not site.chargers["Front"]["on"]
    stopped = now
    site.solar = 5000  # sun back
    issued, now = run(site, p, now, 15)
    first = starts(issued, "Front")
    # 5 min of sustained surplus; the 5 min since the last start long passed
    assert first and 4 * 60 <= (first[0] - stopped).total_seconds() <= 7 * 60
    all_starts = sorted(starts(issued, "Front"))
    assert all((b - a).total_seconds() >= 300 for a, b in zip(all_starts, all_starts[1:]))


def test_house_load_spike_before_cutoff_does_not_stop_the_car():
    # 2026-10-28 14:15: a ~5 min house spike pushed the site into import and
    # the 5 min hold stopped the car, then ~1.8kW was exported for 30 min.
    site = Site(solar=2800, house=1000, soc=100)
    site.cars["Back"].plugged = False
    p = make()
    _, now = run(site, p, at("13:30"), 15)
    assert site.chargers["Front"]["on"]
    site.house = 2300
    _, now = run(site, p, now, 6)
    assert site.chargers["Front"]["on"]
    site.house = 1000
    _, now = run(site, p, now, 10)
    assert site.chargers["Front"]["on"]


def test_car_drawing_under_nominal_at_6a_keeps_running_after_cutoff():
    # 2026-10-28 15:01: the Front car takes ~1230W at 6A (205 W/A). Judged at
    # 240 W/A a balanced site looked short of 6A and the fast stop fired.
    site = Site(solar=3000, house=1100, soc=100)
    site.cars["Front"].w_per_a = 205.0
    site.cars["Back"].plugged = False
    p = make()
    issued, now = run(site, p, at("14:30"), 15)
    assert starts(issued, "Front")
    site.solar = 2600  # ~270W spare at 6A x 205 W/A, ~40W short at 240 W/A
    issued, now = run(site, p, now, 30)  # well past 15:00
    assert not [c for _, c in issued if not c.on]
    assert site.chargers["Front"]["on"] and 6 <= site.chargers["Front"]["current"] <= 8
    assert 195 <= p.w_per_a("Front") <= 215


def test_start_uses_the_cars_measured_draw():
    site = Site(solar=2400, house=1000, soc=100)  # 1.4kW spare: < 6A x 240, > 6A x 205
    site.cars["Front"].w_per_a = 205.0
    site.cars["Back"].plugged = False
    p = make()
    issued, _ = run(site, p, at("11:00"), 30)
    assert not starts(issued, "Front")  # unknown car: judged at 240 W/A
    p.states["Front"].w_per_a = 205.0  # as learnt on an earlier run
    issued, _ = run(site, p, at("11:30"), 10)
    assert starts(issued, "Front")
    site.cars["Front"].plugged = False
    run(site, p, at("11:40"), 1)
    assert p.w_per_a("Front") == 240.0  # forgotten on unplug


def test_after_cutoff_stops_fast_but_restarts_when_the_sun_returns():
    site = Site(solar=5000, house=1000, soc=100)
    site.cars["Back"].plugged = False
    p = make()
    issued, now = run(site, p, at("15:30"), 15)
    started = starts(issued, "Front")[0]
    site.solar = 1500  # a cloud
    before = site.battery_out_wh + site.import_wh
    _, now = run(site, p, now, 3)
    assert not site.chargers["Front"]["on"]
    assert site.battery_out_wh + site.import_wh - before < 300  # ~1 minute at the minimum, not 5
    site.solar = 6000  # sun pops out again; exporting it would be a waste
    issued, now = run(site, p, now, 60)
    assert site.chargers["Front"]["on"]
    again = starts(issued, "Front")
    assert again and (again[0] - started).total_seconds() >= 300


def test_fading_sun_after_cutoff_stays_off():
    site = Site(solar=5000, house=1000, soc=100)
    site.cars["Back"].plugged = False
    p = make()
    _, now = run(site, p, at("16:00"), 15)
    assert site.chargers["Front"]["on"]
    for solar in (3000, 2000, 1500, 800, 300, 0):
        site.solar = solar
        issued, now = run(site, p, now, 20)
    assert not site.chargers["Front"]["on"]
    assert not starts(issued, "Front")
    _, now = run(site, p, at("00:15", day=17), 1)
    assert site.chargers["Front"]["on"]


def test_full_car_hands_its_share_to_the_other_charger():
    site = Site(solar=6000, house=1000, soc=100)
    p = make()
    _, now = run(site, p, at("11:00"), 20)
    assert site.chargers["Front"]["on"] and site.chargers["Front"]["current"] >= 19
    site.cars["Front"].full = True
    _, now = run(site, p, now, 40)
    assert not site.chargers["Front"]["on"]
    assert p.status("Front") == "car full"
    assert site.chargers["Back"]["on"] and site.chargers["Back"]["current"] >= 18


def test_idle_unplugged_charger_left_on_is_switched_off():
    site = Site(solar=0, soc=50)
    site.cars["Front"].plugged = False
    site.chargers["Front"] = {"on": True, "current": 40}
    p = make()
    run(site, p, at("09:00"), 1)
    assert not site.chargers["Front"]["on"]


def test_solar_switch_off_excludes_charger():
    site = Site(solar=8000, house=1000, soc=100)
    p = make()
    now = at("11:00")
    for _ in range(200):
        grid, battery = site.flows()
        readings = site.readings()
        readings[0].solar_enabled = False  # Front
        site.apply(p.step(now, Flows(grid, battery, 100), readings))
        now += datetime.timedelta(seconds=STEP)
    assert not site.chargers["Front"]["on"]
    assert site.chargers["Back"]["on"]


def test_probes_up_when_inverter_throttles_instead_of_exporting():
    site = Site(solar=6000, house=1000, soc=100, no_export=True)  # 5kW really available
    p = make()
    log = []
    run(site, p, at("11:00"), 60, log)
    assert site.chargers["Front"]["on"]
    # 5kW / 240V ~ 20A; front should climb near it, back left with too little to start
    assert 17 <= site.chargers["Front"]["current"] <= 21
    late = [g for t, g, b, ch in log[-120:]]
    assert sum(1 for g in late if g < -100) < 30  # mostly not importing


def test_soc_drop_below_stop_threshold_stops_charging():
    site = Site(solar=8000, house=1000, soc=100)
    site.battery_rate = 0
    p = make()
    _, now = run(site, p, at("11:00"), 20)
    assert site.chargers["Front"]["on"]
    site.soc = 85
    _, now = run(site, p, now, 6)
    assert not site.chargers["Front"]["on"]
