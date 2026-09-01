# NZ Post Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-nz-post.svg)](https://github.com/ha-parcel-integrations/ha-nz-post/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration that tracks [NZ Post](https://www.nzpost.co.nz/tools/tracking) parcels. No account is needed: add each tracking code you want to follow.

Part of the [ha-parcel-integrations](https://github.com/ha-parcel-integrations) family: it publishes the same canonical parcel format, statuses and events as the other carrier integrations, so it plugs straight into the [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) and cross-carrier automations.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Services](#services)
- [Examples](#examples)
- [Debugging](#debugging)
- [Troubleshooting](#troubleshooting)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- Track any number of NZ Post parcels by tracking code — no account needed
- Per-parcel sensor with the canonical status, NZ Post's latest status text and a link to the public tracker
- Summary sensors: incoming parcels, next delivery, recently delivered parcels
- Read-only **Deliveries** calendar; NZ Post currently does not expose delivery dates, so it remains empty until that data is confirmed
- `nz_post.track_parcel` / `nz_post.untrack_parcel` services, so a dashboard button can add a parcel
- Events + device triggers for no-code automations (parcel registered, status changed, delivered, delivery time changed)
- Opt-in per-parcel status history
- Manual refresh button and a diagnostic last-update sensor

## Requirements

- Home Assistant 2024.12 or newer
- A NZ Post parcel and its tracking code (from the shipping
  confirmation email or the missed-delivery card) — no account needed

## Installation

### HACS (recommended)

1. In HACS, choose the three-dot menu → **Custom repositories**.
2. Add `https://github.com/ha-parcel-integrations/ha-nz-post` as an **Integration**.
3. Install **NZ Post** and restart Home Assistant.

### Manual

Copy `custom_components/nz_post` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

Add the integration via **Settings → Devices & Services → Add Integration → NZ Post**. There is nothing to fill in: the hub is created immediately (NZ Post tracking needs no account).

Then add parcels via the integration's **Configure** dialog, the [`nz_post.track_parcel`](#services) service, or a [dashboard button](examples/dashboards/add_parcel_card.yaml). The tracking code is on your shipping confirmation email or the missed-delivery card.

## Options

Open **Configure** on the integration entry:

| Section | Option | Default | Description |
|---|---|---|---|
| Parcels | Add / remove | — | Manage the tracked tracking codes. Changes apply immediately, no restart. |
| Delivered parcels | Filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensor. |
| Parcel history | Include status history | off | Adds a `history` attribute per parcel with each status update. |

Polling isn't one of these settings: the integration polls on a dynamic,
status-driven schedule (quiet overnight window, faster when a parcel is out
for delivery, stopped entirely once nothing is left to track) with nothing to
configure. See [CLAUDE.md](CLAUDE.md) for the details.

## Removal

Standard HA removal applies: **Settings → Devices & Services → NZ Post → ⋮ → Delete**. Nothing is stored on NZ Post's side.

## Sensors

| Entity | Description |
|---|---|
| `sensor.nz_post_incoming_parcels` | Number of active tracked parcels, full list under the `parcels` attribute |
| `sensor.nz_post_parcel_<code>` | One per tracked parcel; state is the canonical status, attributes carry the full normalised parcel |
| `sensor.nz_post_next_delivery` | Earliest confirmed delivery moment; NZ Post currently does not provide one |
| `sensor.nz_post_delivered_parcels` | Recently delivered parcels (see the retention option) |
| `sensor.nz_post_last_successful_update` | Diagnostic: when NZ Post was last polled successfully |

A delivered parcel moves from its per-parcel sensor to the delivered sensor automatically.

## Parcel status reference

The `status` field is the carrier-agnostic enum shared by the whole integration family:

| Status | Meaning |
|---|---|
| `registered` | Announced / received by NZ Post |
| `in_transit` | In the sorting network |
| `out_for_delivery` | With the courier today |
| `delivered` | Delivered |
| `unknown` | Not yet scanned, or a status we have not mapped yet |

The carrier's own human-readable text is always available as `raw_status`.

## Events

The integration fires these on the event bus (also available as device triggers on the NZ Post device):

| Event | When |
|---|---|
| `nz_post_parcel_registered` | A new parcel appears in the active list |
| `nz_post_parcel_status_changed` | A parcel's canonical status changes (`old_status` / `new_status` in the payload), except the final hop to delivered |
| `nz_post_parcel_delivered` | A parcel is delivered |

Every payload is the full normalised parcel plus the hub's `device_id`. Events are suppressed on the first refresh after start-up. NZ Post does not expose a confirmed delivery window, so no delivery-time event is emitted.

## Services

| Service | Fields | Description |
|---|---|---|
| `nz_post.track_parcel` | `tracking_code` | Start tracking a parcel |
| `nz_post.untrack_parcel` | `tracking_code` | Stop tracking a parcel |

## Examples

Ready-to-paste automations and dashboard snippets live in [`examples/`](examples/), including tracking a new parcel straight from a dashboard.

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/hki-parcels-card](https://github.com/jonisnet/hki-parcels-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

```yaml
logger:
  logs:
    custom_components.nz_post: debug
```

## Troubleshooting

- **A parcel shows `unknown`** — NZ Post has not scanned it yet (their API answers `not_found` until the first scan), or the code is wrong. It will pick up automatically once scanned.
- **A status logs "Unrecognised NZ Post status"** — please [open an issue](https://github.com/ha-parcel-integrations/ha-nz-post/issues/new) with the logged line so the mapping can be extended.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://github.com/ha-parcel-integrations) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://github.com/ha-parcel-integrations) for the current list of supported carriers.

## Disclaimer

This integration uses the same public tracking endpoint as the NZ Post consumer website. It is not affiliated with, endorsed by, or supported by NZ Post.

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

[MIT](LICENSE)
