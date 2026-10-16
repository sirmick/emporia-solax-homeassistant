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
import logging
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

from charge_policy import ChargePolicy, ChargerReading, Command, Flows, PolicyConfig

# Set up logger
logger = logging.getLogger("solax_emporia")

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
        logger.debug(f"Inverter API call successful to {ip_address}")
        logger.debug(f"Response status: {response.status_code}")
        logger.debug(f"Data array length: {len(data.get('Data', []))}")
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
        logger.debug(f"Emporia API call successful")
        logger.debug(f"Found {len(charger_by_id)} chargers: {list(charger_by_name.keys())}")
        logger.debug(f"Retrieved power data for {len(vue_power)} devices")
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

        # Log charger status
        power_kw = power_watts / 1000
        logger.debug(f"{name}: {power_kw:.1f}kW | {current_amps}A | Status: {status} | Message: {message}")

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
    policy: str = ""


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

    # Charge policy (watts)
    surplus: float  # averaged: export - import - battery discharge - battery reserve shortfall
    total_charger_power: float
    budget: float  # what the chargers may draw

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
                + (f" ({charger.policy})" if charger.policy else "")
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
        budget_kw = self.budget / 1000
        surplus_kw = self.surplus / 1000

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
            f"🔄 {surplus_kw:+.1f}kW",
            f"☀️  {solar_kw:.1f}kW",
            f"🏠 {house_kw:.1f}kW",
            f"{grid_flow}{grid_text_formatted}",
            f"⚡ {budget_kw:.1f}kW",
            f"🚗 {' | '.join(charger_summaries)}"
        ]

        return " | ".join(log_parts)


def hhmm_to_minutes(hhmm: str):
    """'HH:MM' -> minutes for HA duration sensors; 'None' (HA's unknown payload) for 'N/A'."""
    if hhmm == "N/A":
        return "None"
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


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
            ('Battery/Temperature', 'temperature', '°C'),
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
        logger.debug(f'Registering MQTT sensor: {sensor_id}')
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
    """Battery time estimates for the status line / HA sensors."""

    @staticmethod
    def calculate_time_to_charged(soc_battery: int, battery_capacity: float, charging_power: float) -> str:
        """Time to full battery as HH:MM ("N/A" when not charging). charging_power in kW."""
        if charging_power <= 0 or soc_battery >= 100:
            return "N/A"
        time_hours = (100 - soc_battery) / 100 * battery_capacity / charging_power
        hours = int(time_hours)
        minutes = int((time_hours - hours) * 60)
        return f"{hours:02d}:{minutes:02d}"

    @staticmethod
    def calculate_time_to_depleted(soc_battery: int, min_soc: int, battery_capacity: float, discharging_power: float) -> str:
        """Time to min SOC as HH:MM ("N/A" when not discharging). discharging_power in kW."""
        if discharging_power <= 0 or soc_battery <= min_soc:
            return "N/A"
        time_hours = (soc_battery - min_soc) / 100 * battery_capacity / discharging_power
        hours = int(time_hours)
        minutes = int((time_hours - hours) * 60)
        return f"{hours:02d}:{minutes:02d}"

    @staticmethod
    def update_power_average(current_power: float, power_history: list, max_samples: int) -> float:
        """Append to a rolling window of max_samples and return its mean."""
        power_history.append(current_power)
        if len(power_history) > max_samples:
            power_history.pop(0)
        if power_history:
            return sum(power_history) / len(power_history)
        return 0.0


CONNECTED_MESSAGES = ('Connected to EV', 'Charging', 'Please Wait')


class ChargerEntities:
    """Home Assistant entities for one charger: the solar switch plus current/power/policy sensors."""

    def __init__(self, charger_name: str, mqtt_settings: Settings.MQTT):
        self.name = charger_name
        self.solar_enabled = True
        slug = charger_name.lower().replace(' ', '_')
        device_info = DeviceInfo(name=charger_name, identifiers=slug)

        def switch_changed(client: Client, user_data, message: MQTTMessage):
            payload = message.payload.decode()
            if payload not in ("ON", "OFF"):
                return
            self.solar_enabled = payload == "ON"
            if self.solar_enabled:
                self.solar_ha.on()
            else:
                self.solar_ha.off()
            print(f"🔀 {self.name}: solar charging {'enabled' if self.solar_enabled else 'disabled'} from Home Assistant")

        # "Use Excess Solar": off keeps this charger out of solar charging (the night window still applies).
        self.solar_ha = Switch(Settings(mqtt=mqtt_settings, entity=SwitchInfo(
            device=device_info,
            name='Use Excess Solar',
            unique_id=f'{slug}_use_excess',
        )), switch_changed)
        self.solar_ha.on()
        self.current_ha = Sensor(Settings(mqtt=mqtt_settings, entity=SensorInfo(
            name='Current',
            device_class='current',
            unique_id=f'{slug}_current',
            unit_of_measurement='A',
            device=device_info
        )))
        self.power_ha = Sensor(Settings(mqtt=mqtt_settings, entity=SensorInfo(
            name='Power',
            device_class='power',
            unique_id=f'{slug}_power',
            unit_of_measurement='W',
            device=device_info
        )))
        self.policy_ha = Sensor(Settings(mqtt=mqtt_settings, entity=SensorInfo(
            name='Charge Policy',
            unique_id=f'{slug}_charge_policy',
            icon='mdi:ev-station',
            device=device_info
        )))
        self.last_policy = None

    def publish(self, charger: dict, policy_status: str) -> None:
        self.current_ha.set_state(charger['current'])
        self.power_ha.set_state(charger['power'])
        if policy_status != self.last_policy:
            self.policy_ha.set_state(policy_status or "unknown")
            self.last_policy = policy_status


def charger_readings(charger_data: dict, entities: Dict[str, ChargerEntities]) -> List[ChargerReading]:
    """Emporia charger data -> policy inputs."""
    readings = []
    for name, ch in charger_data.items():
        readings.append(ChargerReading(
            name=name,
            connected=ch['message'] in CONNECTED_MESSAGES,
            on=bool(ch['on']),
            current=int(ch['current'] or 0),
            power_w=float(ch['power'] or 0),
            max_rate=int(ch.get('max_charging_rate') or 48),
            solar_enabled=entities[name].solar_enabled if name in entities else True,
        ))
    return readings


def apply_command(vue: pyemvue.PyEmVue, charger_data: dict, cmd: Command) -> Optional[ChargerAction]:
    """Send one policy command to the Emporia API."""
    ch = charger_data[cmd.name]
    before = f"{'ON' if ch['on'] else 'OFF'} {ch['current']}A"
    after = f"{'ON' if cmd.on else 'OFF'} {cmd.current}A"
    try:
        vue.update_charger(ch['ev_charger'], on=cmd.on, charge_rate=cmd.current)
    except Exception as e:
        print(f"❌ {cmd.name}: {before} -> {after} failed: {e}")
        return None
    print(f"🔌 {cmd.name}: {before} -> {after} | {cmd.reason}")
    return ChargerAction(
        charger_name=cmd.name,
        action_type='state_change' if bool(ch['on']) != cmd.on else 'current_change',
        old_current=ch['current'],
        new_current=cmd.current,
        old_state=bool(ch['on']),
        new_state=cmd.on,
        reason=cmd.reason,
        values={},
    )


def main() -> None:
    """Poll the inverter and chargers, run the charge policy, publish to Home Assistant."""
    parser = argparse.ArgumentParser(
        description="Solar-aware EV charge control for a Solax inverter and Emporia chargers."
    )
    parser.add_argument("ip_address", nargs='?', help="The IP address of your Solax inverter (e.g., 192.168.2.117)")
    parser.add_argument("serial_number", nargs='?', help="The serial number of your Solax inverter, used as the password")
    parser.add_argument("broker", nargs='?', help="MQTT broker")
    parser.add_argument("primary_charger", nargs='?', help="Name of the charger that gets priority for solar")
    # No defaults on the optional args below: a default would override the config file value.
    parser.add_argument("-u", '--username', help="MQTT username")
    parser.add_argument("-p", '--password', help="MQTT password")
    parser.add_argument("-s", '--sleep', help="Poll delay", type=int)
    parser.add_argument("-c", '--creds-file', help="Emporia creds file", type=str, default='keys.json')
    parser.add_argument("-v", '--verbose', help="Enable verbose logging for debugging", action='store_true')
    parser.add_argument("--battery-capacity", help="Battery capacity in kWh", type=float)
    parser.add_argument("--min-soc", help="Minimum battery SOC for depletion estimates", type=int)
    parser.add_argument("--power-avg-window", help="Minutes to average battery power over", type=int)
    parser.add_argument("--max-power-threshold", help="Readings above this many watts are treated as spurious",
                        type=int)
    parser.add_argument("--config", help="Path to configuration JSON file", type=str, default="config.json")
    parser.add_argument("--timezone", help="Timezone, e.g. 'America/Los_Angeles'", type=str)
    parser.add_argument("--detailed-log", help="Enable detailed JSON logging to poll_log.json",
                        action="store_true", default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="Read everything and log the policy's decisions, but never command the chargers "
                             "or publish to MQTT (safe to run next to a live instance)")
    args = parser.parse_args()

    config = {}
    if os.path.exists(args.config):
        try:
            with open(args.config, 'r') as f:
                config = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: Could not load config file {args.config}: {e}")
            print("Using command line arguments only.")

    # CLI takes precedence over the config file
    def get_config_value(section, key, default=None, arg_value=None):
        if arg_value is not None:
            return arg_value
        return config.get(section, {}).get(key, default)

    solax_ip = get_config_value('solax', 'ip_address', arg_value=args.ip_address)
    solax_serial = get_config_value('solax', 'serial_number', arg_value=args.serial_number)
    mqtt_broker = get_config_value('mqtt', 'broker', arg_value=args.broker)
    primary_charger = get_config_value('chargers', 'primary_charger', arg_value=args.primary_charger)
    mqtt_username = get_config_value('mqtt', 'username', 'a', args.username)
    mqtt_password = get_config_value('mqtt', 'password', 'a', args.password)
    sleep_interval = get_config_value('system', 'sleep_interval', 10, args.sleep)
    battery_capacity = get_config_value('system', 'battery_capacity', 20.0, args.battery_capacity)
    min_soc = get_config_value('system', 'min_soc', 20, args.min_soc)
    power_avg_window = get_config_value('system', 'power_avg_window', 5, args.power_avg_window)
    max_power_threshold = get_config_value('system', 'max_power_threshold', 50000, args.max_power_threshold)
    detailed_log = get_config_value('system', 'detailed_log', False, args.detailed_log)
    verbose = args.verbose

    if 'time_period_policies' in config or 'charger_limits' in config:
        print("Warning: 'time_period_policies' / 'charger_limits' are no longer used; "
              "the rules now live in 'charge_policy' (see README)")
    policy_section = dict(config.get('charge_policy', {}))
    timezone_str = args.timezone or policy_section.get('timezone') or config.get('system', {}).get('timezone', 'UTC')
    try:
        timezone = pytz.timezone(timezone_str)
    except pytz.exceptions.UnknownTimeZoneError:
        print(f"Warning: Unknown timezone '{timezone_str}', falling back to UTC")
        timezone, timezone_str = pytz.UTC, "UTC"
    policy_section['timezone'] = timezone_str
    policy_config, unknown = PolicyConfig.from_dict(policy_section)
    if unknown:
        print(f"Warning: unknown charge_policy keys ignored: {', '.join(unknown)}")

    if not solax_ip:
        parser.error("Solax IP address is required (via --ip_address or config file)")
    if not solax_serial:
        parser.error("Solax serial number is required (via --serial_number or config file)")
    if not mqtt_broker:
        parser.error("MQTT broker is required (via --broker or config file)")
    if not primary_charger:
        parser.error("Primary charger name is required (via --primary_charger or config file)")

    mqtt_settings = Settings.MQTT(host=mqtt_broker, username=mqtt_username, password=mqtt_password)

    logging.basicConfig(level=logging.WARNING, format='%(message)s')
    logging.getLogger('ha_mqtt_discoverable').setLevel(logging.ERROR)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(console_handler)
    # Own handler above; don't also bubble up to basicConfig's root handler (double lines)
    logger.propagate = False

    print(f"Charge policy: {policy_config}")
    dry_run = args.dry_run
    if dry_run:
        print("DRY RUN: chargers will not be commanded and nothing is published to MQTT")

    inverter_sensors = None if dry_run else InverterSensorManager(mqtt_settings)

    vue = pyemvue.PyEmVue()
    if not vue.login(token_storage_file=args.creds_file):
        print("Failed to log in to Emporia. Please check your credentials.")
        return None

    try:
        vue_devices = vue.get_devices()
    except Exception as e:
        print(e)
        return None

    charger_names = []
    for device in vue_devices:
        if device.model == 'VVDN01':
            charger_names.append(device.device_name)
            print(f'Found charger: {device.model} {device.device_name} ({device.device_gid})')

    if not charger_names:
        print("Warning: No EV chargers found in Emporia account")
        return

    if primary_charger not in charger_names:
        print(f"Warning: Primary charger '{primary_charger}' not found in available chargers: {charger_names}")
        print("Chargers will share solar in discovery order")
        priority = charger_names
    else:
        print(f"Primary charger set to: {primary_charger}")
        priority = [primary_charger] + [n for n in charger_names if n != primary_charger]

    policy = ChargePolicy(policy_config, priority)
    entities = {} if dry_run else {name: ChargerEntities(name, mqtt_settings) for name in charger_names}
    power_validator = PowerValidator(max_power_threshold=max_power_threshold)
    battery_power_history = []
    max_power_samples = int(power_avg_window * 60 / sleep_interval)

    while True:
        raw_inverter_data = get_inverter_data(solax_ip, solax_serial)
        if not raw_inverter_data:
            print("Failed to retrieve inverter data.")
            time.sleep(sleep_interval)
            continue
        inverter_data = decode_solax_data(raw_inverter_data, power_validator)

        avg_battery_power = PowerCalculator.update_power_average(
            inverter_data['Power/Battery'] / 1000, battery_power_history, max_power_samples
        )
        time_to_charged = PowerCalculator.calculate_time_to_charged(
            inverter_data['Battery/SOC'], battery_capacity, max(0, avg_battery_power)
        )
        time_to_depleted = PowerCalculator.calculate_time_to_depleted(
            inverter_data['Battery/SOC'], min_soc, battery_capacity, max(0, -avg_battery_power)
        )

        if inverter_sensors:
            for key, value in inverter_data.items():
                inverter_sensors.update_sensor(key, value, verbose=verbose)
            inverter_sensors.update_sensor('Battery/TimeToCharged', hhmm_to_minutes(time_to_charged), verbose=verbose)
            inverter_sensors.update_sensor('Battery/TimeToDepleted', hhmm_to_minutes(time_to_depleted),
                                           verbose=verbose)
            inverter_sensors.update_sensor('Battery/Power', avg_battery_power, verbose=verbose)
            inverter_sensors.update_sensor('Battery/MinSOC', min_soc, verbose=verbose)

        charger_data = get_emporia_chargers(vue)
        if not charger_data:
            print("Failed to retrieve charger data.")
            time.sleep(sleep_interval)
            continue

        flows = Flows(
            grid_w=inverter_data['Power/Grid'],
            battery_w=inverter_data['Power/Battery'],
            soc=inverter_data['Battery/SOC'],
        )
        now = datetime.datetime.now(pytz.UTC)
        readings = charger_readings(charger_data, entities)
        commands = policy.step(now, flows, readings)
        if dry_run:
            for cmd in commands:
                print(f"🧪 {cmd.name}: would set {'ON' if cmd.on else 'OFF'} {cmd.current}A | {cmd.reason}")
            actions = []
        else:
            actions = [a for a in (apply_command(vue, charger_data, cmd) for cmd in commands) if a]

        statuses = []
        for r in readings:
            ch = charger_data[r.name]
            for a in actions:  # show what we just asked for rather than the pre-command reading
                if a.charger_name == r.name:
                    ch = dict(ch, on=a.new_state, current=a.new_current)
            if r.name in entities:
                entities[r.name].publish(ch, policy.status(r.name))
            statuses.append(ChargerStatus(
                name=r.name,
                is_primary=r.name == priority[0],
                connected=r.connected,
                charging=r.power_w > 100,
                current_amps=ch['current'],
                power_watts=r.power_w,
                proposed_amps=ch['current'],
                state_active=bool(ch['on']),
                policy=policy.status(r.name),
            ))
        logger.debug(f"Policy: surplus {policy.surplus_w:.0f}W, budget {policy.budget_w:.0f}W, "
                     f"flows grid {flows.grid_w:.0f}W battery {flows.battery_w:.0f}W soc {flows.soc}%")

        system_status = SystemStatus(
            timestamp=now.astimezone(timezone).strftime("%H:%M:%S"),
            battery_soc=inverter_data['Battery/SOC'],
            battery_voltage=inverter_data['Battery/Voltage'],
            battery_temperature=inverter_data['Battery/Temperature'],
            solar_production=inverter_data['Power/FromSolar'],
            house_consumption=inverter_data['Power/ToHome'],
            grid_import=inverter_data['Power/FromGrid'],
            grid_export=inverter_data['Power/ToGrid'],
            battery_charge=inverter_data['Power/ToBattery'],
            battery_discharge=inverter_data['Power/FromBattery'],
            surplus=policy.surplus_w,
            total_charger_power=sum(s.power_watts for s in statuses),
            budget=policy.budget_w,
            chargers=statuses,
            primary_charger_active=any(s.is_primary and s.charging for s in statuses),
            active_charger_names=[s.name for s in statuses if s.charging],
            time_to_charged=time_to_charged,
            time_to_depleted=time_to_depleted,
            battery_power_kw=avg_battery_power,
            min_soc=min_soc,
            battery_power_history=battery_power_history.copy(),
            max_power_samples=max_power_samples,
        )

        log_iteration_data(
            raw_inverter_data=raw_inverter_data,
            raw_charger_data=charger_data,
            decoded_inverter_data=inverter_data,
            calculations={
                "surplus_w": policy.surplus_w,
                "budget_w": policy.budget_w,
                "policy": {name: policy.status(name) for name in charger_names},
                "avg_battery_power": avg_battery_power,
                "time_to_charged": time_to_charged,
                "time_to_depleted": time_to_depleted,
            },
            actions=actions,
            timezone=timezone,
            timezone_str=timezone_str,
            detailed_log=detailed_log,
        )

        if not verbose:
            print(system_status.format_comprehensive_log())

        time.sleep(sleep_interval)


if __name__ == "__main__":
    main()
