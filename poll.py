"""Poll Solax solar inverter and Emporia EV charger data and control charging based on solar production.

This script connects to a Solax solar inverter and Emporia Vue energy monitor to:
- Collect real-time solar production, battery, and grid metrics
- Monitor EV charger status and power usage
- Automatically adjust charger current based on available solar excess
- Publish all data to MQTT for Home Assistant integration
"""

import argparse
import datetime
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import pyemvue
import pytz
import requests
from ha_mqtt_discoverable import Settings, DeviceInfo
from ha_mqtt_discoverable.sensors import Sensor, SensorInfo, Switch, SwitchInfo
from paho.mqtt.client import Client, MQTTMessage
from pyemvue.enums import Scale, Unit

# Log file for JSON logging
LOG_FILE = 'poll_log.json'


def unsigned_8_bit(value: int) -> int:
    """Convert value to unsigned 8-bit integer (0-255).
    
    Args:
        value: Input integer value
        
    Returns:
        Value modulo 256
    """
    return value % 256


def signed_16_bit(value: int) -> int:
    """Convert value to signed 16-bit integer (-32768 to 32767).
    
    Args:
        value: Input integer value
        
    Returns:
        Signed 16-bit representation
    """
    if value > 32767:
        return value - 65536
    return value


def unsigned_32_bit(low_word: int, high_word: int) -> int:
    """Combine two 16-bit words into unsigned 32-bit integer.
    
    Args:
        low_word: Lower 16 bits
        high_word: Higher 16 bits
        
    Returns:
        32-bit unsigned integer
    """
    return (high_word * 65536) + low_word


def signed_32_bit(low_word: int, high_word: int) -> int:
    """Combine two 16-bit words into signed 32-bit integer.
    
    Args:
        low_word: Lower 16 bits
        high_word: Higher 16 bits
        
    Returns:
        32-bit signed integer
    """
    if high_word < 32768:
        return (65536 * high_word) + low_word
    return low_word + (65536 * high_word) - 4294967296


def get_inverter_data(ip_address: str, serial_number: str) -> dict:
    """Fetch real-time data from Solax inverter.
    
    Args:
        ip_address: IP address of the inverter
        serial_number: Serial number used as password
        
    Returns:
        Dictionary of raw inverter data or None if error occurs
    """
    url = f"http://{ip_address}/"
    payload = {
        "optType": "ReadRealTimeData",
        "pwd": serial_number
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded"
    }

    try:
        response = requests.post(url, data=payload, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        if hasattr(get_inverter_data, 'verbose') and get_inverter_data.verbose:
            print(f"[debug] Inverter API call successful to {ip_address}")
            print(f"[debug] Response status: {response.status_code}")
            print(f"[debug] Data array length: {len(data.get('Data', []))}")
        return data
    except requests.exceptions.Timeout:
        print(f"Error: Connection to {ip_address} timed out.", file=sys.stderr)
        return None
    except requests.exceptions.ConnectionError:
        print(f"Error: Could not connect to {ip_address}. Check IP address and network.", file=sys.stderr)
        return None
    except requests.exceptions.HTTPError as e:
        print(f"Error: HTTP request failed - {e}", file=sys.stderr)
        return None
    except json.JSONDecodeError:
        print(f"Error: Failed to decode JSON from response. Response: {response.text}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"An unexpected error occurred: {e}", file=sys.stderr)
        return None


def positive(value: float) -> float:
    """Return value if positive, otherwise return 0.
    
    Args:
        value: Input value
        
    Returns:
        value if >= 0, else 0
    """
    return value if value >= 0 else 0


def invert_positive(value: float) -> float:
    """Invert sign of value and return if positive, else return 0.
    
    Args:
        value: Input value
        
    Returns:
        -value if >= 0, else 0
    """
    value = -1 * value
    return value if value >= 0 else 0


class PowerValidator:
    """Validates power readings and stores last valid values.
    
    This class helps filter out spurious power readings by comparing them
    against a configurable maximum threshold. If a reading exceeds the threshold,
    the last valid reading is used instead.
    """
    
    def __init__(self, max_power_threshold: int = 50000):
        """Initialize with a maximum power threshold.
        
        Args:
            max_power_threshold: Maximum valid power reading in watts
        """
        self.max_power_threshold = max_power_threshold
        self.last_valid_readings = {}
        
    def validate_reading(self, key: str, value: float) -> float:
        """Validate a power reading against the threshold.
        
        Args:
            key: The metric key (e.g., 'Power/FromSolar')
            value: The power reading value
            
        Returns:
            The original value if valid, or the last valid value if available
        """
        # For non-power readings, just return the value
        if not key.startswith('Power/') and not key.startswith('String') and not key.startswith('AC/Power'):
            return value
            
        # Check if the value exceeds the threshold
        if abs(value) > self.max_power_threshold:
            # Use the last valid reading if available
            if key in self.last_valid_readings:
                # Always print spurious warnings
                print(f"Warning: Spurious reading detected for {key}: {value}W exceeds threshold of {self.max_power_threshold}W")
                print(f"Using last valid reading: {self.last_valid_readings[key]}W")
                return self.last_valid_readings[key]
            # No valid reading available yet, use 0 as a safe default
            print(f"Warning: Spurious reading detected for {key}: {value}W exceeds threshold of {self.max_power_threshold}W")
            print(f"No valid previous reading available, using 0W")
            return 0
            
        # Store this valid reading
        self.last_valid_readings[key] = value
        return value


def decode_solax_data(raw_data: dict, power_validator: PowerValidator = None) -> dict:
    """Decode raw Solax inverter data into structured format.
    
    Args:
        raw_data: Raw JSON response from Solax API
        power_validator: Optional PowerValidator instance to validate readings
        
    Returns:
        Dictionary of decoded inverter metrics with units
    """
    inverter_data = {}
    data_array = raw_data.get('Data', [])
    info_array = raw_data.get('Information', [])
    
    # Create a default validator if none provided
    if power_validator is None:
        power_validator = PowerValidator()
    
    inverter_data["Imported/Total"] = unsigned_32_bit(data_array[37], data_array[38]) / 10
    inverter_data["Imported/Today"] = data_array[39] / 10
    inverter_data["Yield/Total"] = unsigned_32_bit(data_array[41], data_array[42]) / 10
    inverter_data["Yield/Today"] = data_array[43] / 10
    
    # Validate string power readings
    inverter_data["String1/Power"] = power_validator.validate_reading("String1/Power", data_array[19])
    inverter_data["String2/Power"] = power_validator.validate_reading("String2/Power", data_array[20])
    inverter_data["String3/Power"] = power_validator.validate_reading("String3/Power", data_array[21])
    
    inverter_data["String1/Voltage"] = data_array[11] / 10
    inverter_data["String2/Voltage"] = data_array[12] / 10
    inverter_data["String3/Voltage"] = data_array[13] / 10
    inverter_data["String1/Current"] = data_array[15] / 10
    inverter_data["String2/Current"] = data_array[16] / 10
    inverter_data["String3/Current"] = data_array[17] / 10
    
    s1_power = inverter_data["String1/Power"]
    s2_power = inverter_data["String2/Power"]
    s3_power = inverter_data["String3/Power"]

    # Calculate and validate total solar power
    solar_power = s1_power + s2_power + s3_power
    inverter_data["Power/FromSolar"] = power_validator.validate_reading("Power/FromSolar", solar_power)
    
    # Validate grid power readings
    grid_power = signed_32_bit(data_array[28], data_array[29])
    inverter_data["Power/Grid"] = power_validator.validate_reading("Power/Grid", grid_power)
    
    to_grid = positive(grid_power)
    inverter_data["Power/ToGrid"] = power_validator.validate_reading("Power/ToGrid", to_grid)
    
    from_grid = invert_positive(grid_power)
    inverter_data["Power/FromGrid"] = power_validator.validate_reading("Power/FromGrid", from_grid)
    
    # Validate home power
    inverter_data["Power/ToHome"] = power_validator.validate_reading("Power/ToHome", data_array[30])
    
    # Validate battery power readings
    battery_power = signed_16_bit(data_array[91])
    inverter_data["Power/Battery"] = power_validator.validate_reading("Power/Battery", battery_power)
    
    to_battery = positive(battery_power)
    inverter_data["Power/ToBattery"] = power_validator.validate_reading("Power/ToBattery", to_battery)
    
    from_battery = invert_positive(battery_power)
    inverter_data["Power/FromBattery"] = power_validator.validate_reading("Power/FromBattery", from_battery)

    # Validate AC power
    ac_power = signed_16_bit(data_array[6])
    inverter_data["AC/Power"] = power_validator.validate_reading("AC/Power", ac_power)
    
    inverter_data["AC/Voltage"] = data_array[4] / 10
    inverter_data["AC/Current"] = signed_16_bit(data_array[5]) / 10
    inverter_data["AC/Frequency"] = data_array[7] / 100

    inverter_data["Battery/SOC"] = data_array[93]
    inverter_data["Battery/Voltage"] = data_array[89] / 100
    inverter_data["Battery/Temperature"] = signed_16_bit(data_array[92])
    inverter_data["RunMode"] = unsigned_8_bit(data_array[10])

    return inverter_data


def format_solax_data(data_raw: dict) -> dict:
    """Format raw data dictionary with units.
    
    Args:
        data_raw: Dictionary of {metric: (value, unit)} pairs
        
    Returns:
        Dictionary with formatted {metric: {value: x, unit: y}} structure
    """
    formatted_data = {}
    for key, (value, unit) in data_raw.items():
        formatted_data[key] = {"value": value, "unit": unit}
    return formatted_data


def debug_data(data: list) -> None:
    """Debug helper to log and compare raw data values.

    Args:
        data: List of raw data values from inverter
    """
    with open('log', 'r') as log:
        previous = log.readlines()
    previous = [p.strip() for p in previous]
    items = {int(p.split(' ')[0]): p for p in previous}

    with open('log', 'w') as log:
        for n in range(0, len(data) - 1):
            d = data[n]
            d1 = data[n + 1]
            x = f'{n} {d} {d/10} {d/100} {signed_16_bit(d)} {signed_32_bit(d, d1)} {unsigned_32_bit(d, d1)}'
            log.write(x + '\n')
            if x in previous:
                continue
            print(x)
            print(items[n])


def log_iteration_data(raw_inverter_data: dict, raw_charger_data: dict, decoded_inverter_data: dict,
                       calculations: dict, actions: List['ChargerAction'], timezone=None, timezone_str="UTC",
                       detailed_log: bool = False) -> None:
    """Log comprehensive iteration data to JSON file.

    Args:
        raw_inverter_data: Raw JSON response from Solax API
        raw_charger_data: Raw charger data from Emporia API
        decoded_inverter_data: Decoded and processed inverter metrics
        calculations: All calculated values (power metrics, time estimates, etc.)
        actions: List of actions taken during this iteration
        timezone: Timezone object
        timezone_str: Timezone string name
        detailed_log: Whether to enable detailed JSON logging (defaults to False)
    """
    # Skip detailed logging if disabled
    if not detailed_log:
        return
    # Custom JSON encoder to handle non-serializable objects
    def json_encoder(obj):
        if hasattr(obj, '__dict__'):
            # Convert objects to their dict representation
            return obj.__dict__
        elif hasattr(obj, '__str__'):
            # Convert other objects to string
            return str(obj)
        else:
            # Fallback for unknown types
            return repr(obj)

    # Create a copy of charger_data with serializable values
    serializable_charger_data = {}
    for charger_name, charger_info in raw_charger_data.items():
        serializable_charger_data[charger_name] = {
            key: value for key, value in charger_info.items()
            if key not in ['ev_charger', 'device_gid']  # Skip non-serializable objects
        }
        # Add string representations of important objects
        if 'ev_charger' in charger_info:
            ev_charger = charger_info['ev_charger']
            serializable_charger_data[charger_name]['ev_charger_info'] = {
                'charging_rate': getattr(ev_charger, 'charging_rate', None),
                'status': getattr(ev_charger, 'status', None),
                'message': getattr(ev_charger, 'message', None),
                'charger_on': getattr(ev_charger, 'charger_on', None),
                'max_charging_rate': getattr(ev_charger, 'max_charging_rate', None),
                'pro_control_code': getattr(ev_charger, 'pro_control_code', None),
                'breaker_pin': getattr(ev_charger, 'breaker_pin', None),
                'fault_text': getattr(ev_charger, 'fault_text', None),
            }

    # Get current time in both UTC and configured timezone
    utc_now = datetime.datetime.now(pytz.UTC)
    if timezone is None:
        timezone = pytz.UTC
    local_now = utc_now.astimezone(timezone)

    log_entry = {
        "timestamp_utc": utc_now.isoformat(),
        "timestamp_local": local_now.isoformat(),
        "timezone": timezone_str,
        "raw_api_data": {
            "inverter": raw_inverter_data,
            "chargers": serializable_charger_data
        },
        "decoded_data": decoded_inverter_data,
        "calculations": calculations,
        "actions": [
            {
                "charger_name": action.charger_name,
                "action_type": action.action_type,
                "old_current": action.old_current,
                "new_current": action.new_current,
                "old_state": action.old_state,
                "new_state": action.new_state,
                "reason": action.reason,
                "values": action.values
            }
            for action in actions if action is not None
        ]
    }

    # Append to log file with pretty formatting
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        json.dump(log_entry, f, indent=2, ensure_ascii=False, default=json_encoder)
        f.write('\n')  # Add newline between entries


def get_emporia_chargers(vue: pyemvue.PyEmVue) -> dict:
    """Fetch charger data from Emporia Vue API.
    
    Args:
        vue: Authenticated PyEmVue instance
        
    Returns:
        Dictionary of charger data by name or None if error occurs
    """
    charger_by_id = {}
    charger_by_name = {}
    charger_power = {}
    
    try:
        vue_devices = vue.get_devices()
    except Exception as e:
        print(e)
        return None
        
    for device in vue_devices:
        if device.model == 'VVDN01':
            charger_by_id[device.device_gid] = device
            charger_by_name[device.device_name] = device
            
    try:
        vue_power = vue.get_device_list_usage(
            deviceGids=charger_by_id.keys(),
            instant=None,
            scale=Scale.SECOND.value,
            unit=Unit.KWH.value
        )
        if hasattr(get_emporia_chargers, 'verbose') and get_emporia_chargers.verbose:
            print(f"[debug] Emporia API call successful")
            print(f"[debug] Found {len(charger_by_id)} chargers: {list(charger_by_name.keys())}")
            print(f"[debug] Retrieved power data for {len(vue_power)} devices")
    except Exception as e:
        print(f"Error reading Emporia API: {e}")
        return None
        
    for gid, device in vue_power.items():
        for channelnum, channel in device.channels.items():
            if channel.name == 'Main':
                charger_power[device.device_gid] = channel.usage
                
    charger_data = {}
    for name, device in charger_by_name.items():
        power_watts = (charger_power[device.device_gid] * 3600) * 1000
        current_amps = device.ev_charger.charging_rate
        status = device.ev_charger.status
        message = device.ev_charger.message

        charger_data[name] = {
            'power': power_watts,
            'current': current_amps,
            'on': device.ev_charger.charger_on,
            'device_gid': device.device_gid,
            'ev_charger': device.ev_charger,
            'message': message,
            'status': status,
            'fault_text': device.ev_charger.fault_text,
            'max_charging_rate': device.ev_charger.max_charging_rate,
            'pro_control_code': device.ev_charger.pro_control_code,
            'breaker_pin': device.ev_charger.breaker_pin,
        }

        # Log charger status if verbose
        if hasattr(get_emporia_chargers, 'verbose') and get_emporia_chargers.verbose:
            power_kw = power_watts / 1000
            print(f"[debug] {name}: {power_kw:.1f}kW | {current_amps}A | Status: {status} | Message: {message}")

    return charger_data


@dataclass
class ChargerStatus:
    """Status information for a single charger."""
    name: str
    is_primary: bool
    connected: bool
    charging: bool
    current_amps: int
    power_watts: float
    proposed_amps: int
    state_active: bool


@dataclass
class ChargerAction:
    """Action taken for a charger during control cycle."""
    charger_name: str
    action_type: str  # 'current_change', 'state_change', 'no_change'
    old_current: int
    new_current: int
    old_state: bool
    new_state: bool
    reason: str
    values: Dict[str, any]


@dataclass
class SystemStatus:
    """Comprehensive system status for logging."""
    timestamp: str

    # Battery metrics
    battery_soc: int
    battery_voltage: float
    battery_temperature: int

    # Power flows (watts)
    solar_production: float
    house_consumption: float
    grid_import: float
    grid_export: float
    battery_charge: float
    battery_discharge: float

    # Energy allocation
    battery_reserve_allocation: int
    total_charger_power: float
    available_excess: float

    # Charger information
    chargers: List[ChargerStatus]
    primary_charger_active: bool
    active_charger_names: List[str]

    # Battery time calculations
    time_to_charged: str
    time_to_depleted: str
    battery_power_kw: float
    min_soc: int

    # Averaging buffers (mutable for updates)
    battery_power_history: List[float]
    max_power_samples: int
    
    def format_comprehensive_log(self) -> str:
        """Format a comprehensive, readable status summary."""
        # Create charger summary with proper formatting
        charger_summaries = []
        for charger in self.chargers:
            # Connection status icon
            connection_icon = "🟢" if charger.connected else "🔴"
            # Charging status icon
            status_icon = "⚡" if charger.charging else "⏸️"
            primary_marker = "★" if charger.is_primary else ""
            name_with_marker = f"{charger.name}{primary_marker}"
            
            # Add condition indicator
            condition_icon = ""
            if charger.state_active:
                condition_icon = "✅"  # Conditions met for enabling
            else:
                condition_icon = "❌"  # Conditions not met for enabling
                
            charger_kw = charger.power_watts / 1000
            charger_summaries.append(
                f"{name_with_marker}: {connection_icon}{status_icon}{condition_icon} {charger.current_amps}A/{charger_kw:.1f}kW"
            )
        
        
        if self.grid_import > 0:
            grid_flow = "🏠⬅️"
            grid_kw = self.grid_import / 1000
            grid_text = f"{grid_kw:.1f}kW importing"
        elif self.grid_export > 0:
            grid_flow = "🏠➡️"
            grid_kw = self.grid_export / 1000
            grid_text = f"{grid_kw:.1f}kW exporting"
        else:
            grid_flow = "🏠⚖️"
            grid_text = "balanced"
        
        # Format power values in kW with fixed width (5 characters)
        solar_kw = self.solar_production / 1000
        house_kw = self.house_consumption / 1000
        available_kw = self.available_excess / 1000

        # Calculate reserve in kW
        reserve_kw = self.battery_reserve_allocation / 1000

        # Determine which time estimate to show based on battery power flow
        if self.battery_power_kw > 0:
            # Battery is charging, show time to full
            time_display = f"⏱️ Full: {self.time_to_charged} (min {self.min_soc}%)"
        elif self.battery_power_kw < 0:
            # Battery is discharging, show time to empty
            time_display = f"⏱️ Empty: {self.time_to_depleted} (min {self.min_soc}%)"
        else:
            # Battery idle, show N/A
            time_display = f"⏱️  Idle: N/A (min {self.min_soc}%)"

        # Format grid text with exactly one space after emoji
        if self.grid_import > 0:
            grid_kw = self.grid_import / 1000
            grid_text_formatted = f" {grid_kw:.1f}kW"
        elif self.grid_export > 0:
            grid_kw = self.grid_export / 1000
            grid_text_formatted = f" {grid_kw:.1f}kW"
        else:
            grid_text_formatted = " 0.0kW"

        # Format as compact horizontally aligned single line with exact spacing
        log_parts = [
            f"[{self.timestamp}]",
            f"🔋 {self.battery_soc:>2}% ({self.battery_power_kw:+.1f}kW, {self.battery_temperature:>2}°C)",
            f"{time_display}",
            f"🔄 {reserve_kw:.1f}kW",
            f"☀️  {solar_kw:.1f}kW",
            f"🏠 {house_kw:.1f}kW",
            f"{grid_flow}{grid_text_formatted}",
            f"⚡ {available_kw:.1f}kW",
            f"🚗 {' | '.join(charger_summaries)}"
        ]

        return " | ".join(log_parts)


class InverterSensorManager:
    """Manages MQTT sensors for solar inverter data."""
    
    def __init__(self, mqtt_settings: Settings.MQTT):
        self.mqtt_settings = mqtt_settings
        self.sensors = {}
        self._setup_sensors()
    
    def _setup_sensors(self):
        """Setup all inverter MQTT sensors."""
        device_info = DeviceInfo(name="Solax A1-HYB-G2", identifiers="solax")
        
        sensor_configs = [
            ('Power/FromSolar', 'power', 'W'),
            ('Power/Battery', 'power', 'W'),
            ('Power/FromBattery', 'power', 'W'),
            ('Power/ToBattery', 'power', 'W'),
            ('Power/FromGrid', 'power', 'W'),
            ('Power/Grid', 'power', 'W'),
            ('Power/ToGrid', 'power', 'W'),
            ('Power/ToHome', 'power', 'W'),
            ('Battery/SOC', 'battery', '%'),
            ('Battery/Voltage', 'voltage', 'V'),
            ('Battery/Temperature', 'temperature', 'C'),
            ('Battery/TimeToCharged', 'duration', 'min'),
            ('Battery/TimeToDepleted', 'duration', 'min'),
            ('Battery/Power', 'power', 'kW'),
            ('Battery/MinSOC', 'battery', '%'),
            ('String1/Power', 'power', 'W'),
            ('String1/Voltage', 'voltage', 'V'),
            ('String1/Current', 'current', 'A'),
            ('String2/Power', 'power', 'W'),
            ('String2/Voltage', 'voltage', 'V'),
            ('String2/Current', 'current', 'A'),
            ('String3/Power', 'power', 'W'),
            ('String3/Voltage', 'voltage', 'V'),
            ('String3/Current', 'current', 'A'),
            ('AC/Power', 'power', 'W'),
            ('AC/Voltage', 'voltage', 'V'),
            ('AC/Current', 'current', 'A'),
            ('AC/Frequency', 'frequency', 'Hz'),
        ]
        
        for name, device_class, unit in sensor_configs:
            self._create_sensor(name, device_class, unit, device_info)
    
    def _create_sensor(self, name: str, device_class: str, unit: str, device_info: DeviceInfo):
        """Create a single MQTT sensor."""
        sensor_id = name.lower().replace('/', '_')
        print(f'Registering MQTT sensor: {sensor_id}')
        self.sensors[name] = Sensor(Settings(mqtt=self.mqtt_settings, entity=SensorInfo(
            name=name.replace('/', ' '),
            device_class=device_class,
            unique_id=sensor_id,
            unit_of_measurement=unit,
            device=device_info
        )))
    
    def update_sensor(self, name: str, value, verbose=False):
        """Update a sensor value."""
        if name in self.sensors:
            if verbose:
                print(f"[debug] Inverter sensor update: {name}: {value}")
            self.sensors[name].set_state(value)


class PowerCalculator:
    """Handles power calculations and allocation logic."""
    
    @staticmethod
    def calculate_base_power_metrics(inverter_data: dict, buffer: int) -> dict:
        """Calculate base power metrics used by all chargers."""
        solar_power = inverter_data['Power/FromSolar']
        house_load = inverter_data['Power/ToHome']
        excess = solar_power - house_load - buffer

        metrics = {
            'house_load': house_load,
            'from_battery': inverter_data['Power/FromBattery'],
            'to_battery': inverter_data['Power/ToBattery'],
            'from_grid': inverter_data['Power/FromGrid'],
            'to_grid': inverter_data['Power/ToGrid'],
            'soc_battery': inverter_data['Battery/SOC'],
            'excess': excess,
            'bus_load': inverter_data['AC/Power']
        }

        # Debug logging
        if hasattr(PowerCalculator, 'verbose') and PowerCalculator.verbose:
            print(f"[debug] Power Metrics: Solar {solar_power}W | House {house_load}W | Excess {excess}W | Buffer {buffer}W")
            print(f"[debug] Battery: To {metrics['to_battery']}W | From {metrics['from_battery']}W | SOC {metrics['soc_battery']}%")

        return metrics
    
    @staticmethod
    def calculate_battery_reserve(soc_battery: int) -> int:
        """Calculate power to reserve for battery charging based on SOC."""
        if soc_battery < 75:
            return 1700
        elif soc_battery < 85:
            return 1200
        elif soc_battery < 95:
            return 700
        elif soc_battery < 99:
            return 500
        return 0
    
    @staticmethod
    def calculate_available_power(power_metrics: dict, total_charger_load: float,
                                bus_maximum: int, reserve_for_battery: int) -> dict:
        """Calculate available power for charging."""
        available_excess = power_metrics['excess'] + total_charger_load - reserve_for_battery
        available_via_bus = bus_maximum - (power_metrics['house_load'] - total_charger_load)
        available_for_charge = min(available_excess, available_via_bus)

        # Debug logging
        if hasattr(PowerCalculator, 'verbose') and PowerCalculator.verbose:
            print(f"[debug] Power Budget: Excess {available_excess}W | Bus {available_via_bus}W | Available {available_for_charge}W")
            print(f"[debug] Constraints: Total charger load {total_charger_load}W | Reserve {reserve_for_battery}W | Bus max {bus_maximum}W")

        return {
            'available_excess': available_excess,
            'available_via_bus': available_via_bus,
            'available_for_charge': available_for_charge
        }

    @staticmethod
    def calculate_time_to_charged(soc_battery: int, battery_capacity: float, charging_power: float) -> str:
        """Calculate time to full battery charge.

        Args:
            soc_battery: Current battery SOC (0-100)
            battery_capacity: Battery capacity in kWh
            charging_power: Current charging power in kW

        Returns:
            Time string in HH:MM format or "N/A" if invalid
        """
        if charging_power <= 0 or soc_battery >= 100:
            return "N/A"

        # Calculate energy needed in kWh
        energy_needed = (100 - soc_battery) / 100 * battery_capacity

        # Calculate time in hours
        time_hours = energy_needed / charging_power

        # Convert to HH:MM format
        hours = int(time_hours)
        minutes = int((time_hours - hours) * 60)

        return f"{hours:02d}:{minutes:02d}"

    @staticmethod
    def calculate_time_to_depleted(soc_battery: int, min_soc: int, battery_capacity: float, discharging_power: float) -> str:
        """Calculate time to minimum battery SOC.

        Args:
            soc_battery: Current battery SOC (0-100)
            min_soc: Minimum SOC threshold (0-100)
            battery_capacity: Battery capacity in kWh
            discharging_power: Current discharging power in kW

        Returns:
            Time string in HH:MM format or "N/A" if invalid
        """
        if discharging_power <= 0 or soc_battery <= min_soc:
            return "N/A"

        # Calculate energy available in kWh
        energy_available = (soc_battery - min_soc) / 100 * battery_capacity

        # Calculate time in hours
        time_hours = energy_available / discharging_power

        # Convert to HH:MM format
        hours = int(time_hours)
        minutes = int((time_hours - hours) * 60)

        return f"{hours:02d}:{minutes:02d}"

    @staticmethod
    def update_power_average(current_power: float, power_history: list, max_samples: int) -> float:
        """Update rolling average of power values.

        Args:
            current_power: Latest power reading in kW
            power_history: List of recent power readings
            max_samples: Maximum number of samples to keep

        Returns:
            Rolling average power in kW
        """
        # Add current reading to history
        power_history.append(current_power)

        # Keep only the most recent samples
        if len(power_history) > max_samples:
            power_history.pop(0)

        # Calculate average
        if power_history:
            return sum(power_history) / len(power_history)
        return 0.0


class TimeBasedController:
    """Manages time-based charging rules independent of power calculations."""

    def __init__(self, switch_on_time="11:00", switch_off_time="18:00",
                 fixed_charge_start="00:10", fixed_charge_end="06:00",
                 fixed_charge_current=40, min_excess_threshold=1440,
                 battery_soc_threshold=85, timezone=None, min_current=6, max_current=30):
        self.timezone = timezone or pytz.UTC
        self.switch_on_time = datetime.datetime.strptime(switch_on_time, "%H:%M").time()
        self.switch_off_time = datetime.datetime.strptime(switch_off_time, "%H:%M").time()
        self.fixed_charge_start = datetime.datetime.strptime(fixed_charge_start, "%H:%M").time()
        self.fixed_charge_end = datetime.datetime.strptime(fixed_charge_end, "%H:%M").time()
        self.fixed_charge_current = fixed_charge_current
        self.min_excess_threshold = min_excess_threshold
        self.battery_soc_threshold = battery_soc_threshold
        self.min_current = min_current
        self.max_current = max_current

        # State tracking for daily off-after-6pm logic
        self.daily_disabled = False
        self.last_reset_date = datetime.date.today()

    def _reset_daily_state_if_needed(self):
        """Reset daily state at midnight."""
        today = datetime.datetime.now(self.timezone).date()
        if today != self.last_reset_date:
            self.daily_disabled = False
            self.last_reset_date = today

    def is_unrestricted_period(self) -> bool:
        """Check if we're in the unrestricted charging period (00:10-06:00)."""
        self._reset_daily_state_if_needed()
        current_time = datetime.datetime.now(self.timezone).time()
        return self.fixed_charge_start <= current_time < self.fixed_charge_end

    def should_enable_based_on_time(self, excess_power: float, battery_soc: int) -> bool:
        """Check if charger should be enabled based on time rules."""
        self._reset_daily_state_if_needed()
        current_time = datetime.datetime.now(self.timezone).time()

        # If already disabled for the day after 6pm, don't re-enable
        if self.daily_disabled:
            return False

        # Check if within switch-on window (11am-6pm)
        if self.switch_on_time <= current_time < self.switch_off_time:
            # Enable if excess > threshold and battery SOC > threshold
            return excess_power > self.min_excess_threshold and battery_soc > self.battery_soc_threshold

        return False

    def should_disable_based_on_time(self, excess_power: float) -> bool:
        """Check if charger should be disabled based on time rules."""
        self._reset_daily_state_if_needed()
        current_time = datetime.datetime.now(self.timezone).time()

        # After 6pm, disable if excess becomes negative and mark as disabled for the day
        if current_time >= self.switch_off_time and excess_power < 0:
            self.daily_disabled = True
            return True

        return False

    def get_time_based_current(self, excess_power: float, battery_soc: int, voltage: int, charger_current: int = None) -> tuple[int, bool]:
        """Get current and state based on time rules. Returns (current, enabled)."""
        current_time = datetime.datetime.now(self.timezone).time()
        timezone_name = str(self.timezone).split('/')[-1] if '/' in str(self.timezone) else str(self.timezone)
        if hasattr(self, 'verbose') and self.verbose:
            print(f"DEBUG TIMEZONE: UTC={datetime.datetime.now(pytz.UTC).time()}, Local({timezone_name})={current_time}, Timezone={self.timezone}")
            print(f"DEBUG TIME: current_time={current_time}, switch_on={self.switch_on_time}, switch_off={self.switch_off_time}")
            print(f"DEBUG TIME: is_unrestricted={self.is_unrestricted_period()}")
            print(f"DEBUG TIME: should_enable={self.should_enable_based_on_time(excess_power, battery_soc)}")
            print(f"DEBUG TIME: should_disable={self.should_disable_based_on_time(excess_power)}")

        if self.is_unrestricted_period():
            # Unrestricted charging at fixed current
            if self.fixed_charge_current > 6:  # Only print if enabling
                print(f"🔌 TIME RULE ({timezone_name}): Unrestricted charging period ({self.fixed_charge_start}-{self.fixed_charge_end}) - Fixed charge at {self.fixed_charge_current}A")
            return self.fixed_charge_current, True

        if self.should_disable_based_on_time(excess_power):
            return self.min_current, False  # Minimum current, disabled

        if self.should_enable_based_on_time(excess_power, battery_soc):
            # Calculate based on excess power, but cap appropriately
            calculated_current = min(int(excess_power / voltage), self.max_current)  # Use configured max current
            final_current = max(calculated_current, self.min_current)
            return final_current, True

        return self.min_current, False  # Default to disabled


class ChargerController:
    """Controller for managing EV charger based on solar/battery conditions."""

    def __init__(self, vue: pyemvue.PyEmVue, charger_name: str,
                 mqtt_settings: Settings.MQTT, upper_limit=30, lower_limit=6,
                 voltage=240, on_to_off_lockout=60, off_to_on_lockout=240,
                 bus_maximum=7000, buffer=100, is_primary=False,
                 time_controller: TimeBasedController = None):
        self.upper_limit = upper_limit
        self.bus_maximum = bus_maximum
        self.lower_limit = lower_limit
        self.voltage = voltage
        self.on_to_off_lockout = on_to_off_lockout
        self.off_to_on_lockout = off_to_on_lockout
        self.buffer = buffer
        self.on_to_off_time = datetime.datetime.now()
        self.off_to_on_time = datetime.datetime.now()
        self.vue = vue
        self.charger_name = charger_name
        self.is_primary = is_primary
        self.mqtt_settings = mqtt_settings
        self.time_controller = time_controller
        
        # Initialize charger state attributes
        self.charger_load = 0
        self.charger_state = False
        self.charger_current = 0
        self.charger_connected = False
        self.connected = False
        self.charging = False
        
        # Initialize HA sensors and switches
        self._setup_ha_entities()
    
    def _setup_ha_entities(self):
        """Setup Home Assistant MQTT entities for this charger."""
        charger_info = DeviceInfo(
            name=f'{self.charger_name}',
            identifiers=f"{self.charger_name.lower().replace(' ', '_')}"
        )
        
        # Setup switch for excess solar control
        self._setup_excess_switch(charger_info)
        
        # Setup sensors for current and power
        self._setup_sensors(charger_info)
    
    def _setup_excess_switch(self, device_info: DeviceInfo):
        """Setup the excess solar control switch."""
        def switch_enabled(client: Client, controller, message: MQTTMessage):
            payload = message.payload.decode()
            if payload == "ON":
                controller.enabled = True
                self.enabled_ha.on()
            elif payload == "OFF":
                controller.enabled = False
                self.enabled_ha.off()
        
        switch_id = f'{self.charger_name}_use_excess'.lower().replace(' ', '_')
        print(f'Registering MQTT switch: {switch_id}')
        
        self.enabled_ha = Switch(Settings(mqtt=self.mqtt_settings, entity=SwitchInfo(
            device=device_info,
            name=f'{self.charger_name} Use Excess Solar',
            unique_id=switch_id,
        )), switch_enabled)
        self.enabled_ha.off()
    
    def _setup_sensors(self, device_info: DeviceInfo):
        """Setup current and power sensors."""
        # Current sensor
        current_id = f'{self.charger_name}_current'.lower().replace(' ', '_')
        print(f'Registering MQTT sensor: {current_id}')
        self.current_ha = Sensor(Settings(mqtt=self.mqtt_settings, entity=SensorInfo(
            name='Current',
            device_class='current',
            unique_id=current_id,
            unit_of_measurement='A',
            device=device_info
        )))
        
        # Power sensor
        power_id = f'{self.charger_name}_power'.lower().replace(' ', '_')
        print(f'Registering MQTT sensor: {power_id}')
        self.power_ha = Sensor(Settings(mqtt=self.mqtt_settings, entity=SensorInfo(
            name='Power',
            device_class='power',
            unique_id=power_id,
            unit_of_measurement='W',
            device=device_info
        )))

    def update(self, charger_data: dict) -> None:
        """Update charger state from latest data.
        
        Args:
            charger_data: Dictionary of current charger metrics
        """
        the_charger = charger_data[self.charger_name]
        self.charger_load = the_charger['power']
        self.charger_state = the_charger['on']
        self.charger_current = the_charger['current']
        self.current_ha.set_state(self.charger_current)
        self.power_ha.set_state(self.charger_load)
        self.charger_connected = the_charger['message'] in ('Connected to EV', 'Charging', 'Please Wait')
        self.charging = self.charger_load > 100
        if hasattr(self, 'verbose') and self.verbose:
            print(f"DEBUG CHARGER STATUS: {self.charger_name} - message='{the_charger.get('message', 'NO_MESSAGE')}', status='{the_charger.get('status', 'NO_STATUS')}', connected={self.charger_connected}, charging={self.charging}, current={self.charger_current}A, power={self.charger_load}W")

    def control(self, charger_data: dict, inverter_data: dict, all_controllers: dict) -> tuple[ChargerStatus, Optional[ChargerAction]]:
        """Control charger based on solar/battery conditions with primary/secondary logic.

        Args:
            charger_data: Current charger metrics
            inverter_data: Current solar/battery metrics
            all_controllers: Dictionary of all charger controllers for coordination

        Returns:
            ChargerStatus: Status information for this charger
        """
        # Check time-based rules first if time controller is available
        if self.time_controller:
            excess_power = inverter_data['Power/FromSolar'] - inverter_data['Power/ToHome'] - self.buffer
            battery_soc = inverter_data['Battery/SOC']

            # Get time-based current and state
            time_based_current, time_based_enabled = self.time_controller.get_time_based_current(
                excess_power, battery_soc, self.voltage, self.charger_current
            )

            # If in unrestricted period or time-based rules apply, use time-based logic
            if self.time_controller.is_unrestricted_period() or time_based_enabled:
                if hasattr(self, 'verbose') and self.verbose:
                    period_type = "unrestricted charging" if self.time_controller.is_unrestricted_period() else "daytime automated charging"
                    print(f"[debug] {self.charger_name}: Using {period_type} logic - Current: {time_based_current}A, Enabled: {time_based_enabled}")

                # Apply time-based changes with detailed logging
                values = {
                    'excess_power': f"{excess_power:.0f}W",
                    'battery_soc': f"{battery_soc}%",
                    'time': datetime.datetime.now(self.time_controller.timezone).strftime("%H:%M:%S")
                }
                period_type = "unrestricted charging" if self.time_controller.is_unrestricted_period() else "daytime automated charging"
                reason = f"Time-based rule ({period_type})"
                if hasattr(self, 'verbose') and self.verbose:
                    print(f"DEBUG CHARGER: Before update - current={self.charger_current}A, proposed={time_based_current}A, enabled={time_based_enabled}, connected={self.charger_connected}")
                action = self._apply_charger_changes(charger_data, time_based_current, time_based_enabled, reason, values)
                if hasattr(self, 'verbose') and self.verbose:
                    print(f"DEBUG CHARGER: After update - current={self.charger_current}A, action={action}")

                status = ChargerStatus(
                    name=self.charger_name,
                    is_primary=self.is_primary,
                    connected=self.charger_connected,
                    charging=self.charging,
                    current_amps=self.charger_current,
                    power_watts=self.charger_load,
                    proposed_amps=time_based_current,
                    state_active=time_based_enabled
                )
                return status, action

        # Fall back to original power-based logic
        # Calculate base power metrics using PowerCalculator
        power_metrics = PowerCalculator.calculate_base_power_metrics(inverter_data, self.buffer)
        total_charger_load = sum(controller.charger_load for controller in all_controllers.values())
        reserve_for_battery = PowerCalculator.calculate_battery_reserve(power_metrics['soc_battery'])

        # Calculate available power for charging
        power_availability = PowerCalculator.calculate_available_power(
            power_metrics, total_charger_load, self.bus_maximum, reserve_for_battery
        )

        # Check time-based conditions
        should_enable, should_disable = self._check_time_conditions(inverter_data)

        # Find primary controller and check if it's actively charging
        primary_is_charging = self._get_primary_charging_status(all_controllers)

        # Check if this charger should be active based on priority and single-charger rule
        should_be_active = self._should_be_active(all_controllers, should_enable, should_disable)

        # Calculate proposed current based on charger type and conditions
        proposed_current, proposed_state = self._calculate_proposed_current(
            power_availability['available_for_charge'], primary_is_charging,
            all_controllers, should_be_active, should_enable, should_disable
        )

        # Apply the proposed changes if needed with detailed logging
        values = {
            'available_power': f"{power_availability['available_for_charge']:.0f}W",
            'excess_power': f"{power_metrics['excess']:.0f}W",
            'battery_soc': f"{power_metrics['soc_battery']}%",
            'reserve': f"{reserve_for_battery}W",
            'total_charger_load': f"{total_charger_load:.0f}W",
            'should_enable': should_enable,
            'should_disable': should_disable,
            'should_be_active': should_be_active,
            'primary_charging': primary_is_charging,
            'time': datetime.datetime.now().strftime("%H:%M:%S")
        }
        reason = f"Power-based logic ({'Primary' if self.is_primary else 'Secondary'})"
        action = self._apply_charger_changes(charger_data, proposed_current, proposed_state, reason, values)

        # Return status information for logging
        status = ChargerStatus(
            name=self.charger_name,
            is_primary=self.is_primary,
            connected=self.charger_connected,
            charging=self.charging,
            current_amps=self.charger_current,
            power_watts=self.charger_load,
            proposed_amps=proposed_current,
            state_active=proposed_state
        )
        return status, action
    
    def _get_primary_charging_status(self, all_controllers: dict) -> bool:
        """Check if primary charger is actively charging."""
        for controller in all_controllers.values():
            if controller.is_primary:
                return controller.charger_connected and controller.charging
        return False

    def _check_time_conditions(self, inverter_data: dict) -> tuple[bool, bool]:
        """Check time-based enable/disable conditions.
        
        Returns:
            tuple: (should_enable, should_disable)
        """
        from datetime import datetime
        
        current_time = datetime.now()
        hour = current_time.hour
        
        # Get battery SOC and excess power from inverter data
        soc_battery = inverter_data.get('Battery/SOC', 0)
        excess_power = inverter_data.get('Power/FromSolar', 0) - inverter_data.get('Power/ToHome', 0) - self.buffer
        
        # Enable conditions: after 10am, battery > 85%, excess > 0
        should_enable = (
            10 <= hour < 16 and  # Between 10am and 4pm
            soc_battery > 85 and  # Battery above 85%
            excess_power > 0  # Excess power available
        )
        
        # Disable conditions: after 4pm and battery is draining
        battery_draining = inverter_data.get('Power/FromBattery', 0) > 0
        should_disable = (
            hour >= 16 and  # After 4pm
            battery_draining  # Battery is draining
        )
        
        return should_enable, should_disable

    def _should_be_active(self, all_controllers: dict, should_enable: bool, should_disable: bool) -> bool:
        """Determine if this charger should be active based on priority and single-charger rule.
        
        Args:
            all_controllers: Dictionary of all charger controllers
            should_enable: Whether time conditions allow enabling
            should_disable: Whether time conditions require disabling
            
        Returns:
            bool: Whether this charger should be active
        """
        # If should_disable is True, no charger should be active
        if should_disable:
            return False
            
        # If should_enable is False, use normal power-based logic
        if not should_enable:
            return True
            
        # When should_enable is True, only one charger should be active
        # Priority: primary chargers first, then secondary in order
        
        # Check if this is a primary charger
        if self.is_primary:
            # Primary chargers have priority
            return True
            
        # For secondary chargers, check if any primary is active or wants to be active
        for controller in all_controllers.values():
            if controller.is_primary and controller.charger_connected:
                return False
                
        # If no primary chargers are available, allow this secondary
        return True
    
    def _calculate_proposed_current(self, available_for_charge: float,
                                  primary_is_charging: bool, all_controllers: dict,
                                  should_be_active: bool, should_enable: bool, should_disable: bool) -> tuple[int, bool]:
        """Calculate proposed current and state for this charger."""
        # If should_disable is True, always return minimum current and False state
        if should_disable:
            return self.lower_limit, False
            
        # If should_enable is False, use normal power-based logic
        if not should_enable:
            if self.is_primary:
                return self._calculate_primary_current(available_for_charge)
            else:
                return self._calculate_secondary_current(available_for_charge, primary_is_charging, all_controllers)
        
        # When should_enable is True, respect the should_be_active flag
        if not should_be_active:
            return self.lower_limit, False
            
        # Otherwise, calculate normally
        if self.is_primary:
            return self._calculate_primary_current(available_for_charge)
        else:
            return self._calculate_secondary_current(available_for_charge, primary_is_charging, all_controllers)
    
    def _calculate_primary_current(self, available_for_charge: float) -> tuple[int, bool]:
        """Calculate current for primary charger."""
        proposed_current = round(available_for_charge / self.voltage)
        
        if hasattr(self, 'verbose') and self.verbose:
            print(f"[debug] Primary {self.charger_name}: Available power: {available_for_charge:.0f}W")
            print(f"[debug] Primary {self.charger_name}: Calculated current: {proposed_current}A (voltage: {self.voltage}V)")
            print(f"[debug] Primary {self.charger_name}: Current charger setting: {self.charger_current}A")
            print(f"[debug] Primary {self.charger_name}: Limits: {self.lower_limit}A - {self.upper_limit}A")
        
        if proposed_current > self.upper_limit:
            if hasattr(self, 'verbose') and self.verbose:
                print(f"[debug] Primary {self.charger_name}: Clipping {proposed_current}A to upper limit {self.upper_limit}A")
            return self.upper_limit, True
        elif proposed_current < self.lower_limit:
            if hasattr(self, 'verbose') and self.verbose:
                print(f"[debug] Primary {self.charger_name}: {proposed_current}A below minimum, pausing charger")
            return self.lower_limit, False
        else:
            if hasattr(self, 'verbose') and self.verbose:
                print(f"[debug] Primary {self.charger_name}: Setting current to {proposed_current}A, enabling charger")
            return proposed_current, True
    
    def _calculate_secondary_current(self, available_for_charge: float,
                                   primary_is_charging: bool, all_controllers: dict) -> tuple[int, bool]:
        """Calculate current for secondary charger."""
        if hasattr(self, 'verbose') and self.verbose:
            print(f"[debug] Secondary {self.charger_name}: Primary charging: {primary_is_charging}")
            print(f"[debug] Secondary {self.charger_name}: Available power: {available_for_charge:.0f}W")
            print(f"[debug] Secondary {self.charger_name}: Current charger setting: {self.charger_current}A")
        
        if primary_is_charging:
            # Primary is charging, secondary gets minimum 6A
            if hasattr(self, 'verbose') and self.verbose:
                print(f"[debug] Secondary {self.charger_name}: Primary active, limiting to minimum {self.lower_limit}A")
            return self.lower_limit, True
        else:
            # Primary not charging, secondary can use excess power
            secondary_controllers = [c for c in all_controllers.values() if not c.is_primary and c != self]
            secondary_minimum_power = len(secondary_controllers) * self.lower_limit * self.voltage
            available_for_this_secondary = available_for_charge - secondary_minimum_power
            
            if hasattr(self, 'verbose') and self.verbose:
                print(f"[debug] Secondary {self.charger_name}: Other secondaries: {len(secondary_controllers)}")
                print(f"[debug] Secondary {self.charger_name}: Reserved for others: {secondary_minimum_power}W")
                print(f"[debug] Secondary {self.charger_name}: Available for this charger: {available_for_this_secondary:.0f}W")
            
            proposed_current = round(available_for_this_secondary / self.voltage)
            
            if proposed_current > self.upper_limit:
                if hasattr(self, 'verbose') and self.verbose:
                    print(f"[debug] Secondary {self.charger_name}: Clipping {proposed_current}A to upper limit {self.upper_limit}A")
                return self.upper_limit, True
            elif proposed_current < self.lower_limit:
                if hasattr(self, 'verbose') and self.verbose:
                    print(f"[debug] Secondary {self.charger_name}: {proposed_current}A below minimum, using minimum {self.lower_limit}A")
                return self.lower_limit, True
            else:
                if hasattr(self, 'verbose') and self.verbose:
                    print(f"[debug] Secondary {self.charger_name}: Setting current to {proposed_current}A")
                return proposed_current, True
    
    def _set_charger_state(self, charger_device, on: bool, charge_rate: int) -> bool:
        """Set charger on/off state and charge rate.

        Args:
            charger_device: The Emporia charger device object
            on: Whether to turn charger on or off
            charge_rate: Charge rate in amps

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            self.vue.update_charger(charger_device, on=on, charge_rate=charge_rate)
            if hasattr(self, 'verbose') and self.verbose:
                print(f"DEBUG CHARGER: Successfully set {self.charger_name} to on={on}, rate={charge_rate}A")
            return True
        except Exception as e:
            print(f"Error setting charger state for {self.charger_name}: {e}")
            return False

    def _verify_charger_state(self, charger_data: dict, expected_on: bool, expected_current: int) -> bool:
        """Verify that charger is in the expected state by re-reading from API.

        Args:
            charger_data: Current charger data (will be updated)
            expected_on: Expected on state
            expected_current: Expected current

        Returns:
            bool: True if state matches expected, False otherwise
        """
        try:
            # Re-fetch charger data to verify state
            updated_chargers = get_emporia_chargers(self.vue)
            if updated_chargers and self.charger_name in updated_chargers:
                updated_charger = updated_chargers[self.charger_name]
                actual_on = updated_charger.get('on', False)
                actual_current = updated_charger.get('current', 0)

                # Update our local charger_data with fresh data
                charger_data[self.charger_name] = updated_charger

                # Update local state
                self.charger_load = updated_charger['power']
                self.charger_state = updated_charger['on']
                self.charger_current = updated_charger['current']
                self.charger_connected = updated_charger['message'] in ('Connected to EV', 'Charging', 'Please Wait')
                self.charging = self.charger_load > 100

                if hasattr(self, 'verbose') and self.verbose:
                    print(f"DEBUG CHARGER: Verified {self.charger_name} - expected: on={expected_on}, rate={expected_current}A | actual: on={actual_on}, rate={actual_current}A")

                return actual_on == expected_on and actual_current == expected_current
            else:
                print(f"Warning: Could not verify charger state for {self.charger_name}")
                return False
        except Exception as e:
            print(f"Error verifying charger state for {self.charger_name}: {e}")
            return False

    def _apply_charger_changes(self, charger_data: dict, proposed_current: int, proposed_state: bool, reason: str, values: dict) -> Optional[ChargerAction]:
        """Apply charger current changes if needed.

        Args:
            charger_data: Current charger data
            proposed_current: Desired current in amps
            proposed_state: Whether charger should be enabled
            reason: The rule or logic that triggered this change
            values: Dictionary of relevant values (excess power, SOC, time, etc.)

        Returns:
            ChargerAction if a change was made, None otherwise
        """
        # Only update if charger is connected
        if not self.charger_connected:
            if hasattr(self, 'verbose') and self.verbose:
                print(f"[debug] {self.charger_name}: Not connected, skipping update")
                print(f"DEBUG CHARGER: Not connected - charger_connected={self.charger_connected}")
            return None

        # For Emporia chargers, we need to handle both on/off state and current
        # If proposed_state is True, we want to enable charging (on=True)
        # If proposed_state is False, we want to disable charging (on=False)
        charger_on = proposed_state

        # Get current charger on state from the API data
        the_charger = charger_data[self.charger_name]
        current_charger_on = the_charger.get('on', False)

        needs_update = (self.charger_current != proposed_current) or (current_charger_on != charger_on)

        if not needs_update:
            if hasattr(self, 'verbose') and self.verbose:
                print(f"DEBUG CHARGER: No change needed - current={self.charger_current}A (on={current_charger_on}), proposed={proposed_current}A (on={charger_on})")
            return None

        if hasattr(self, 'verbose') and self.verbose:
            print(f"DEBUG CHARGER: State change detected - current={self.charger_current}A (on={current_charger_on}) -> proposed={proposed_current}A (on={charger_on})")

        try:
            the_charger = charger_data[self.charger_name]
            if hasattr(self, 'verbose') and self.verbose:
                print(f"[debug] {self.charger_name}: Updating charger from {self.charger_current}A to {proposed_current}A")

            # Log charger state changes to console regardless of verbose mode
            old_state = self.charger_current > self.lower_limit  # Was it enabled (>min_current)?
            new_state = proposed_current > self.lower_limit      # Will it be enabled (>min_current)?

            # Determine action type
            if old_state != new_state:
                action_type = 'state_change'
            else:
                action_type = 'current_change'

            if old_state != new_state:
                state_word = "ON" if new_state else "OFF"
                current_time = datetime.datetime.now(self.time_controller.timezone) if self.time_controller else datetime.datetime.now()
                timezone_name = str(self.time_controller.timezone).split('/')[-1] if self.time_controller and '/' in str(self.time_controller.timezone) else "UTC"

                # Determine the policy type and time windows
                if self.time_controller:
                    if self.time_controller.is_unrestricted_period():
                        policy_type = "Unrestricted charging"
                        time_policy = f"Fixed charge period: {self.time_controller.fixed_charge_start.strftime('%H:%M')}-{self.time_controller.fixed_charge_end.strftime('%H:%M')}"
                    elif self.time_controller.daily_disabled:
                        policy_type = "Post-switch-off disable"
                        time_policy = f"Switch-off time: {self.time_controller.switch_off_time.strftime('%H:%M')} (daily disable active)"
                    elif new_state:
                        policy_type = "Daytime automated charging"
                        time_policy = f"Daytime window: {self.time_controller.switch_on_time.strftime('%H:%M')}-{self.time_controller.switch_off_time.strftime('%H:%M')}"
                    else:
                        policy_type = "Outside time windows"
                        time_policy = f"Outside configured time windows"
                else:
                    policy_type = "Power-based control"
                    time_policy = "No time controller configured"

                print(f"🔌 {self.charger_name}: Switched {state_word} at {proposed_current}A")
                print(f"   Reason: {reason}")
                print(f"   Time: {current_time.strftime('%H:%M:%S')} ({timezone_name})")
                print(f"   Policy: {time_policy}")

            # Set the charger state
            success = self._set_charger_state(the_charger['ev_charger'], charger_on, proposed_current)

            if success:
                # Verify the state was set correctly
                if self._verify_charger_state(charger_data, charger_on, proposed_current):
                    if hasattr(self, 'verbose') and self.verbose:
                        print(f"DEBUG CHARGER: State verification successful for {self.charger_name}")
                else:
                    print(f"Warning: Charger state verification failed for {self.charger_name}")

                # Update Home Assistant sensor
                self.current_ha.set_state(proposed_current)

                # Create action record
                action = ChargerAction(
                    charger_name=self.charger_name,
                    action_type=action_type,
                    old_current=self.charger_current,
                    new_current=proposed_current,
                    old_state=old_state,
                    new_state=new_state,
                    reason=reason,
                    values=values
                )

                # Update local state (already done in _verify_charger_state)
                return action
            else:
                print(f"Failed to set charger state for {self.charger_name}")
                return None

        except Exception as e:
            print(f"Error updating {self.charger_name}: {e}")
            return None


def main() -> None:
    """Main execution function for polling and control loop.
    
    Handles:
    - Command line argument parsing
    - MQTT sensor setup
    - Emporia Vue authentication
    - Main polling and control loop
    """
    parser = argparse.ArgumentParser(
        description="Fetch and display real-time data from a Solax inverter."
    )
    parser.add_argument(
        "ip_address",
        nargs='?',
        help="The IP address of your Solax inverter (e.g., 192.168.2.117)"
    )
    parser.add_argument(
        "serial_number",
        nargs='?',
        help="The serial number of your Solax inverter, used as the password (e.g., SSAXHKSYAE)"
    )
    parser.add_argument(
        "broker",
        nargs='?',
        help="MQTT broker"
    )
    parser.add_argument(
        "primary_charger",
        nargs='?',
        help="Name of the primary charger that gets priority for excess power"
    )
    parser.add_argument(
        "-u", '--username',
        help="MQTT username",
        default="a",
        required=False
    )
    parser.add_argument(
        "-p", '--password',
        help="MQTT password",
        default="a",
        required=False
    )
    parser.add_argument(
        "-s", '--sleep',
        help="Poll delay",
        type=int,
        default=10
    )
    parser.add_argument(
        "-c", '--creds-file',
        help="Emporia creds file",
        type=str,
        default='keys.json'
    )
    parser.add_argument(
        "-v", '--verbose',
        help="Enable verbose logging for debugging",
        action='store_true'
    )
    parser.add_argument(
        "--battery-capacity",
        help="Battery capacity in kWh",
        type=float,
        default=20.0
    )
    parser.add_argument(
        "--min-soc",
        help="Minimum battery SOC threshold for depletion calculations",
        type=int,
        default=30
    )
    parser.add_argument(
        "--power-avg-window",
        help="Time window in minutes for averaging battery power demands",
        type=int,
        default=5
    )
    parser.add_argument(
        "--max-power-threshold",
        help="Maximum valid power reading in watts (readings above this will be considered spurious)",
        type=int,
        default=50000
    )
    parser.add_argument(
        "--config",
        help="Path to configuration JSON file",
        type=str,
        default="config.json"
    )
    parser.add_argument(
        "--timezone",
        help="Timezone for timestamps (e.g., 'America/New_York', 'Europe/London')",
        type=str
    )
    # Time-based behavior CLI args
    parser.add_argument(
        "--switch-on-time",
        help="Time to enable charging (e.g., '11:00')",
        type=str
    )
    parser.add_argument(
        "--switch-off-time",
        help="Time to disable charging (e.g., '18:00')",
        type=str
    )
    parser.add_argument(
        "--fixed-charge-start",
        help="Start time for unrestricted charging period (e.g., '00:10')",
        type=str
    )
    parser.add_argument(
        "--fixed-charge-end",
        help="End time for unrestricted charging period (e.g., '06:00')",
        type=str
    )
    parser.add_argument(
        "--fixed-charge-current",
        help="Current for unrestricted charging period in amps",
        type=int
    )
    parser.add_argument(
        "--min-excess-threshold",
        help="Minimum excess power threshold in watts for daytime automated charging",
        type=int
    )
    parser.add_argument(
        "--battery-soc-threshold",
        help="Battery SOC threshold in percent for enabling daytime automated charging",
        type=int
    )
    # Charger limits CLI args
    parser.add_argument(
        "--max-current",
        help="Maximum charging current in amps",
        type=int
    )
    parser.add_argument(
        "--min-current",
        help="Minimum charging current in amps",
        type=int
    )
    parser.add_argument(
        "--bus-maximum",
        help="Maximum power the AC bus can handle in watts",
        type=int
    )
    parser.add_argument(
        "--buffer",
        help="Power buffer in watts to maintain as safety margin",
        type=int
    )
    parser.add_argument(
        "--on-to-off-lockout",
        help="Lockout time in seconds when changing from on to off",
        type=int
    )
    parser.add_argument(
        "--off-to-on-lockout",
        help="Lockout time in seconds when changing from off to on",
        type=int
    )
    parser.add_argument(
        "--detailed-log",
        help="Enable detailed JSON logging to poll_log.json (off by default)",
        action="store_true"
    )
    
    args = parser.parse_args()

    # Load configuration from file if it exists
    config = {}
    if os.path.exists(args.config):
        try:
            with open(args.config, 'r') as f:
                config = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: Could not load config file {args.config}: {e}")
            print("Using command line arguments only.")

    # Merge config with CLI args (CLI takes precedence)
    def get_config_value(section, key, default=None, arg_value=None):
        if arg_value is not None:
            return arg_value
        return config.get(section, {}).get(key, default)

    # Extract values from config or CLI
    solax_ip = get_config_value('solax', 'ip_address', arg_value=args.ip_address)
    solax_serial = get_config_value('solax', 'serial_number', arg_value=args.serial_number)
    mqtt_broker = get_config_value('mqtt', 'broker', arg_value=args.broker)
    primary_charger = get_config_value('chargers', 'primary_charger', arg_value=args.primary_charger)

    # Get optional parameters (same logic for both modes)
    mqtt_username = get_config_value('mqtt', 'username', 'a', getattr(args, 'username', None))
    mqtt_password = get_config_value('mqtt', 'password', 'a', getattr(args, 'password', None))
    creds_file = get_config_value('system', 'creds_file', 'keys.json', getattr(args, 'creds_file', None))
    sleep_interval = get_config_value('system', 'sleep_interval', 10, getattr(args, 'sleep', None))
    battery_capacity = get_config_value('system', 'battery_capacity', 20.0, getattr(args, 'battery_capacity', None))
    min_soc = get_config_value('system', 'min_soc', 30, getattr(args, 'min_soc', None))
    power_avg_window = get_config_value('system', 'power_avg_window', 5, getattr(args, 'power_avg_window', None))
    max_power_threshold = get_config_value('system', 'max_power_threshold', 50000, getattr(args, 'max_power_threshold', None))
    timezone_str = get_config_value('time_based_behavior', 'timezone', 'UTC', getattr(args, 'timezone', None))
    detailed_log = get_config_value('system', 'detailed_log', False, getattr(args, 'detailed_log', None))
    print(f"DEBUG: args.timezone: {getattr(args, 'timezone', 'NOT_SET')}")
    print(f"DEBUG: get_config_value result: {timezone_str}")
    verbose = getattr(args, 'verbose', False)
    # Time-based behavior config
    switch_on_time = get_config_value('time_based_behavior', 'switch_on_time', '11:00', getattr(args, 'switch_on_time', None))
    switch_off_time = get_config_value('time_based_behavior', 'switch_off_time', '18:00', getattr(args, 'switch_off_time', None))
    fixed_charge_start = get_config_value('time_based_behavior', 'fixed_charge_start', '00:10', getattr(args, 'fixed_charge_start', None))
    fixed_charge_end = get_config_value('time_based_behavior', 'fixed_charge_end', '06:00', getattr(args, 'fixed_charge_end', None))
    fixed_charge_current = get_config_value('time_based_behavior', 'fixed_charge_current', 40, getattr(args, 'fixed_charge_current', None))
    min_excess_threshold = get_config_value('time_based_behavior', 'min_excess_threshold', 1440, getattr(args, 'min_excess_threshold', None))
    battery_soc_threshold = get_config_value('time_based_behavior', 'battery_soc_threshold', 85, getattr(args, 'battery_soc_threshold', None))

    # Setup timezone
    print(f"DEBUG: timezone_str from config: '{timezone_str}'")
    try:
        timezone = pytz.timezone(timezone_str)
        print(f"DEBUG: Configured timezone: {timezone_str} -> {timezone}")
    except pytz.exceptions.UnknownTimeZoneError:
        print(f"Warning: Unknown timezone '{timezone_str}', falling back to UTC")
        timezone = pytz.UTC
        timezone_str = "UTC"
    
    # Charger limits config
    max_current = get_config_value('charger_limits', 'max_current', 30, getattr(args, 'max_current', None))
    min_current = get_config_value('charger_limits', 'min_current', 6, getattr(args, 'min_current', None))
    bus_maximum = get_config_value('system', 'bus_maximum', 7000, getattr(args, 'bus_maximum', None))
    buffer = get_config_value('system', 'buffer', 100, getattr(args, 'buffer', None))
    on_to_off_lockout = get_config_value('charger_limits', 'on_to_off_lockout', 60, getattr(args, 'on_to_off_lockout', None))
    off_to_on_lockout = get_config_value('charger_limits', 'off_to_on_lockout', 240, getattr(args, 'off_to_on_lockout', None))

    # Validate required parameters
    if not solax_ip:
        parser.error("Solax IP address is required (via --ip_address or config file)")
    if not solax_serial:
        parser.error("Solax serial number is required (via --serial_number or config file)")
    if not mqtt_broker:
        parser.error("MQTT broker is required (via --broker or config file)")
    if not primary_charger:
        parser.error("Primary charger name is required (via --primary_charger or config file)")

    mqtt_settings = Settings.MQTT(host=mqtt_broker, username=mqtt_username, password=mqtt_password)
    
    # Set verbose flags on functions
    get_inverter_data.verbose = verbose
    get_emporia_chargers.verbose = verbose
    PowerCalculator.verbose = verbose

    # Create time-based controller
    time_controller = TimeBasedController(
        switch_on_time=switch_on_time,
        switch_off_time=switch_off_time,
        fixed_charge_start=fixed_charge_start,
        fixed_charge_end=fixed_charge_end,
        fixed_charge_current=fixed_charge_current,
        min_excess_threshold=min_excess_threshold,
        battery_soc_threshold=battery_soc_threshold,
        timezone=timezone,
        min_current=min_current,
        max_current=max_current
    )
    time_controller.verbose = verbose  # Set verbose flag on time controller
    
    # Setup inverter sensors using OO approach
    inverter_sensors = InverterSensorManager(mqtt_settings)

    vue = pyemvue.PyEmVue()
    if not vue.login(token_storage_file=args.creds_file):
        print("Failed to log in to Enphase Encharge. Please check your credentials.")
        return None
        
    try:
        vue_devices = vue.get_devices()
    except Exception as e:
        print(e)
        return None
        
    charger_by_id = {}
    charger_by_name = {}
    for device in vue_devices:
        if device.model == 'VVDN01':
            charger_by_id[device.device_gid] = device
            charger_by_name[device.device_name] = device
            print(f'Found charger: {device.model} {device.device_name} ({device.device_gid})')

    if len(charger_by_name) == 0:
        print("Warning: No EV chargers found in Emporia account")
        return

    # Validate primary charger name
    if primary_charger not in charger_by_name:
        print(f"Warning: Primary charger '{primary_charger}' not found in available chargers: {list(charger_by_name.keys())}")
        print("All chargers will be treated equally (no primary designation)")
        primary_charger_name = None
    else:
        primary_charger_name = primary_charger
        print(f"Primary charger set to: {primary_charger_name}")

    # Create charger controllers using OO approach
    controllers = {}
    for charger_name in charger_by_name.keys():
        is_primary = (charger_name == primary_charger_name)
        controller = ChargerController(
            vue=vue,
            charger_name=charger_name,
            mqtt_settings=mqtt_settings,
            upper_limit=max_current,
            lower_limit=min_current,
            voltage=240,  # This is still hardcoded, could be a future enhancement
            on_to_off_lockout=on_to_off_lockout,
            off_to_on_lockout=off_to_on_lockout,
            bus_maximum=bus_maximum,
            buffer=buffer,
            is_primary=is_primary,
            time_controller=time_controller
        )
        controller.verbose = verbose  # Set verbose flag on controller
        controllers[charger_name] = controller
        
        if verbose:
            print(f"[debug] Configured {charger_name} as {'Primary' if is_primary else 'Secondary'} charger")

    # Initialize the power validator with the configured threshold
    power_validator = PowerValidator(max_power_threshold=max_power_threshold)
    power_validator.verbose = verbose  # Set verbose flag on validator

    if verbose:
        print(f"[debug] Initialized power validator with threshold: {max_power_threshold}W")
    
    while True:
        inverter_data = get_inverter_data(solax_ip, solax_serial)
        if not inverter_data:
            print("Failed to retrieve inverter data.")
            time.sleep(sleep_interval)
            continue
            
        data = inverter_data['Data']
        inverter_data = decode_solax_data(inverter_data, power_validator)

        # Initialize averaging buffers if this is the first run
        if not hasattr(main, 'battery_power_history'):
            main.battery_power_history = []
            main.max_power_samples = int(power_avg_window * 60 / sleep_interval)  # Convert minutes to samples

        # Update battery power averaging
        current_battery_power = inverter_data['Power/Battery'] / 1000  # Convert to kW
        avg_battery_power = PowerCalculator.update_power_average(
            current_battery_power, main.battery_power_history, main.max_power_samples
        )

        # Calculate time estimates
        time_to_charged = PowerCalculator.calculate_time_to_charged(
            inverter_data['Battery/SOC'], battery_capacity, max(0, avg_battery_power)
        )
        time_to_depleted = PowerCalculator.calculate_time_to_depleted(
            inverter_data['Battery/SOC'], min_soc, battery_capacity, max(0, -avg_battery_power)
        )

        for key, value in inverter_data.items():
            inverter_sensors.update_sensor(key, value, verbose=verbose)

        # Update new battery time sensors
        inverter_sensors.update_sensor('Battery/TimeToCharged', time_to_charged, verbose=verbose)
        inverter_sensors.update_sensor('Battery/TimeToDepleted', time_to_depleted, verbose=verbose)
        inverter_sensors.update_sensor('Battery/Power', avg_battery_power, verbose=verbose)
        inverter_sensors.update_sensor('Battery/MinSOC', min_soc, verbose=verbose)

        charger_data = get_emporia_chargers(vue)
        if not charger_data:
            print("Failed to retrieve charger data.")
            time.sleep(sleep_interval)
            continue
        
        # Update all controllers and collect status and actions
        charger_statuses = []
        charger_actions = []
        for controller in controllers.values():
            controller.update(charger_data)
            if verbose:
                print(f"[debug] Controlling {controller.charger_name}: Connected={controller.charger_connected}, Current={controller.charger_current}A, Power={controller.charger_load:.0f}W")
            status, action = controller.control(charger_data, inverter_data, controllers)
            charger_statuses.append(status)
            if action:
                charger_actions.append(action)
            
        if verbose:
            print(f"[debug] Completed control cycle for {len(controllers)} chargers")
        
        # Calculate system-wide metrics
        total_charger_power = sum(status.power_watts for status in charger_statuses)
        active_charger_names = [status.name for status in charger_statuses if status.charging]
        primary_charger_active = any(status.is_primary and status.charging for status in charger_statuses)

        # Calculate power metrics for logging
        power_metrics = PowerCalculator.calculate_base_power_metrics(inverter_data, buffer)  # Using configured buffer
        reserve_for_battery = PowerCalculator.calculate_battery_reserve(power_metrics['soc_battery'])
        power_availability = PowerCalculator.calculate_available_power(
            power_metrics, total_charger_power, bus_maximum, reserve_for_battery  # Using configured bus_maximum
        )

        # Get current time in both UTC and configured timezone
        utc_now = datetime.datetime.now(pytz.UTC)
        local_now = utc_now.astimezone(timezone)

        # Create comprehensive system status
        system_status = SystemStatus(
            timestamp=local_now.strftime("%H:%M:%S"),
            battery_soc=inverter_data['Battery/SOC'],
            battery_voltage=inverter_data['Battery/Voltage'],
            battery_temperature=inverter_data['Battery/Temperature'],
            solar_production=inverter_data['Power/FromSolar'],
            house_consumption=inverter_data['Power/ToHome'],
            grid_import=inverter_data['Power/FromGrid'],
            grid_export=inverter_data['Power/ToGrid'],
            battery_charge=inverter_data['Power/ToBattery'],
            battery_discharge=inverter_data['Power/FromBattery'],
            battery_reserve_allocation=reserve_for_battery,
            total_charger_power=total_charger_power,
            available_excess=power_availability['available_for_charge'],
            chargers=charger_statuses,
            primary_charger_active=primary_charger_active,
            active_charger_names=active_charger_names,
            time_to_charged=time_to_charged,
            time_to_depleted=time_to_depleted,
            battery_power_kw=avg_battery_power,
            min_soc=min_soc,
            battery_power_history=main.battery_power_history.copy(),
            max_power_samples=main.max_power_samples
        )
        
        # Prepare calculations data for logging
        calculations = {
            "power_metrics": power_metrics,
            "reserve_for_battery": reserve_for_battery,
            "power_availability": power_availability,
            "total_charger_power": total_charger_power,
            "active_charger_names": active_charger_names,
            "primary_charger_active": primary_charger_active,
            "avg_battery_power": avg_battery_power,
            "time_to_charged": time_to_charged,
            "time_to_depleted": time_to_depleted,
            "battery_power_history": main.battery_power_history.copy(),
            "max_power_samples": main.max_power_samples,
            "min_soc": min_soc,
            "battery_capacity": battery_capacity
        }

        # Log comprehensive data to JSON file
        log_iteration_data(
            raw_inverter_data=inverter_data,
            raw_charger_data=charger_data,
            decoded_inverter_data=inverter_data,
            calculations=calculations,
            actions=charger_actions,
            timezone=timezone,
            timezone_str=timezone_str,
            detailed_log=detailed_log
        )

        # Display comprehensive log
        # Display comprehensive log (only if not verbose, verbose mode shows detailed logs above)
        if not verbose:
            print(system_status.format_comprehensive_log())

        time.sleep(sleep_interval)


if __name__ == "__main__":
    main()
