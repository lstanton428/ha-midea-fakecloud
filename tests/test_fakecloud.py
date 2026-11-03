"""End-to-end tests: a simulated module talks to the fake cloud over TCP.

Run with: python3 -m unittest discover -s tests
Uses synthetic frames only; no real recording is needed.
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["FAKECLOUD_INGRESS_PEERS"] = "127.0.0.1"
os.environ["FAKECLOUD_INGRESS_HOST"] = "127.0.0.1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "midea_fakecloud"))
import fakecloud  # noqa: E402

fakecloud.DISCONNECT_GRACE = 0.1


def frame(length: int, tag: int, mtype: int = 3) -> bytes:
    """A well-formed 8370 frame of the given total length, filled with tag."""
    return b"\x83\x70" + (length - 8).to_bytes(2, "big") + bytes([0x20, mtype]) + bytes([tag]) * (length - 6)


REQUEST = b"\x83\x70" + (416).to_bytes(2, "big") + b"\x20\x00\x00\x00" + bytes(range(256)) * 2
REQUEST = REQUEST[:424]
REPLY = frame(168, 0xAA, 1)
# Module frame -> cloud replies, in the order the real cloud's session went.
SCRIPT = [
    (frame(54, 1), []),
    (frame(214, 2), [frame(118, 0x10)]),
    (frame(150, 3), [frame(150, 0x11)]),
    (frame(102, 4), [frame(150, 0x12)]),
    (frame(102, 5), [frame(166, 0x13)]),
    (frame(134, 6), []),
    (frame(134, 7), [frame(182, 0x14), frame(118, 0x15)]),
    (frame(102, 8), [frame(102, 0x20)]),
    (frame(102, 9), [frame(102, 0x21)]),
    (frame(102, 10), [frame(102, 0x22)]),
]


def relay_log() -> str:
    """The script as a JSONL relay log, as the Pi prototype wrote it."""
    rows = [("module->cloud", REQUEST), ("cloud->module", REPLY)]
    for m, replies in SCRIPT:
        rows.append(("module->cloud", m))
        rows += [("cloud->module", r) for r in replies]
    return "\n".join(json.dumps({"event": "relay", "t": 1793700000 + i, "dir": d, "hex": f.hex()})
                     for i, (d, f) in enumerate(rows))


async def read_exact(reader, n, timeout=2):
    return await asyncio.wait_for(reader.readexactly(n), timeout)


async def http(port, method, path, body=b""):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(f"{method} {path} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
    await w.drain()
    data = await asyncio.wait_for(r.read(), 5)
    w.close()
    head, _, payload = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), payload


class FakeCloudTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.servers = []

    async def asyncTearDown(self):
        for s in self.servers:
            s.close()
        self.tmp.cleanup()

    async def start(self, name, options, recording=None):
        d = Path(self.tmp.name) / name
        d.mkdir()
        if recording:
            (d / "recordings").mkdir()
            rec = fakecloud.Recording.parse(recording)
            (d / "recordings" / f"{rec.variant}.json").write_text(json.dumps(rec.to_json()))
        app = fakecloud.App({"bind_address": "127.0.0.1", "port": 0, "capture_minutes": 0,
                             "upstream": "", **options}, d)
        servers = await app.start(ingress_port=0)
        self.servers += servers
        app.port = servers[0].sockets[0].getsockname()[1]
        app.http_port = servers[1].sockets[0].getsockname()[1]
        return app

    async def run_module(self, port, request=REQUEST):
        """Play the module side of SCRIPT; return the replies received."""
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(request)
        await w.drain()
        got = [await read_exact(r, len(REPLY))]
        for m, replies in SCRIPT:
            w.write(m)
            await w.drain()
            for expected in replies:
                got.append(await read_exact(r, len(expected)))
        w.close()
        return got

    async def test_replay_answers_in_recorded_order(self):
        app = await self.start("a", {}, relay_log())
        got = await self.run_module(app.port)
        expected = [REPLY] + [r for _, replies in SCRIPT for r in replies]
        self.assertEqual(got, expected)

    async def test_heartbeats_past_the_recording_repeat_the_last(self):
        rec = fakecloud.Recording.parse(relay_log())
        s = fakecloud.Session(rec)
        hows = [s.replies_for(frame(102, 0))[1] for _ in range(7)]
        self.assertEqual(hows, ["len102#0", "len102#1", "len102#2", "len102#3", "len102#4",
                                "len102#last", "len102#last"])
        self.assertEqual(s.replies_for(frame(999, 0))[1], "unknown999")

    async def test_unrecorded_handshake_is_closed_and_reported(self):
        app = await self.start("b", {}, relay_log())
        other = REQUEST[:8] + bytes(416)
        r, w = await asyncio.open_connection("127.0.0.1", app.port)
        w.write(other)
        await w.drain()
        self.assertEqual(await asyncio.wait_for(r.read(), 2), b"")
        w.close()
        mod = app.modules["127.0.0.1"]
        self.assertEqual(mod.status, "no recording")
        self.assertIn("run a capture", mod.last_issue)

    async def test_random_handshakes_are_flagged_unsupported(self):
        app = await self.start("c", {})
        for i in range(fakecloud.RANDOM_AFTER + 1):
            r, w = await asyncio.open_connection("127.0.0.1", app.port)
            w.write(REQUEST[:8] + bytes([i]) * 416)
            await w.drain()
            await asyncio.wait_for(r.read(), 2)
            w.close()
        self.assertTrue(app.modules["127.0.0.1"].status.startswith("unsupported"))

    async def test_capture_relays_and_records_then_replays(self):
        cloud = await self.start("cloud", {}, relay_log())  # stands in for Midea
        app = await self.start("cap", {"capture_minutes": 1, "upstream": f"127.0.0.1:{cloud.port}"})
        self.assertTrue(app.capture_open())
        got = await self.run_module(app.port)
        self.assertEqual(got[0], REPLY)
        for _ in range(50):  # the relay saves once both sides close
            if app.recordings:
                break
            await asyncio.sleep(0.05)
        rec = app.recordings[REQUEST]
        self.assertEqual(rec.reply, REPLY)
        self.assertEqual(rec.summary()["heartbeat_replies"], 3)
        self.assertTrue((Path(self.tmp.name) / "cap" / "recordings" / f"{rec.variant}.json").exists())
        # Once the window closes, the capture replays on its own.
        app.options["capture_minutes"] = 0
        got2 = await self.run_module(app.port)
        self.assertEqual(got2, got)

    async def test_panel_status_upload_download_delete(self):
        app = await self.start("d", {})
        code, body = await http(app.http_port, "GET", "/")
        self.assertEqual(code, 200)
        self.assertIn(b"Midea Local", body)
        code, body = await http(app.http_port, "POST", "/api/recordings", relay_log().encode())
        self.assertEqual(code, 200)
        variant = json.loads(body)["variant"]
        code, body = await http(app.http_port, "GET", "/api/status")
        status = json.loads(body)
        self.assertEqual(status["recordings"][0]["heartbeat_replies"], 3)
        code, body = await http(app.http_port, "GET", f"/api/recordings/{variant}")
        self.assertEqual(fakecloud.Recording.parse(body.decode()).reply, REPLY)  # round-trips
        code, _ = await http(app.http_port, "DELETE", f"/api/recordings/{variant}")
        self.assertEqual(code, 200)
        self.assertEqual(app.recordings, {})
        code, body = await http(app.http_port, "POST", "/api/recordings", b"garbage")
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
