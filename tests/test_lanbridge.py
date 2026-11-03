"""Tests for local control: the MQTT bridge and the MQTT client's subscriptions.

A fake device stands in for midea-lan, so these run without the library or
an appliance. test_real_library runs only where midea-lan is installed
(Python 3.12+).
"""

import asyncio
import json
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "midea_fakecloud"))
import fakecloud  # noqa: E402
import lanbridge  # noqa: E402

CFG = {"name": "Dehumidifier", "ip": "192.0.2.10", "device_id": 151700000000001,
       "token": "00" * 64, "key": "00" * 32, "model": "00000Q1A", "area": "Bedroom"}


class FakeDevice:
    """Behaves like a midea-lan A1 device: replies arrive from another thread."""

    modes = ["manual", "continuous", "auto", "clothes_dry", "shoes_dry"]
    fan_speeds = ["lowest", "low", "medium", "high", "auto", "off"]

    def __init__(self, cfg):
        self.attributes = {"power": True, "prompt_tone": True, "mode": "manual", "fan_speed": "high",
                           "target_humidity": 35, "tank": 0, "tank_full": False,
                           "current_humidity": 41, "current_temperature": 25.0,
                           "filter_cleaning_reminder": True}
        self.updates = []
        self.sets = []
        self.opened = False

    def register_update(self, fn):
        self.updates.append(fn)

    def open(self):
        self.opened = True
        self._reply({"available": True})

    def close(self):
        self.opened = False

    def set_attribute(self, attr, value):
        self.sets.append((attr, value))
        self.attributes[attr] = value
        if attr != "prompt_tone":  # the real device answers a set with its new status
            self._reply({attr: value})

    def _reply(self, status):
        t = threading.Thread(target=lambda: [fn(status) for fn in self.updates])
        t.start()
        t.join()


class BridgeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sent = []
        self.bridge = lanbridge.DeviceBridge(CFG, lambda t, p: self.sent.append((t, p)),
                                             asyncio.get_running_loop(), FakeDevice)

    def last_state(self):
        return next(p for t, p in reversed(self.sent) if t.endswith("/state"))

    async def test_discovery_keeps_midea_ac_lan_entity_ids(self):
        self.bridge.announce()
        configs = {t: p for t, p in self.sent if t.startswith("homeassistant/")}
        ids = sorted(p["default_entity_id"] for p in configs.values())
        self.assertEqual(ids, [
            "binary_sensor.151700000000001_filter_cleaning_reminder",
            "binary_sensor.151700000000001_tank_full",
            "humidifier.151700000000001_humidifier",
            "select.151700000000001_fan_speed",
            "sensor.151700000000001_current_humidity",
            "sensor.151700000000001_current_temperature",
            "sensor.151700000000001_tank",
            "switch.151700000000001_prompt_tone",
        ])
        hum = configs["homeassistant/humidifier/midea_151700000000001_humidifier/config"]
        self.assertEqual(hum["modes"], FakeDevice.modes)
        self.assertEqual((hum["min_humidity"], hum["max_humidity"]), (35, 85))
        self.assertEqual(hum["device"]["suggested_area"], "Bedroom")

    async def test_prefix_option_changes_entity_ids(self):
        self.bridge.prefix = "dehumidifier_test"
        self.bridge.announce()
        ids = [p["default_entity_id"] for t, p in self.sent if t.startswith("homeassistant/")]
        self.assertIn("humidifier.dehumidifier_test_humidifier", ids)

    async def test_device_thread_updates_reach_mqtt(self):
        self.bridge.start()
        await asyncio.sleep(0.05)
        self.assertTrue(self.bridge.available)
        self.assertIn((f"{self.bridge.base}/availability", "online"), self.sent)
        state = self.last_state()
        self.assertEqual((state["power_state"], state["filter_state"], state["current_humidity"]), ("ON", "ON", 41))

    async def test_commands(self):
        dev = self.bridge.device
        await self.bridge.command("target_humidity", "45.0")
        await self.bridge.command("target_humidity", "99")  # clamped to the device range
        await self.bridge.command("mode", "continuous")
        await self.bridge.command("mode", "turbo")  # not a mode: ignored
        await self.bridge.command("fan_speed", "low")
        await self.bridge.command("power", "OFF")
        await self.bridge.command("prompt_tone", "OFF")
        await asyncio.sleep(0.05)
        self.assertEqual(dev.sets, [("target_humidity", 45), ("target_humidity", 85), ("mode", "continuous"),
                                    ("fan_speed", "low"), ("power", False), ("prompt_tone", False)])
        state = self.last_state()
        self.assertEqual((state["power_state"], state["prompt_tone_state"], state["mode"]), ("OFF", "OFF", "continuous"))

    async def test_cloud_sensors_join_the_appliance_device(self):
        app = fakecloud.App({"bind_address": "127.0.0.1", "port": 0, "upstream": ""},
                            Path(self._tmp()))
        app.bridges[self.bridge.device_id] = self.bridge

        class M:
            writer = True
            published = []

            def publish(self, t, p):
                self.published.append((t, p))

        app.mqtt = M()
        app.publish(app.module("192.0.2.10"))
        cfg = next(p for t, p in M.published if t.endswith("_connected/config"))
        self.assertEqual(cfg["device"]["identifiers"], ["midea_151700000000001"])

    async def test_command_topics_route_to_the_bridge(self):
        self.assertEqual(lanbridge.parse_command("midea_fakecloud/dev/151700000000001/set/mode"),
                         (151700000000001, "mode"))
        self.assertIsNone(lanbridge.parse_command("midea_fakecloud/dev/x/set/mode"))
        self.assertIsNone(lanbridge.parse_command("homeassistant/status"))

    def _tmp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        return self._td.name


class MqttClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_subscribes_and_receives_publishes(self):
        got = asyncio.get_running_loop().create_future()
        subscribed = asyncio.get_running_loop().create_future()

        async def broker(reader, writer):
            async def packet():
                first = (await reader.readexactly(1))[0]
                n, shift = 0, 0
                while True:
                    b = (await reader.readexactly(1))[0]
                    n |= (b & 0x7F) << shift
                    shift += 7
                    if not b & 0x80:
                        break
                return first, await reader.readexactly(n)

            first, _ = await packet()
            assert first == 0x10
            writer.write(b"\x20\x02\x00\x00")  # CONNACK
            first, body = await packet()
            assert first == 0x82
            tlen = int.from_bytes(body[2:4], "big")
            subscribed.set_result(body[4:4 + tlen].decode())
            writer.write(bytes([0x90, 3]) + body[:2] + b"\x00")  # SUBACK
            topic, payload = b"midea_fakecloud/dev/1/set/mode", b"auto"
            writer.write(fakecloud.Mqtt._packet(0x30, len(topic).to_bytes(2, "big") + topic + payload))
            await writer.drain()
            await asyncio.sleep(1)
            writer.close()

        server = await asyncio.start_server(broker, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = fakecloud.Mqtt("127.0.0.1", port, "u", "p", "midea_fakecloud/status")
        client.subscriptions = ["midea_fakecloud/dev/+/set/+"]
        client.on_message = lambda t, p: got.done() or got.set_result((t, p))

        async def connected():
            pass

        task = asyncio.create_task(client.run(connected))
        self.assertEqual(await asyncio.wait_for(subscribed, 3), "midea_fakecloud/dev/+/set/+")
        self.assertEqual(await asyncio.wait_for(got, 3), ("midea_fakecloud/dev/1/set/mode", b"auto"))
        task.cancel()
        server.close()


class RealLibraryTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_library(self):
        try:
            import midealan  # noqa: F401
        except ImportError:
            self.skipTest("midea-lan not installed (needs Python 3.12+)")
        sent = []
        bridge = lanbridge.DeviceBridge({**CFG, "subtype": 41377, "protocol": 3}, lambda t, p: sent.append((t, p)),
                                        asyncio.get_running_loop())
        bridge.announce()
        bridge.publish_state()
        hum = next(p for t, p in sent if t.endswith("_humidifier/config"))
        self.assertEqual(hum["modes"], ["manual", "continuous", "auto", "clothes_dry", "shoes_dry"])
        state = next(p for t, p in sent if t.endswith("/state"))
        json.dumps(state)  # serialisable as published
        self.assertIn("target_humidity", state)
        with self.assertRaises(ValueError):
            lanbridge.make_device({**CFG, "type": 0xAC})


if __name__ == "__main__":
    unittest.main()
