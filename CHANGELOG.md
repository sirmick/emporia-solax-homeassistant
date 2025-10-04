# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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