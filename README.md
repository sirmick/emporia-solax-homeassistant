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

### Emporia Vue Setup

Create a `keys.json` file with your Emporia Vue login credentials:

```json
{
  "username": "your_emporia_email@example.com",
  "password": "your_emporia_password"
}
```

The script will automatically handle token storage and refresh.

### MQTT Configuration

The script connects to an MQTT broker for Home Assistant integration. Configure the broker details via command-line arguments.

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

- `-u, --username`: MQTT username (default: 'a')
- `-p, --password`: MQTT password (default: 'a')
- `-s, --sleep`: Poll interval in seconds (default: 10)
- `-c, --creds-file`: Emporia credentials file (default: 'keys.json')
- `-v, --verbose`: Enable verbose logging
- `--battery-capacity`: Battery capacity in kWh (default: 20.0)
- `--min-soc`: Minimum battery SOC threshold (default: 30)
- `--power-avg-window`: Battery power averaging window in minutes (default: 5)
- `--max-power-threshold`: Maximum valid power reading in watts (default: 50000)

### Example

```bash
python poll.py 192.168.1.100 SSAXHKSYAE 192.168.1.10 "Tesla Model 3" \
  --username homeassistant --password mypassword \
  --battery-capacity 25.0 --min-soc 20 --verbose
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
6. **Time-based Rules**: Enables charging only between 10am-4pm, disables after 4pm if battery draining

## Logging Output

The script provides comprehensive status logging:

```
[19:37:42] 🔋 85% (0.5kW, 25°C) ⏱️ Full: 02:15 (min 30%) 🔄 0.0kW ☀️ 5.2kW 🏠 2.1kW 🏠➡️ 1.8kW ⚡ 1.3kW 🚗 Tesla Model 3: 🟢⚡✅ 16A/3.7kW
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