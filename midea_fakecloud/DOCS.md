# Midea Local

This add-on does two things for Midea dehumidifiers, with no Midea cloud
involved:

1. **Local control.** It talks to the appliance over your LAN and gives Home
   Assistant a full device: the dehumidifier (on/off, mode, target humidity),
   fan speed, beep on/off, humidity, temperature, tank level, tank-full and
   filter alerts. This replaces the `midea_ac_lan` integration.
2. **Fake cloud.** It stands in for Midea's cloud so the appliance's Wi-Fi
   stays connected (below).

## Local control

Needs MQTT (the Mosquitto add-on). Add each appliance under **devices**:

```yaml
devices:
  - name: Dehumidifier
    ip: 192.168.30.121
    device_id: 151732607699241
    token: <128 hex characters>
    key: <64 hex characters>
    model: 00000Q1A      # optional
    subtype: 41377       # optional
    area: Bedroom        # optional, suggested area for the new device
```

`device_id`, `token` and `key` are the same values `midea_ac_lan` stores. If
you are moving from that integration, copy them from its config entry, or
fetch them once with the `midea-lan` command-line tool. `protocol`
(default 3), `port` (default 6444), `refresh_interval` (seconds, default
30) and `type` (only 161, dehumidifier, is supported) are optional.

Entity IDs follow `midea_ac_lan` (`humidifier.<device_id>_humidifier`,
`select.<device_id>_fan_speed`, ...), so dashboards and automations keep
working if you remove the integration's entities first. Set
`entity_id_prefix` to use something other than the device ID, for example
to try the add-on alongside the integration.

## Fake cloud

A Midea appliance's Wi-Fi module keeps a
connection open to Midea's cloud. Block its internet access, as you would for
a device you control locally, and the module reboots its Wi-Fi every few
minutes looking for the cloud. That is hundreds of reconnects a day.

The add-on stands in for the cloud. The module connects to it, gets the
answers it expects, and stays put. Local control (above) talks to the
appliance directly and doesn't depend on the fake cloud, and the fake cloud
works without local control too.

**Status: experimental.** It has been proven on one dehumidifier module.
Whether your module works depends on a firmware quirk, and the add-on checks
for it (see "Compatibility").

### How it works

The module opens each cloud session with a 424-byte handshake. That handshake
should be random, but on the modules this was built for it cycles through a
small fixed set of values. Because of that, the cloud's reply to a given
handshake can be recorded once and played back later: the module ends up with
the same session key it had during the recording, so the cloud's recorded
messages are valid to it again. The add-on answers each message from the
module with what the cloud sent at the same point in the recording.

The add-on cannot read or create messages; it only replays them. Replayed
messages are replies (heartbeat and acknowledgement). In testing, the real cloud
never sent anything unprompted, and replaying never changed the appliance's
settings.

### Setup

1. **Make the module find this add-on.** In your DNS server (AdGuard Home,
   Pi-hole or your router), rewrite `module.appsmb.com` to your Home Assistant
   host's LAN IP. Make sure the module uses that DNS server.
2. **Firewall.** Let the module reach your HA host on TCP 443. Keep it blocked
   from the internet otherwise.
3. **Start the add-on** and open its panel. Within a minute or two the module
   should appear with status "no recording". That means it is connecting.
4. **Capture once.** Set **Capture minutes** to `10`, save, and restart the
   add-on. Your firewall must let the **HA host** (not the module) reach the
   internet on TCP 443. For those 10 minutes the add-on relays the module's
   sessions to Midea's real cloud and records them; then capture switches
   itself off. Do it twice if the panel shows only one handshake: modules
   usually use two.

   During a capture the appliance is connected to Midea's cloud, so the cloud
   could in principle push a command or a firmware update. Keep the window
   short.

5. That's it. The panel should show the module as "replaying", and its
   Wi-Fi should stop reconnecting.

### Compatibility

The add-on counts the distinct handshakes each module sends. If a module sends
more than four different ones, its handshake is genuinely random, replay
cannot work, and the status says "unsupported". You can check this before
capturing anything: just do steps 1–3 and watch for a few minutes.

### What to expect

- **Wi-Fi stays connected.** This is the point.
- **The TCP session to the add-on renews every few minutes.** The module
  rejects a reply it has already seen, so a session ends once the recording's
  heartbeat replies run out (longer captures help, up to a point). The
  module's periodic status reports (every 15 and 30 minutes) also end the
  session, because their acknowledgements cannot be replayed. Either way the
  module reconnects to the add-on within a second; Wi-Fi is not affected.
- **Firmware updates** can only arrive from the real cloud, so they won't
  arrive. If you ever let one in, its handshake may change and you would need
  a new capture.

## Options

| Option | Meaning |
|---|---|
| `bind_address` | LAN address to listen on. `auto` uses the primary interface. |
| `port` | Always 443 for real modules. |
| `capture_minutes` | 0 for replay only. 1–30 relays that many minutes to the real cloud, then resets to 0. |
| `upstream` | The real cloud as `ip:port`, used only during capture. |
| `devices` | Appliances to control locally (see "Local control"). |

## Fake-cloud sensors

With MQTT, each module also gets these entities (on the appliance's device
when local control is set up for it):

- **Cloud session**: connectivity; stays on across the brief renewals.
- **Session started**: when the current session began.
- **Sessions**: total sessions served.
- **Status**: `replaying`, `waiting`, `capturing`, `no recording` or
  `unsupported`. Attributes include the last issue seen.

## Files

Everything lives in this add-on's config folder (`/addon_configs/<slug>/` over
Samba): `recordings/*.json`, `state.json` and `events.jsonl` (a detailed
log, rotated at 5 MB). Recordings are specific to your module and are
included in HA backups. Download them from the panel to keep a copy.
