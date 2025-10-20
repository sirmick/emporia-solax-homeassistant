# Emporia Solax Home Assistant Integration

A Python script that polls data from a Solax solar inverter and Emporia Vue energy monitor to automatically control EV charger current based on available solar excess power. All data is published to MQTT for seamless Home Assistant integration.

## Features

- **Real-time Solar Monitoring**: Collects live data from Solax inverters including solar production, battery status, and grid interaction
- **EV Charger Control**: Automatically adjusts Emporia EV charger current based on solar excess power
- **Smart Battery Management**: Reserves battery power for charging when SOC is low, optimizes for solar self-consumption
- **Home Assistant Integration**: Publishes all metrics to MQTT with auto-discovery for easy HA setup
- **Primary/Secondary Charger Support**: Handles multiple EV chargers with priority logic
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
  "time_based_behavior": {
    "switch_on_time": "11:00",
    "switch_off_time": "18:00",
    "fixed_charge_start": "00:10",
    "fixed_charge_end": "06:00",
    "fixed_charge_current": 40,
    "min_excess_threshold": 1440,
    "battery_soc_threshold": 85,
    "timezone": "Europe/London"
  },
  "charger_limits": {
    "max_current": 30,
    "min_current": 6,
    "on_to_off_lockout": 60,
    "off_to_on_lockout": 240
  },
  "system": {
    "battery_capacity": 20.0,
    "min_soc": 20,
    "power_avg_window": 5,
    "max_power_threshold": 50000,
    "sleep_interval": 10,
    "creds_file": "keys.json",
    "timezone": "Europe/London",
    "bus_maximum": 7000,
    "buffer": 100
  }
}
```

All options in the configuration file have corresponding command-line arguments. Command-line arguments take precedence over configuration file settings.

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

#### Charger Limits Options
- `--max-current`: Maximum charging current in amps (default: 30)
- `--min-current`: Minimum charging current in amps (default: 6)
- `--bus-maximum`: Maximum power the AC bus can handle in watts (default: 7000)
- `--buffer`: Power buffer in watts to maintain as safety margin (default: 100)
- `--state-change-min-interval`: Minimum time in minutes between charger on/off state changes (default: 10)

#### Time-Based Behavior Options
- `--switch-on-time`: Time to enable daytime automated charging (e.g., '11:00')
- `--switch-off-time`: Time to disable daytime automated charging (e.g., '18:00')
- `--fixed-charge-start`: Start time for unrestricted charging period (e.g., '00:10')
- `--fixed-charge-end`: End time for unrestricted charging period (e.g., '06:00')
- `--fixed-charge-current`: Current for unrestricted charging period in amps (default: 40)
- `--min-excess-threshold`: Minimum excess power threshold in watts for daytime automated charging (default: 1440)
- `--battery-soc-threshold`: Battery SOC threshold in percent for enabling daytime automated charging (default: 85)

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
  --timezone "America/Chicago" --max-current 32 --verbose
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
- `homeassistant/switch/<charger_name>/use_excess_solar`

### Battery Time Estimates
- `homeassistant/sensor/solax/battery_time_to_charged`
- `homeassistant/sensor/solax/battery_time_to_depleted`

## How It Works

1. **Data Collection**: Polls Solax inverter and Emporia Vue API every 10 seconds
2. **Power Calculation**: Calculates available solar excess after house load and battery reserve
3. **Charger Control**: Adjusts EV charger current based on excess power availability
4. **Priority Logic**: Primary chargers get priority, secondary chargers share remaining power
5. **Battery Protection**: Reserves power for battery charging when SOC is below thresholds
6. **Time-based Rules**: Two distinct charging modes based on time of day:
   - **Unrestricted Charging Period**: During nighttime hours (default 12:10am-6am), charges at a fixed rate (default 40A) regardless of solar production or battery status. Ideal for off-peak electricity rates.
   - **Daytime Automated Charging**: During daylight hours (default 11am-6pm), operates with these rules:
     * Only enables charging when BOTH conditions are true:
       1. Excess solar power exceeds minimum threshold (default 1440W)
       2. Battery SOC is above minimum threshold (default 85%)
     * Current is calculated dynamically based on available excess power
     * If available power drops below minimum current threshold, charging is paused
     * After switch-off time (default 6pm), charging is disabled if battery begins discharging
   - **Outside Hours**: Charging is disabled by default outside of these specific time windows unless manually controlled.

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