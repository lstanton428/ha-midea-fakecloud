# Midea Local for Home Assistant

A Home Assistant add-on that runs Midea dehumidifiers with no Midea cloud:

- **Local control** over your LAN, published to Home Assistant over MQTT.
  It uses [`midea-lan`](https://github.com/wuwentao/midea-lan), the library
  behind [`midea_ac_lan`](https://github.com/wuwentao/midea_ac_lan), and
  replaces that integration.
- **A fake cloud.** Block a Midea module from the internet and it reboots
  its Wi-Fi every few minutes looking for its cloud. This add-on answers in
  the cloud's place by replaying a cloud session recorded once from the real
  thing.

**Experimental.** It has been proven on one dehumidifier. The fake cloud
relies on the module's handshake repeating, and the add-on checks for that. See
[the docs](midea_fakecloud/DOCS.md) for how it works and how to set it up.

## Install

Settings → Add-ons → Add-on store → ⋮ → Repositories → add
`https://github.com/lstanton428/ha-midea-fakecloud`, then install
**Midea Local**.

## Development

`fakecloud.py` (fake cloud, MQTT, web panel; stdlib only) and
`lanbridge.py` (local control via midea-lan). Tests use a simulated module,
a fake device and a fake MQTT broker; the real-library test needs Python
3.12+ with `midea-lan` installed:

```bash
python3 -m unittest discover -s tests
```

Recordings are device-specific and stay on your HA host; never commit them.

## License

MIT
