"""Local control of Midea dehumidifiers, published to Home Assistant over MQTT.

The protocol work is done by midea-lan (the library behind the midea_ac_lan
integration); this module only maps a device's attributes to MQTT discovery
entities and MQTT commands back to the device. Entity names, units and IDs
follow midea_ac_lan, so the entities can take over its entity IDs.
"""

import asyncio
import logging

log = logging.getLogger("midea_fakecloud.lan")

DEHUMIDIFIER = 0xA1


def on_off(value) -> str:
    return "ON" if value else "OFF"


class DeviceBridge:
    """One appliance: a midea-lan device thread plus its MQTT entities."""

    def __init__(self, cfg: dict, mqtt_publish, loop, device_factory=None):
        self.cfg = cfg
        self.device_id = int(cfg["device_id"])
        self.name = cfg.get("name") or "Dehumidifier"
        self.ip = cfg["ip"]
        self.prefix = cfg.get("entity_id_prefix") or str(self.device_id)
        self.base = f"midea_fakecloud/dev/{self.device_id}"
        self.publish = mqtt_publish
        self.loop = loop
        self.available = False
        self.device = (device_factory or make_device)(cfg)
        self.device.register_update(self._from_thread)

    # --- device side -------------------------------------------------

    def start(self) -> None:
        self.device.daemon = True  # never hold up the add-on's exit
        self.device.open()
        log.info("%s: connecting to %s:%s", self.name, self.ip, self.cfg.get("port") or 6444)

    def stop(self) -> None:
        self.device.close()

    def _from_thread(self, status: dict) -> None:
        """midea-lan calls this from its device thread."""
        self.loop.call_soon_threadsafe(self._apply, dict(status))

    def _apply(self, status: dict) -> None:
        if "available" in status and status["available"] != self.available:
            self.available = bool(status["available"])
            log.info("%s: %s", self.name, "connected" if self.available else "unavailable")
            self.publish(f"{self.base}/availability", "online" if self.available else "offline")
        self.publish_state()

    def state(self) -> dict:
        s = {str(k): v for k, v in self.device.attributes.items()}
        s["power_state"] = on_off(s.get("power"))
        s["prompt_tone_state"] = on_off(s.get("prompt_tone"))
        s["tank_full_state"] = on_off(s.get("tank_full"))
        s["filter_state"] = on_off(s.get("filter_cleaning_reminder"))
        return s

    def publish_state(self) -> None:
        self.publish(f"{self.base}/state", self.state())

    async def command(self, attr: str, payload: str) -> None:
        """Apply an MQTT command; the device's reply updates the state."""
        p = payload.strip()
        if attr in ("power", "prompt_tone"):
            value = p.upper() in ("ON", "TRUE", "1")
        elif attr == "target_humidity":
            value = int(round(float(p)))
            value = max(35, min(85, value))
        elif attr == "mode" and p in self.device.modes:
            value = p
        elif attr == "fan_speed" and p in self.device.fan_speeds:
            value = p
        else:
            log.warning("%s: ignoring %s=%r", self.name, attr, payload)
            return
        log.info("%s: set %s=%s", self.name, attr, value)
        await asyncio.to_thread(self.device.set_attribute, attr, value)
        if attr == "prompt_tone":  # local-only setting, no device reply
            self.publish_state()

    # --- MQTT discovery ----------------------------------------------

    def device_info(self) -> dict:
        info = {"identifiers": [f"midea_{self.device_id}"], "name": self.name,
                "manufacturer": "Midea", "model": self.cfg.get("model") or "Dehumidifier"}
        if self.cfg.get("area"):
            info["suggested_area"] = self.cfg["area"]
        return info

    def announce(self) -> None:
        state = f"{self.base}/state"
        common = {
            "device": self.device_info(),
            "availability": [{"topic": "midea_fakecloud/status"}, {"topic": f"{self.base}/availability"}],
            "availability_mode": "all",
        }
        entities = [
            ("humidifier", "humidifier", {
                "name": None, "device_class": "dehumidifier", "icon": "mdi:air-humidifier",
                "state_topic": state, "state_value_template": "{{ value_json.power_state }}",
                "command_topic": f"{self.base}/set/power",
                "target_humidity_state_topic": state,
                "target_humidity_state_template": "{{ value_json.target_humidity }}",
                "target_humidity_command_topic": f"{self.base}/set/target_humidity",
                "current_humidity_topic": state,
                "current_humidity_template": "{{ value_json.current_humidity }}",
                "mode_state_topic": state, "mode_state_template": "{{ value_json.mode }}",
                "mode_command_topic": f"{self.base}/set/mode", "modes": self.device.modes,
                "min_humidity": 35, "max_humidity": 85}),
            ("select", "fan_speed", {
                "name": "Fan Speed", "icon": "mdi:fan", "options": self.device.fan_speeds,
                "state_topic": state, "value_template": "{{ value_json.fan_speed }}",
                "command_topic": f"{self.base}/set/fan_speed"}),
            ("switch", "prompt_tone", {
                "name": "Prompt Tone", "icon": "mdi:bell", "entity_category": "config",
                "state_topic": state, "value_template": "{{ value_json.prompt_tone_state }}",
                "command_topic": f"{self.base}/set/prompt_tone"}),
            ("sensor", "current_humidity", {
                "name": "Current Humidity", "device_class": "humidity", "unit_of_measurement": "%",
                "state_class": "measurement",
                "state_topic": state, "value_template": "{{ value_json.current_humidity }}"}),
            ("sensor", "current_temperature", {
                "name": "Current Temperature", "device_class": "temperature",
                "unit_of_measurement": "°C", "state_class": "measurement", "suggested_display_precision": 1,
                "state_topic": state, "value_template": "{{ value_json.current_temperature }}"}),
            ("sensor", "tank", {
                "name": "Tank", "icon": "mdi:cup-water", "unit_of_measurement": "%",
                "state_class": "measurement",
                "state_topic": state, "value_template": "{{ value_json.tank }}"}),
            ("binary_sensor", "tank_full", {
                "name": "Tank status", "icon": "mdi:alert-circle", "device_class": "problem",
                "state_topic": state, "value_template": "{{ value_json.tank_full_state }}"}),
            ("binary_sensor", "filter_cleaning_reminder", {
                "name": "Filter Cleaning Reminder", "icon": "mdi:alert-circle", "device_class": "problem",
                "state_topic": state, "value_template": "{{ value_json.filter_state }}"}),
        ]
        for component, key, cfg in entities:
            uid = f"midea_{self.device_id}_{key}"
            self.publish(f"homeassistant/{component}/{uid}/config", {
                **common, **cfg, "unique_id": uid,
                "default_entity_id": f"{component}.{self.prefix}_{key}"})
        self.publish(f"{self.base}/availability", "online" if self.available else "offline")


def make_device(cfg: dict):
    """Build the midea-lan device for one configured appliance."""
    from midealan.const import ProtocolVersion
    from midealan.devices import device_selector

    device_type = int(cfg.get("type") or DEHUMIDIFIER)
    if device_type != DEHUMIDIFIER:
        raise ValueError(f"device type {device_type:#x} is not supported yet (dehumidifiers only)")
    device = device_selector(
        name=cfg.get("name") or "Dehumidifier", device_id=int(cfg["device_id"]),
        device_type=device_type, ip_address=cfg["ip"], port=int(cfg.get("port") or 6444),
        token=cfg["token"], key=cfg["key"], device_protocol=ProtocolVersion(int(cfg.get("protocol") or 3)),
        model=cfg.get("model") or "", subtype=int(cfg.get("subtype") or 0), customize="")
    device.set_refresh_interval(int(cfg.get("refresh_interval") or 30))
    return device


def parse_command(topic: str):
    """midea_fakecloud/dev/<device_id>/set/<attr> -> (device_id, attr)."""
    parts = topic.split("/")
    if len(parts) == 5 and parts[:2] == ["midea_fakecloud", "dev"] and parts[3] == "set":
        try:
            return int(parts[2]), parts[4]
        except ValueError:
            return None
    return None
