# Midea fake cloud

A Midea appliance's Wi-Fi module (dehumidifiers, air conditioners) keeps a
connection open to Midea's cloud. Block its internet access, as you would for
a device you control locally, and the module reboots its Wi-Fi every few
minutes looking for the cloud. That is hundreds of reconnects a day.

This add-on stands in for the cloud. The module connects to it, gets the
answers it expects, and stays put. Your local integration (for example
`midea_ac_lan`) keeps working as before; it talks to the appliance directly
and never goes through this add-on.

**Status: experimental.** It has been proven on one dehumidifier module.
Whether your module works depends on a firmware quirk, and the add-on checks
for it (see "Compatibility").

## How it works

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

## Setup

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

## Compatibility

The add-on counts the distinct handshakes each module sends. If a module sends
more than four different ones, its handshake is genuinely random, replay
cannot work, and the status says "unsupported". You can check this before
capturing anything: just do steps 1–3 and watch for a few minutes.

## What to expect

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

## Sensors

With the Mosquitto add-on (or another MQTT broker set up in HA), each module
gets a device with these entities:

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
