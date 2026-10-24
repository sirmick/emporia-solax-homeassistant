# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - 2026-10-16

### Changed
- Charging rules rewritten as a single policy (`charge_policy.py`, pure logic with scenario tests) replacing the
  time-period and power-based controllers. Config moves to a `charge_policy` section; `time_period_policies` and
  `charger_limits` are no longer read (a warning is printed if present).
- Battery first: cars only get solar that would otherwise be exported or throttled; battery discharge, grid import
  and a battery charge reserve all count against the budget.
- Primary charger takes priority; the secondary gets the remainder and now pauses too.
- On/off is limited by a minimum interval between starts; dips are ridden out at the minimum current, and after
  `no_grid_after` a charger stops quickly (it may still restart under the normal start rules if the sun comes back).
- Chargers left on while unplugged are switched off outside the night window.
- The "Use Excess Solar" switch now actually enables/disables solar charging per charger (on at start-up).

### Added
- Per-charger "Charge Policy" sensor showing what the policy is doing.
- `--dry-run`: run against live data without commanding chargers or publishing to MQTT.
- Car-full detection and upward probing when the inverter throttles solar instead of exporting.

### Fixed
- Solar window times were read from the wrong config keys; the night current ignored `max_current`; the on/off
  rate limit never engaged around the minimum current; the fallback rules used the container's UTC clock.

## [1.0.0] - 2025-01-XX

### Added
- Initial release of Emporia Solax Home Assistant Integration
- Real-time polling of Solax solar inverter data
- Automatic EV charger current control based on solar excess power
- Home Assistant MQTT auto-discovery integration
- Comprehensive logging with battery time estimates
- Support for primary/secondary charger priority logic
- Power validation to filter spurious readings
- Time-based charging rules (10am-4pm enable, 4pm+ disable if battery draining)
- Battery reserve allocation based on SOC
- Command-line interface with extensive configuration options

### Technical Details
- Python 3.8+ compatibility
- Dependencies: pyemvue, ha-mqtt-discoverable, paho-mqtt, requests
- MIT License
- GitHub Actions CI/CD pipeline
- Comprehensive documentation and examples

### Known Limitations
- Requires manual configuration of inverter IP and credentials
- No built-in web interface (command-line only)
- Limited to Solax inverters with API access
- Requires Emporia Vue account for charger control