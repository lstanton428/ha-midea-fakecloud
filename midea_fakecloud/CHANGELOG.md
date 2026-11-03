# Changelog

## 0.2.2

- Stops cleanly when Home Assistant stops the add-on (it was being marked
  "error"), closing appliance connections first.

## 0.2.1

- Temperature shows one decimal place, as in midea_ac_lan.

## 0.2.0

- Local control: the add-on now talks to Midea dehumidifiers over the LAN
  (via midea-lan, the library behind the midea_ac_lan integration) and
  publishes them to Home Assistant over MQTT, so one add-on replaces the
  integration. Entity names, units and IDs match midea_ac_lan's.
- A module's fake-cloud sensors join its appliance's device when local
  control is configured for it.
- Renamed to "Midea Local" (same slug, so it updates in place).

## 0.1.0

- First release: replay of recorded Midea cloud sessions, capture mode with
  automatic switch-off, compatibility self-check, MQTT sensors, and a web
  panel for recording upload, download and deletion.
