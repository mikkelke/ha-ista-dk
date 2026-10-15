# ista Denmark

[![CI](https://github.com/mikkelke/ha-ista-dk/actions/workflows/ci.yml/badge.svg)](https://github.com/mikkelke/ha-ista-dk/actions/workflows/ci.yml)
[![GitHub release](https://img.shields.io/github/v/release/mikkelke/ha-ista-dk)](https://github.com/mikkelke/ha-ista-dk/releases)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Home Assistant custom integration for ista Denmark
([istaonline.dk](https://www.istaonline.dk)) accounts. It shows the current
readings of your cold water, hot water and heat meters, imports the daily
consumption history as long term statistics, and prices it with the unit prices
on your own settlement statements.

This is an unofficial integration. It is not affiliated with or endorsed by ista.

## Features

- UI-based setup with your consumer number (forbrugernummer) and password
- One cumulative sensor per meter: cold water (m³), hot water (m³) and heat (kWh)
- The full daily consumption history that ista provides, imported as long-term
  statistics and extended on every poll
- Cost statistics per meter, priced with the unit prices printed on your ista
  settlement statements (PDF)
- Unit price sensors, and optional estimated prices for the period that has not
  been settled yet
- Re-authentication flow when the password changes
- Diagnostics with the login redacted
- English and Danish translations

## Installation

### HACS (recommended)

[![Open your Home Assistant instance and open this repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=mikkelke&repository=ha-ista-dk&category=integration)

1. Click the button above — or, in HACS, open the three-dot menu → *Custom repositories* and add
   `https://github.com/mikkelke/ha-ista-dk` with category **Integration**.
2. Download **ista Denmark**.
3. Restart Home Assistant.

Updates show up in HACS like any other integration.

### Manual

1. Download the [latest release](https://github.com/mikkelke/ha-ista-dk/releases/latest)
   and copy `custom_components/ista_dk` into your Home Assistant
   `config/custom_components/` folder.
2. Restart Home Assistant.

Home Assistant 2026.5 or newer is required. The PDF reader `pypdf` is installed
automatically.

## Configuration

Configuration is done entirely through the Home Assistant UI.

[![Open your Home Assistant instance and start setting up this integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=ista_dk)

1. Go to **Settings → Devices & Services**.
2. Click **Add Integration** and search for **ista Denmark**.
3. Enter the **consumer number** and **password** you use to log in on istaonline.dk.
4. Click **Submit**.

The consumer number identifies the account, so the same account cannot be added
twice. If ista later rejects the stored password, Home Assistant asks you to log
in again.

## Entities

One device per account, with one sensor for each meter the account has:

| Entity | Unit | Description |
| --- | --- | --- |
| `ista Cold water` | m³ | Cumulative register of the cold water meter |
| `ista Hot water` | m³ | Cumulative register of the hot water meter |
| `ista Heat` | kWh | Cumulative register of the heat meter |
| `ista Cold water price` | currency/m³ | Unit price of cold water |
| `ista Hot water price` | currency/m³ | Unit price of hot water |
| `ista Heat price` | currency/kWh | Unit price of heat |

The meter sensors use the `total_increasing` state class. Each has a
`reading_date` attribute with the date of the latest reading as an ISO date.

The price sensors appear once a unit price is known, see [Costs](#costs). They
have the attributes `source` (`settled` or `estimate`), `period_start` and
`period_end`.

## Statistics

The daily consumption is imported as external long-term statistics, one per meter:

| Statistic id | Unit |
| --- | --- |
| `ista_dk:<consumer number>_cold_water` | m³ |
| `ista_dk:<consumer number>_hot_water` | m³ |
| `ista_dk:<consumer number>_heat` | kWh |

Each day is one row that starts at local midnight in Home Assistant's time zone.
The row's `state` is the consumption of that day and its `sum` is the running
total. Days without a value are skipped; a day with zero consumption is kept.
ista only provides daily values, so in hourly views the whole day appears in
the midnight hour.

Pick them in the **statistics graph card**, or in the **Energy dashboard**: the
two water statistics as water sources and the heat statistic as an individual
device.

On every poll only the days after the last stored statistic are added, continuing
its running total, so existing history is never rewritten. Removing the
integration deletes the statistics it imported; adding it again imports the full
history from ista once more.

## Costs

ista sends a settlement statement for water (*Brugsvandsregnskab*) and one for
heat (*Varmeregnskab*) for each accounting period. The integration lists them
on every poll, downloads each PDF once, reads the unit prices from its first page
and keeps them in Home Assistant's storage.

| Meter | Unit price |
| --- | --- |
| Cold water | *Brugsvand kubikmeter* |
| Hot water | *Brugsvand kubikmeter* plus *Varmt vand kubikmeter* |
| Heat | *Varme KWh* |

The prices are taken as printed. Fixed charges on the statements (per square
meter, tap shares, energy management and so on) are not included, so the cost is
the consumption times the unit price and nothing else.

A day is priced with the statement period that contains it. Days after the
newest settled period use the newest price until the statement for their own
period arrives, and days before the oldest period use the oldest price.

### Cost statistics

The cost is imported as external long-term statistics, one per meter, in
Home Assistant's currency:

| Statistic id |
| --- |
| `ista_dk:<consumer number>_cold_water_cost` |
| `ista_dk:<consumer number>_hot_water_cost` |
| `ista_dk:<consumer number>_heat_cost` |

They use the same daily rows as the consumption statistics, with the cost of the
day as `state` and the running total as `sum`. Select one as the cost statistic
of a water source in the Energy dashboard, or in the statistics graph card. The
prices on the statements are in Danish kroner, so set the currency under
**Settings → System → General** to DKK.

The whole series is recalculated on every poll, so a statement that settles the
current period re-prices its days. Only rows that differ from the stored ones are
written. Nothing is imported until a unit price is known, and removing the
integration deletes the cost statistics and the cached prices.

### Estimated prices

The statements arrive months after their period has ended. Until then the
newest settled price is used. To use a better guess instead, open the
integration's **Configure** dialog and enter an estimated price for any of the
three lines: *Brugsvand kubikmeter* and *Varmt vand kubikmeter* in
currency/m³, and *Varme KWh* in currency/kWh. Leave a field empty to keep using
the settled price for that line. Saving reloads the integration.

An estimate applies to the days after the end of the newest settled statement at
the time you saved it, and replaces the settled price for those days. It is
ignored as soon as a settled statement ending after that date arrives; the
integration then logs a warning and goes back to the settled prices, and you can
save new estimates for the next period. Before the first statement has arrived
the estimates apply to every day. The `source` attribute of the price sensors
shows whether the estimate or a settled price is in use.

## Notes on the data

- Home Assistant polls ista every 6 hours.
- ista publishes the daily consumption with a delay of roughly a week, so the
  newest statistic is typically about a week old, while the sensors show the
  latest meter register.
- The unit prices are read from the text on the first page of the statements. If
  ista changes that layout, new statements are skipped with a warning in the log
  and the costs keep using the prices that were read before.
- The integration uses the same web endpoints as ista's own apps. ista may change
  them without notice.
- Your login is stored in Home Assistant's config entry storage and only sent to
  ista.

## Reporting issues

Found a bug? [Open an issue](https://github.com/mikkelke/ha-ista-dk/issues)
and attach the integration's diagnostics (**Settings → Devices & Services → ista
Denmark → ⋮ → Download diagnostics**) together with the relevant lines from your
Home Assistant log. The diagnostics do not contain your login.
