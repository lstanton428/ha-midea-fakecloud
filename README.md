# Midea fake cloud for Home Assistant

A Home Assistant add-on that keeps a Midea appliance's Wi-Fi module online
without Midea's cloud.

Block a Midea module from the internet and it reboots its Wi-Fi every few
minutes looking for its cloud. This add-on answers in the cloud's place by
replaying a cloud session recorded once from the real thing. Local control
through an integration like
[`midea_ac_lan`](https://github.com/wuwentao/midea_ac_lan) is unaffected.

**Experimental.** It has been proven on one dehumidifier module. It relies
on the module's handshake repeating, and the add-on checks for that. See
[the docs](midea_fakecloud/DOCS.md) for how it works and how to set it up.

## Install

Settings → Add-ons → Add-on store → ⋮ → Repositories → add
`https://github.com/lstanton428/ha-midea-fakecloud`, then install
**Midea fake cloud**.

## Development

The add-on is a single stdlib-only Python file. Tests run a simulated
module against it:

```bash
python3 -m unittest discover -s tests
```

Recordings are device-specific and stay on your HA host; never commit them.

## License

MIT
