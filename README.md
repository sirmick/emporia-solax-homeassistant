# Emporia Solax Home Assistant Integration

A Python script that polls data from a Solax solar inverter and Emporia Vue energy monitor to automatically control EV charger current based on available solar excess power. All data is published to MQTT for seamless Home Assistant integration.

## Features

- **Real-time Solar Monitoring**: Collects live data from Solax inverters including solar production, battery status, and grid interaction
- **EV Charger Control**: Automatically adjusts Emporia EV charger current based on solar excess power
- **Battery First**: The cars only get solar that would otherwise be exported (or throttled away), never the battery's charge
- **Home Assistant Integration**: Publishes all metrics to MQTT with auto-discovery for easy HA setup
- **Charger Priority**: Multiple chargers share the solar surplus, primary first
- **Comprehensive Logging**: Detailed status logging with battery time estimates and power flow visualization
- **Power Validation**: Filters out spurious readings to ensure reliable operation

## Installation

### Prerequisites

- Python 3.8 or higher
- Access to Solax inverter API
- Emporia Vue account with EV charger
- MQTT broker (e.g., Mosquitto)

### Setup

1. Clone this repository:
   ```bash
   git clone https://github.com/sirmick/emporia-solax-homeassistant.git
   cd emporia-solax-homeassistant
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Configure Emporia credentials:
   - Run the script once to generate initial token storage
   - Or manually create `keys.json` with your Emporia credentials

## Configuration

### Configuration File

You can configure the script using a JSON configuration file (`config.json`):

```json
{
  "solax": {
    "ip_address": "192.168.88.123",
    "serial_number": "XTR789BZY42"
  },
  "mqtt": {
    "broker": "mqtt.example.home",
    "username": "mqttuser",
    "password": "secretpass"
  },
  "chargers": {
    "primary_charger": "Garage Charger"
  },
  "charge_policy": {
    "timezone": "Europe/London",
    "night_start": "00:10",
    "night_end": "05:00",
    "night_current": 40,
    "battery_first_soc": 95,
    "battery_stop_soc": 90,
    "battery_full_soc": 99,
    "battery_reserve_w": 1000,
    "no_grid_after": "15:00",
    "min_current": 6,
    "max_current": 30,
    "min_start_interval_s": 1800
  },
  "system": {
    "battery_capacity": 20.0,
    "min_soc": 20,
    "power_avg_window": 5,
    "max_power_threshold": 50000,
    "sleep_interval": 10,
    "creds_file": "keys.json",
    "detailed_log": false
  }
}
```

The `solax`, `mqtt`, `chargers` and `system` basics also have command-line arguments (see below), which take
precedence over the file. The charging rules live in `charge_policy` only; every key is optional.

#### `charge_policy` keys

| Key | Default | Meaning |
|---|---|---|
| `timezone` | `UTC` | All times below are local to this zone |
| `night_start` / `night_end` | `00:10` / `05:00` | Night window: every charger on at `night_current` |
| `night_current` | `40` | Amps per charger in the night window (capped at the charger's own maximum) |
| `battery_first_soc` | `95` | A stopped charger only starts on solar at or above this battery SOC |
| `battery_stop_soc` | `90` | Below this SOC running chargers lose their solar budget |
| `battery_full_soc` | `99` | Below this, the battery keeps at least `battery_reserve_w` of charge before the cars get any |
| `battery_reserve_w` | `1000` | See above |
| `no_grid_after` | `15:00` | From here until midnight: stop quickly (after `late_stop_delay_s`) when solar can't cover the minimum |
| `min_current` / `max_current` | `6` / `30` | Solar current range per charger |
| `voltage` | `240` | Used to turn watts into amps |
| `margin_w` | `100` | Deadband: the current holds while the surplus is between 0 and this |
| `avg_window_s` | `120` | Surplus averaging window |
| `settle_s` | `60` | After any command, wait this long before adjusting current again |
| `start_delay_s` | `300` | Surplus must cover `min_current` this long before a charger starts |
| `stop_delay_s` | `300` | Before `no_grid_after`: how long a charger holds at the minimum through a dip before stopping |
| `late_stop_delay_s` | `60` | The same, after `no_grid_after` |
| `min_start_interval_s` | `1800` | Minimum time between two starts of the same charger |
| `full_after_s` / `idle_power_w` | `300` / `100` | A charger that is on and plugged in but draws under `idle_power_w` this long is treated as "car full" until unplugged or the next night |
| `probe_step_a` | `1` | Step used to probe upwards when the inverter may be throttling solar (see below) |
| `resend_s` | `300` | An identical command that didn't take effect is re-sent at most this often |

### Emporia Vue Setup

Create a `keys.json` file with your Emporia Vue login credentials:

```json
{
  "username": "my.email@example.org",
  "password": "my-secure-emporia-password"
}
```

The script will automatically handle token storage and refresh.

### MQTT Configuration

The script connects to an MQTT broker for Home Assistant integration. Configure the broker details via command-line arguments or the configuration file.

## Usage

### Basic Command

```bash
python poll.py <inverter_ip> <serial_number> <mqtt_broker> <primary_charger_name> [options]
```

### Arguments

- `inverter_ip`: IP address of your Solax inverter (e.g., 192.168.1.100)
- `serial_number`: Serial number of your inverter (used as password)
- `mqtt_broker`: MQTT broker hostname or IP
- `primary_charger_name`: Name of the primary EV charger in Emporia app

### Options

#### Basic Options
- `-u, --username`: MQTT username (default: 'a')
- `-p, --password`: MQTT password (default: 'a')
- `-s, --sleep`: Poll interval in seconds (default: 10)
- `-c, --creds-file`: Emporia credentials file (default: 'keys.json')
- `-v, --verbose`: Enable verbose logging
- `--detailed-log`: Enable detailed JSON logging to poll_log.json (off by default)
- `--battery-capacity`: Battery capacity in kWh (default: 20.0)
- `--min-soc`: Minimum battery SOC threshold (default: 30)
- `--power-avg-window`: Battery power averaging window in minutes (default: 5)
- `--max-power-threshold`: Maximum valid power reading in watts (default: 50000)
- `--timezone`: Timezone for timestamps (e.g., 'America/New_York', 'Europe/London')
- `--config`: Path to configuration JSON file (default: 'config.json')

- `--dry-run`: Read everything and log what the policy would do, without commanding the chargers or publishing to
  MQTT. Safe to run next to a live instance.

### Examples

#### Using Command-line Arguments

```bash
python poll.py inverter.local XTR789BZY42 mqtt.local "BMW i4" \
  --username homeassistant --password securepass \
  --battery-capacity 25.0 --min-soc 20 --verbose
```

#### Using Configuration File

```bash
# Create config.json with your settings first
python poll.py --config my-solar-setup.json --verbose
```

#### Mixed Mode

```bash
# Use config file but override specific options
python poll.py --config config.json \
  --timezone "America/Chicago" --dry-run
```

## MQTT Topics

The script publishes data to MQTT with Home Assistant auto-discovery. Main topics:

### Solar Inverter Data
- `homeassistant/sensor/solax/power_from_solar`
- `homeassistant/sensor/solax/power_battery`
- `homeassistant/sensor/solax/battery_soc`
- `homeassistant/sensor/solax/battery_voltage`
- `homeassistant/sensor/solax/battery_temperature`
- And many more...

### EV Charger Data
- `homeassistant/sensor/<charger_name>/current`
- `homeassistant/sensor/<charger_name>/power`
- `homeassistant/sensor/<charger_name>/charge_policy` (what the policy is doing, e.g. `solar 14A`,
  `waiting: battery 87% < 95%`, `stopped: not enough solar`)
- `homeassistant/switch/<charger_name>/use_excess_solar` (on by default at start-up; off keeps that charger out of
  solar charging, the night window still applies)

### Battery Time Estimates
- `homeassistant/sensor/solax/battery_time_to_charged`
- `homeassistant/sensor/solax/battery_time_to_depleted`

## How It Works

Every `sleep_interval` seconds the script reads the inverter and the chargers, publishes everything to MQTT, and asks
the charge policy (`charge_policy.py`, pure logic with unit tests) what each charger should do.

**Night window** (default 00:10-05:00): every charger is switched on at `night_current`, plugged in or not, so a car
connected during the window starts straight away. Keeping the house battery out of it is the inverter's job (for
example a Solax TOU "Battery off" slot over the same hours).

**The rest of the day is solar only, battery first:**

1. **Surplus** = grid export − grid import − battery discharge − any shortfall below `battery_reserve_w` of battery
   charge (while SOC < `battery_full_soc`), averaged over `avg_window_s`. In self-use mode the inverter feeds loads
   before the battery, so this is what stops a car from quietly taking the battery's charge.
2. **Budget** = what the chargers are drawing + the surplus (less `margin_w`), or nothing below `battery_stop_soc`.
3. Chargers share the budget in priority order, the primary first. A charger with an unplugged or full car, or with
   its "Use Excess Solar" switch off, gets nothing and is switched off (so plugging a car in never starts an
   unplanned charge).
4. A stopped charger starts at `min_current` once SOC ≥ `battery_first_soc`, its share has covered `min_current` for
   `start_delay_s`, and at least `min_start_interval_s` has passed since its last start. It then follows its share.
5. A running charger whose share drops below `min_current` holds at the minimum for `stop_delay_s` (rides out a
   cloud), then stops.
6. From `no_grid_after` until midnight it stops after `late_stop_delay_s` instead, so the grid doesn't top the car
   up. It can still start again under rule 4 if the sun comes back (a passing cloud doesn't end the day); as the
   sun fades the share simply never covers the minimum again.
7. If the battery is full and nothing is flowing to or from the grid or battery, the inverter may be throttling
   solar (export-limited setups). The policy then probes upwards `probe_step_a` amps at a time and backs off as soon
   as import or battery discharge appears.

## Logging Output

The script provides comprehensive status logging:

```
[14:22:35] 🔋 82% (0.7kW, 24°C) ⏱️ Full: 01:45 (min 30%) 🔄 0.0kW ☀️ 4.8kW 🏠 1.9kW 🏠➡️ 2.2kW ⚡ 1.4kW 🚗 BMW i4: 🟢⚡✅ 18A/4.1kW
```

This shows battery status, time estimates, power flows, and charger states.

## Troubleshooting

### Common Issues

1. **Connection Errors**: Check IP address and network connectivity to inverter
2. **Authentication Failures**: Verify Emporia credentials and charger names
3. **MQTT Connection**: Ensure broker is running and credentials are correct
4. **Spurious Readings**: The script filters out readings above 50kW threshold

### Verbose Mode

Use `--verbose` flag for detailed debugging information:

```bash
python poll.py ... --verbose
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Disclaimer

This software is provided as-is. Use at your own risk. Ensure proper electrical safety measures when working with solar and EV charging systems.