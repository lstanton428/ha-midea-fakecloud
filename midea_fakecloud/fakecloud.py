#!/usr/bin/env python3
"""Midea fake cloud: keeps a Midea Wi-Fi module online without Midea's cloud.

A Midea module (e.g. in a dehumidifier) whose cloud is unreachable reboots its
Wi-Fi every few minutes. This server answers it on TCP 443 instead.

How replay works: the module opens with a 424-byte handshake request. On the
modules this was built for, that request is not random; it cycles through a
small fixed set. Replaying the real cloud's recorded handshake reply to a
recorded request makes the module derive the session key it had in the
recording, so the cloud's recorded encrypted frames are valid again. Each
module frame is answered with what the cloud sent after the same frame (by
length and occurrence) in the recording.

Recordings come from a capture: with capture_minutes > 0, sessions are relayed
to the real cloud for that many minutes and saved, then capture switches
itself off. Everything is stdlib-only.
"""

import asyncio
import base64
import hashlib
import json
import os
import socket
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OPTIONS_FILE = Path(os.environ.get("FAKECLOUD_OPTIONS", "/data/options.json"))
CONFIG_DIR = Path(os.environ.get("FAKECLOUD_CONFIG", "/config"))
SUPERVISOR = "http://supervisor"
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
# Ingress requests come from the Supervisor; tests add 127.0.0.1.
INGRESS_PEERS = set(os.environ.get("FAKECLOUD_INGRESS_PEERS", "172.30.32.2").split(","))
INGRESS_HOST = os.environ.get("FAKECLOUD_INGRESS_HOST", "172.30.32.1")
EVENTS_MAX = 5_000_000
MAX_UPLOAD = 2_000_000
RANDOM_AFTER = 4  # distinct handshakes from one module before replay is hopeless
DISCONNECT_GRACE = 30  # seconds without a session before "connected" turns off
HANDSHAKE_TIMEOUT = 30


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def supervisor(method: str, path: str, body=None):
    """Call the Supervisor API (blocking); None when not running as an add-on."""
    if not TOKEN:
        return None
    req = urllib.request.Request(
        SUPERVISOR + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp).get("data")


def split_frames(buf: bytearray) -> list:
    """Pop complete 8370 frames off the front of buf."""
    out = []
    while len(buf) >= 6 and buf[:2] == b"\x83\x70":
        n = int.from_bytes(buf[2:4], "big") + 8
        if len(buf) < n:
            break
        out.append(bytes(buf[:n]))
        del buf[:n]
    return out


def variant_of(request: bytes) -> str:
    """Short name for a handshake request: the first 4 bytes of its block."""
    return request[8:12].hex()


class Recording:
    """One recorded cloud session for one handshake request."""

    def __init__(self, request: bytes, reply: bytes, frames: list, captured: str = "", source: str = ""):
        self.request = request
        self.reply = reply
        self.frames = frames  # [("m"|"c", bytes)] after the handshake
        self.captured = captured or now_iso()
        self.source = source
        script = []  # (module frame length, [cloud replies])
        for direction, frame in frames:
            if direction == "m":
                script.append((len(frame), []))
            elif script:
                script[-1][1].append(frame)
        self.answers = {}  # module frame length -> reply lists, by occurrence
        for n, replies in script:
            self.answers.setdefault(n, []).append(replies)

    @property
    def variant(self) -> str:
        return variant_of(self.request)

    def summary(self) -> dict:
        return {
            "variant": self.variant,
            "captured": self.captured,
            "source": self.source,
            "module_frames": sum(1 for d, _ in self.frames if d == "m"),
            "heartbeat_replies": sum(1 for r in self.answers.get(102, []) if r and len(r[0]) == 102),
        }

    def to_json(self) -> dict:
        b64 = lambda b: base64.b64encode(b).decode()
        return {"format": 1, "variant": self.variant, "captured": self.captured, "source": self.source,
                "request": b64(self.request), "handshake_reply": b64(self.reply),
                "frames": [[d, b64(f)] for d, f in self.frames]}

    @classmethod
    def parse(cls, text: str, source: str = "upload") -> "Recording":
        """Accept this add-on's JSON format, or JSONL relay logs (one frame per
        line with dir "module->cloud"/"cloud->module" and a hex payload)."""
        text = text.strip()
        try:
            d = json.loads(text)
        except json.JSONDecodeError:
            d = None  # several lines: a JSONL relay log
        if isinstance(d, dict) and "format" in d:
            dec = base64.b64decode
            return cls(dec(d["request"]), dec(d["handshake_reply"]),
                       [(x[0], dec(x[1])) for x in d["frames"]], d.get("captured", ""), d.get("source", source))
        lines = [json.loads(l) for l in text.splitlines() if l.strip()]
        rel = [l for l in lines if l.get("event", "relay") == "relay" and "hex" in l and "dir" in l]
        if len(rel) < 3:
            raise ValueError("not a recording: need a handshake request, its reply and some frames")
        frames = [("m" if l["dir"].startswith("module") else "c", bytes.fromhex(l["hex"])) for l in rel]
        if frames[0][0] != "m" or frames[1][0] != "c" or len(frames[0][1]) != 424:
            raise ValueError("recording must start with the module's 424-byte handshake and the cloud's reply")
        captured = datetime.fromtimestamp(rel[0]["t"], timezone.utc).isoformat(timespec="seconds") if "t" in rel[0] else ""
        return cls(frames[0][1], frames[1][1], frames[2:], captured, source)


class Session:
    """Replay state for one TCP connection."""

    def __init__(self, rec: Recording):
        self.rec = rec
        self.seen = {}

    def replies_for(self, frame: bytes):
        """The cloud's replies to this frame: the Nth frame of a given length
        gets the replies to the recording's Nth frame of that length, and the
        last recorded ones once the recording runs out."""
        n = len(frame)
        k = self.seen.get(n, 0)
        self.seen[n] = k + 1
        options = self.rec.answers.get(n)
        if not options:
            return [], f"unknown{n}"
        if k < len(options):
            return options[k], f"len{n}#{k}"
        return options[-1], f"len{n}#last"


class Module:
    """What we know about one module (by IP)."""

    def __init__(self, ip: str, saved: dict):
        self.ip = ip
        self.sessions = saved.get("sessions", 0)
        self.handshakes = saved.get("handshakes", {})  # sha256 prefix -> count
        self.last_issue = saved.get("last_issue", "")
        self.status = "waiting"
        self.active = 0
        self.connected = False
        self.session_started = saved.get("session_started")
        self.announced = False

    @property
    def slug(self) -> str:
        return self.ip.replace(".", "_").replace(":", "_")

    @property
    def random_handshake(self) -> bool:
        return len(self.handshakes) > RANDOM_AFTER

    def to_state(self) -> dict:
        return {"sessions": self.sessions, "handshakes": self.handshakes,
                "last_issue": self.last_issue, "session_started": self.session_started}

    def to_mqtt(self) -> dict:
        return {"connected": "ON" if self.connected else "OFF", "session_started": self.session_started,
                "sessions": self.sessions, "status": self.status, "last_issue": self.last_issue,
                "handshakes_seen": len(self.handshakes)}


class Mqtt:
    """Just enough MQTT 3.1.1 to publish retained QoS 0 messages."""

    def __init__(self, host, port, username, password, will_topic):
        self.host, self.port = host, port
        self.username, self.password = username or "", password or ""
        self.will_topic = will_topic
        self.writer = None

    @staticmethod
    def _str(s: str) -> bytes:
        b = s.encode()
        return len(b).to_bytes(2, "big") + b

    @staticmethod
    def _packet(first: int, body: bytes) -> bytes:
        n, enc = len(body), bytearray()
        while True:
            digit, n = n % 128, n // 128
            enc.append(digit | (0x80 if n else 0))
            if not n:
                break
        return bytes([first]) + bytes(enc) + body

    def _connect_packet(self) -> bytes:
        flags = 0x02 | 0x04 | 0x20  # clean session, will, will retain
        payload = self._str(f"midea-fakecloud-{os.getpid()}") + self._str(self.will_topic) + self._str("offline")
        if self.username:
            flags |= 0x80
            payload += self._str(self.username)
            if self.password:
                flags |= 0x40
                payload += self._str(self.password)
        return self._packet(0x10, self._str("MQTT") + bytes([4, flags]) + (60).to_bytes(2, "big") + payload)

    def publish(self, topic: str, payload, retain: bool = True) -> None:
        if not self.writer:
            return
        data = payload if isinstance(payload, bytes) else (
            payload if isinstance(payload, str) else json.dumps(payload)).encode()
        self.writer.write(self._packet(0x30 | (1 if retain else 0), self._str(topic) + data))

    async def run(self, on_connect) -> None:
        while True:
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), 10)
                writer.write(self._connect_packet())
                await writer.drain()
                ack = await asyncio.wait_for(reader.readexactly(4), 10)
                if ack[0] != 0x20 or ack[3] != 0:
                    raise ConnectionError(f"MQTT broker refused the connection (CONNACK {ack.hex()})")
                self.writer = writer
                log(f"MQTT connected to {self.host}:{self.port}")
                await on_connect()
                pinger = asyncio.create_task(self._ping())
                try:
                    while True:  # drain whatever the broker sends (PINGRESP)
                        await reader.readexactly(1)
                        n, shift = 0, 0
                        while True:
                            b = (await reader.readexactly(1))[0]
                            n |= (b & 0x7F) << shift
                            shift += 7
                            if not b & 0x80:
                                break
                        await reader.readexactly(n)
                finally:
                    pinger.cancel()
            except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError) as e:
                log(f"MQTT: {e!r}; retrying in 15 s")
            self.writer = None
            await asyncio.sleep(15)

    async def _ping(self) -> None:
        while True:
            await asyncio.sleep(30)
            if self.writer:
                self.writer.write(b"\xc0\x00")
                await self.writer.drain()


class App:
    def __init__(self, options: dict, config_dir: Path = CONFIG_DIR):
        self.options = dict(options)
        self.dir = config_dir
        self.rec_dir = config_dir / "recordings"
        self.rec_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = config_dir / "state.json"
        self.events_file = config_dir / "events.jsonl"
        self.recordings = {}  # request bytes -> Recording
        self.load_recordings()
        saved = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        self.modules = {ip: Module(ip, s) for ip, s in saved.get("modules", {}).items()}
        self.capture_started = None
        self.mqtt = None
        self.bind = None
        self._conn = 0
        host, _, port = self.options.get("upstream", "").rpartition(":")
        self.upstream = (host, int(port)) if host and port.isdigit() else None

    # --- persistence -------------------------------------------------

    def load_recordings(self) -> None:
        self.recordings = {}
        for p in sorted(self.rec_dir.glob("*.json")):
            try:
                rec = Recording.parse(p.read_text(), source=p.name)
                self.recordings[rec.request] = rec
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                log(f"skipping {p.name}: {e}")
        log(f"{len(self.recordings)} recording(s): {', '.join(r.variant for r in self.recordings.values()) or 'none'}")

    def save_recording(self, rec: Recording, only_if_better: bool = False) -> bool:
        old = self.recordings.get(rec.request)
        if only_if_better and old and old.summary()["module_frames"] >= rec.summary()["module_frames"]:
            return False
        (self.rec_dir / f"{rec.variant}.json").write_text(json.dumps(rec.to_json()))
        self.recordings[rec.request] = rec
        return True

    def save_state(self) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"modules": {ip: m.to_state() for ip, m in self.modules.items()}}))
        tmp.replace(self.state_file)

    def event(self, kind: str, **fields) -> None:
        try:
            if self.events_file.exists() and self.events_file.stat().st_size > EVENTS_MAX:
                self.events_file.replace(self.events_file.with_suffix(".jsonl.1"))
            with self.events_file.open("a") as fh:
                fh.write(json.dumps({"t": round(time.time(), 3), "event": kind, **fields}) + "\n")
        except OSError as e:
            log(f"event log write failed: {e}")

    # --- capture window ----------------------------------------------

    def capture_minutes(self) -> int:
        return int(self.options.get("capture_minutes", 0) or 0)

    def capture_deadline(self):
        if self.capture_started is None:
            return None
        return self.capture_started + self.capture_minutes() * 60

    def capture_open(self) -> bool:
        if self.capture_minutes() <= 0 or not self.upstream:
            return False
        deadline = self.capture_deadline()
        return deadline is None or time.time() < deadline

    async def end_capture(self) -> None:
        """Turn capture off for good once its window has passed."""
        deadline = self.capture_deadline()
        if deadline is None:
            return
        await asyncio.sleep(max(0, deadline - time.time()) + 1)
        self.options["capture_minutes"] = 0
        self.capture_started = None
        log("capture window over; replaying only")
        self.event("capture_end")
        try:
            await asyncio.to_thread(supervisor, "POST", "/addons/self/options", {"options": self.options})
        except OSError as e:
            log(f"could not reset capture_minutes in the add-on options: {e} (set it to 0 yourself)")

    # --- modules and MQTT --------------------------------------------

    def module(self, ip: str) -> Module:
        if ip not in self.modules:
            self.modules[ip] = Module(ip, {})
        return self.modules[ip]

    def issue(self, mod: Module, text: str) -> None:
        if mod.last_issue != text:
            log(f"{mod.ip}: {text}")
        mod.last_issue = text
        self.event("issue", ip=mod.ip, text=text)

    def publish(self, mod: Module) -> None:
        if not self.mqtt or not self.mqtt.writer:
            return
        if not mod.announced:
            self.announce(mod)
        self.mqtt.publish(f"midea_fakecloud/{mod.slug}/state", mod.to_mqtt())

    def announce(self, mod: Module) -> None:
        state = f"midea_fakecloud/{mod.slug}/state"
        device = {"identifiers": [f"midea_fakecloud_{mod.slug}"], "name": f"Midea fake cloud {mod.ip}",
                  "manufacturer": "midea-fakecloud", "model": "Replayed Midea cloud"}
        common = {"state_topic": state, "availability_topic": "midea_fakecloud/status", "device": device}
        entities = [
            ("binary_sensor", "connected", {"name": "Cloud session", "device_class": "connectivity",
                                            "value_template": "{{ value_json.connected }}"}),
            ("sensor", "session_started", {"name": "Session started", "device_class": "timestamp",
                                           "value_template": "{{ value_json.session_started }}"}),
            ("sensor", "sessions", {"name": "Sessions", "state_class": "total_increasing",
                                    "entity_category": "diagnostic", "value_template": "{{ value_json.sessions }}"}),
            ("sensor", "status", {"name": "Status", "value_template": "{{ value_json.status }}",
                                  "json_attributes_topic": state,
                                  "json_attributes_template":
                                      '{{ {"last_issue": value_json.last_issue, '
                                      '"handshakes_seen": value_json.handshakes_seen} | tojson }}'}),
        ]
        for component, key, cfg in entities:
            uid = f"midea_fakecloud_{mod.slug}_{key}"
            self.mqtt.publish(f"homeassistant/{component}/{uid}/config", {**common, **cfg, "unique_id": uid})
        mod.announced = True

    async def on_mqtt_connect(self) -> None:
        for mod in self.modules.values():
            mod.announced = False
        self.mqtt.publish("midea_fakecloud/status", "online")
        for mod in self.modules.values():
            self.publish(mod)

    def session_open(self, mod: Module, status: str) -> None:
        mod.active += 1
        mod.sessions += 1
        mod.connected = True
        mod.status = status
        mod.session_started = now_iso()
        self.save_state()
        self.publish(mod)

    def session_closed(self, mod: Module) -> None:
        mod.active -= 1

        def settle():
            if mod.active == 0:
                mod.connected = False
                mod.status = "waiting" if not mod.random_handshake else mod.status
                self.publish(mod)

        asyncio.get_running_loop().call_later(DISCONNECT_GRACE, settle)

    # --- module connections ------------------------------------------

    async def handle_module(self, reader, writer) -> None:
        self._conn += 1
        cid = self._conn
        ip = writer.get_extra_info("peername")[0]
        mod = self.module(ip)
        opened = time.time()
        buf = bytearray()
        try:
            while True:
                data = await asyncio.wait_for(reader.read(65536), HANDSHAKE_TIMEOUT)
                if not data:
                    return
                buf += data
                frames = split_frames(buf)
                if frames or (buf and buf[:2] != b"\x83\x70"):
                    break
            if not frames or len(frames[0]) != 424 or frames[0][5] & 0x0F != 0:
                self.event("not_a_handshake", cid=cid, ip=ip, hex=bytes(buf[:64]).hex())
                return
            request, rest = frames[0], frames[1:]
            leftover = b"".join(rest) + bytes(buf)
            digest = hashlib.sha256(request).hexdigest()[:16]
            mod.handshakes[digest] = mod.handshakes.get(digest, 0) + 1
            rec = self.recordings.get(request)
            self.event("handshake", cid=cid, ip=ip, variant=variant_of(request), recorded=bool(rec))
            if self.capture_open():
                await self.relay(cid, mod, request, leftover, reader, writer)
            elif rec:
                await self.replay(cid, mod, rec, leftover, reader, writer)
            else:
                if mod.random_handshake:
                    mod.status = "unsupported: handshake changes every time"
                    self.issue(mod, f"{len(mod.handshakes)} different handshakes seen; this module's "
                                    "handshake looks random, so replay cannot work for it")
                else:
                    mod.status = "no recording"
                    self.issue(mod, f"no recording for handshake {variant_of(request)}; run a capture")
                self.save_state()
                self.publish(mod)
        except (asyncio.TimeoutError, ConnectionError, OSError) as e:
            self.event("error", cid=cid, ip=ip, err=repr(e))
        finally:
            writer.close()
            self.event("close", cid=cid, ip=ip, secs=round(time.time() - opened, 1))

    async def replay(self, cid, mod, rec, leftover, reader, writer) -> None:
        self.session_open(mod, "replaying")
        try:
            session = Session(rec)
            writer.write(rec.reply)
            await writer.drain()
            buf = bytearray(leftover)
            while True:
                for frame in split_frames(buf):
                    out, how = session.replies_for(frame)
                    if how.startswith("unknown"):
                        self.issue(mod, f"no recorded answer for a {len(frame)}-byte message")
                    for r in out:
                        writer.write(r)
                    await writer.drain()
                    self.event("rx", cid=cid, len=len(frame), hex=frame.hex(), answer=how,
                               tx=[len(r) for r in out])
                if buf and buf[:2] != b"\x83\x70":
                    self.event("junk", cid=cid, hex=bytes(buf[:64]).hex())
                    buf.clear()
                data = await reader.read(65536)
                if not data:
                    break
                buf += data
        finally:
            self.session_closed(mod)

    async def relay(self, cid, mod, request, leftover, reader, writer) -> None:
        if self.capture_started is None:
            self.capture_started = time.time()
            log(f"capture started: relaying to {self.upstream[0]}:{self.upstream[1]} "
                f"for {self.capture_minutes()} min")
            self.event("capture_start", upstream=f"{self.upstream[0]}:{self.upstream[1]}")
            asyncio.create_task(self.end_capture())
        self.session_open(mod, "capturing")
        frames = []
        try:
            up_r, up_w = await asyncio.wait_for(asyncio.open_connection(*self.upstream), 10)
        except (OSError, asyncio.TimeoutError) as e:
            self.issue(mod, f"capture: cannot reach the real cloud at {self.upstream}: {e!r}")
            self.session_closed(mod)
            return

        async def pump(src, dst, direction, initial=b""):
            buf = bytearray(initial)
            frames.extend((direction, f) for f in split_frames(buf))
            while True:
                data = await src.read(65536)
                if not data:
                    break
                dst.write(data)
                await dst.drain()
                buf += data
                frames.extend((direction, f) for f in split_frames(buf))
            dst.close()

        try:
            up_w.write(request + leftover)
            await up_w.drain()
            remaining = max(1.0, self.capture_deadline() - time.time())
            await asyncio.wait_for(asyncio.gather(
                pump(reader, up_w, "m", leftover), pump(up_r, writer, "c")), remaining)
        except asyncio.TimeoutError:
            pass  # capture window over
        except (ConnectionError, OSError) as e:
            self.event("error", cid=cid, err=repr(e))
        finally:
            up_w.close()
            self.session_closed(mod)
        if frames and frames[0][0] == "c":
            rec = Recording(request, frames[0][1], frames[1:], source="capture")
            saved = self.save_recording(rec, only_if_better=True)
            s = rec.summary()
            log(f"captured {rec.variant}: {s['module_frames']} module frames, "
                f"{s['heartbeat_replies']} heartbeat replies ({'saved' if saved else 'kept the longer existing one'})")
            self.event("captured", saved=saved, **s)

    # --- ingress web panel -------------------------------------------

    def status(self) -> dict:
        deadline = self.capture_deadline()
        return {
            "bind": self.bind,
            "upstream": f"{self.upstream[0]}:{self.upstream[1]}" if self.upstream else None,
            "capture": {"minutes": self.capture_minutes(), "open": self.capture_open(),
                        "ends": datetime.fromtimestamp(deadline, timezone.utc).isoformat(timespec="seconds")
                        if deadline else None},
            "mqtt": bool(self.mqtt and self.mqtt.writer),
            "recordings": [r.summary() for r in self.recordings.values()],
            "modules": [{"ip": m.ip, **m.to_mqtt()} for m in self.modules.values()],
        }

    async def handle_http(self, reader, writer) -> None:
        try:
            if writer.get_extra_info("peername")[0] not in INGRESS_PEERS:
                return
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            lines = head.decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = {k.strip().lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)}
            length = int(headers.get("content-length", "0") or 0)
            if length > MAX_UPLOAD:
                return self._respond(writer, 413, {"error": "too large"})
            body = await asyncio.wait_for(reader.readexactly(length), 30) if length else b""
            path = path.split("?", 1)[0].rstrip("/") or "/"
            await self._route(writer, method, path, body)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError, ConnectionError) as e:
            self._respond(writer, 400, {"error": str(e)})
        finally:
            try:
                await writer.drain()
            except ConnectionError:
                pass
            writer.close()

    async def _route(self, writer, method, path, body) -> None:
        if method == "GET" and path == "/":
            return self._respond(writer, 200, PANEL_HTML, "text/html; charset=utf-8")
        if method == "GET" and path == "/api/status":
            return self._respond(writer, 200, self.status())
        if path.startswith("/api/recordings"):
            variant = path[len("/api/recordings"):].strip("/")
            rec = next((r for r in self.recordings.values() if r.variant == variant), None)
            if method == "GET" and rec:
                return self._respond(writer, 200, rec.to_json(), extra={
                    "Content-Disposition": f'attachment; filename="midea-recording-{variant}.json"'})
            if method == "DELETE" and rec:
                (self.rec_dir / f"{variant}.json").unlink(missing_ok=True)
                del self.recordings[rec.request]
                self.event("recording_deleted", variant=variant)
                return self._respond(writer, 200, {"deleted": variant})
            if method in ("POST", "PUT") and not variant:
                try:
                    rec = Recording.parse(body.decode(), source="upload")
                except (ValueError, KeyError, json.JSONDecodeError, UnicodeDecodeError) as e:
                    return self._respond(writer, 400, {"error": f"not a recording: {e}"})
                self.save_recording(rec)
                self.event("recording_uploaded", **rec.summary())
                log(f"recording {rec.variant} uploaded")
                return self._respond(writer, 200, rec.summary())
        return self._respond(writer, 404, {"error": "not found"})

    @staticmethod
    def _respond(writer, code, body, ctype="application/json", extra=None) -> None:
        data = body.encode() if isinstance(body, str) else json.dumps(body, indent=1).encode()
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 413: "Payload Too Large"}.get(code, "")
        head = [f"HTTP/1.1 {code} {reason}", f"Content-Type: {ctype}", f"Content-Length: {len(data)}",
                "Cache-Control: no-store", "Connection: close"]
        head += [f"{k}: {v}" for k, v in (extra or {}).items()]
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + data)

    # --- startup -----------------------------------------------------

    async def start(self, ingress_port=None):
        bind = self.options.get("bind_address", "auto")
        if bind == "auto":
            bind = await asyncio.to_thread(primary_ipv4)
        self.bind = f"{bind}:{self.options.get('port', 443)}"
        servers = [await asyncio.start_server(self.handle_module, bind, int(self.options.get("port", 443)),
                                              reuse_address=True)]
        log(f"listening for modules on {self.bind}")
        if ingress_port is None:
            info = await asyncio.to_thread(supervisor, "GET", "/addons/self/info")
            ingress_port = (info or {}).get("ingress_port")
        if ingress_port is not None:
            servers.append(await asyncio.start_server(self.handle_http, INGRESS_HOST, int(ingress_port)))
            log(f"web panel on {INGRESS_HOST}:{ingress_port} (ingress)")
        try:
            mq = await asyncio.to_thread(supervisor, "GET", "/services/mqtt")
        except OSError as e:
            mq = None
            log(f"MQTT service lookup failed: {e}")
        if mq:
            self.mqtt = Mqtt(mq["host"], mq["port"], mq.get("username"), mq.get("password"),
                             "midea_fakecloud/status")
            asyncio.create_task(self.mqtt.run(self.on_mqtt_connect))
        else:
            log("no MQTT service; sensors disabled")
        if self.capture_minutes() > 0:
            log(f"capture armed: the next {self.capture_minutes()} min of sessions go to the real cloud")
        return servers


def primary_ipv4() -> str:
    """The host's primary LAN IPv4, so we never bind Tailscale's addresses."""
    try:
        info = supervisor("GET", "/network/info") or {}
        for iface in info.get("interfaces", []):
            addrs = (iface.get("ipv4") or {}).get("address") or []
            if iface.get("primary") and addrs:
                return addrs[0].split("/")[0]
    except OSError as e:
        log(f"network info lookup failed: {e}")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))  # no packet is sent; picks the default-route address
        return s.getsockname()[0]


PANEL_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Midea fake cloud</title>
<style>
:root{--bg:#fafafa;--fg:#1c1c1c;--muted:#666;--line:#ddd;--ok:#1b7f3b;--warn:#b25e00}
@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#e6e6e6;--muted:#9a9a9a;--line:#333;--ok:#5fd17f;--warn:#f0a040}}
body{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:16px;max-width:900px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:22px 0 6px}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:500}.muted{color:var(--muted)}.ok{color:var(--ok)}.warn{color:var(--warn)}
button{font:inherit;padding:3px 10px;cursor:pointer}a{color:inherit}
</style></head><body>
<h1>Midea fake cloud</h1><div class="muted" id="meta">Loading…</div>
<h2>Modules</h2><table id="mods"></table>
<h2>Recordings</h2><table id="recs"></table>
<p><label>Upload a recording (this add-on's .json or a JSONL relay log): <input type="file" id="up"></label></p>
<p class="muted" id="msg"></p>
<script>
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
async function load(){
  const s=await (await fetch("api/status")).json();
  const cap=s.capture.open?`<span class="warn">capture ON (${s.capture.minutes} min${s.capture.ends?", ends "+esc(s.capture.ends):""})</span>`:"capture off";
  document.getElementById("meta").innerHTML=`Listening on ${esc(s.bind)} · ${cap} · MQTT ${s.mqtt?"connected":"off"}`;
  document.getElementById("mods").innerHTML="<tr><th>Module</th><th>Status</th><th>Session</th><th>Sessions</th><th>Handshakes seen</th><th>Last issue</th></tr>"+
    (s.modules.map(m=>`<tr><td>${esc(m.ip)}</td><td class="${m.status==="replaying"||m.status==="waiting"?"ok":"warn"}">${esc(m.status)}</td><td>${m.connected==="ON"?"connected since "+esc(m.session_started):"none"}</td><td>${m.sessions}</td><td>${m.handshakes_seen}</td><td class="muted">${esc(m.last_issue)}</td></tr>`).join("")||'<tr><td colspan="6" class="muted">No module has connected yet.</td></tr>');
  document.getElementById("recs").innerHTML="<tr><th>Handshake</th><th>Captured</th><th>Module frames</th><th>Heartbeat replies</th><th></th></tr>"+
    (s.recordings.map(r=>`<tr><td>${esc(r.variant)}</td><td>${esc(r.captured)}</td><td>${r.module_frames}</td><td>${r.heartbeat_replies}</td><td><a href="api/recordings/${esc(r.variant)}">download</a> <button data-v="${esc(r.variant)}">delete</button></td></tr>`).join("")||'<tr><td colspan="5" class="muted">No recordings. Set capture_minutes in the add-on options to make one.</td></tr>');
}
document.getElementById("recs").addEventListener("click",async e=>{
  const v=e.target.dataset.v; if(!v||!confirm("Delete recording "+v+"?"))return;
  await fetch("api/recordings/"+v,{method:"DELETE"}); load();
});
document.getElementById("up").addEventListener("change",async e=>{
  const f=e.target.files[0]; if(!f)return;
  const r=await fetch("api/recordings",{method:"POST",body:await f.text()});
  const j=await r.json(); document.getElementById("msg").textContent=r.ok?`Saved recording ${j.variant}.`:`Upload failed: ${j.error}`;
  e.target.value=""; load();
});
load(); setInterval(load,10000);
</script></body></html>
"""


async def main() -> None:
    options = json.loads(OPTIONS_FILE.read_text())
    app = App(options)
    await app.start()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
