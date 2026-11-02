# REST Mapper

Turn any JSON endpoint into Home Assistant entities through the interface, while spending the API's requests carefully. Works with no YAML and no templates.

- **Pick values from a list.** One test request is kept as a sample, so choosing paths uses no further requests.
- **Five entity types.** `sensor`, `binary_sensor`, `device_tracker`, `geo_location` and `calendar`, each with extra attributes where it makes sense.
- **Lists become entities.** Each item gets its own entity by a key you choose, added and removed as the list changes, with an event for new items.
- **A request budget.** Set a limit per hour, day or month, and polling is paced to last until the period ends. An on-demand mode makes no automatic requests.
- **Polite requests.** `ETag`, `Last-Modified`, `Retry-After` and backoff are all handled. Starting or reloading Home Assistant sends nothing.
- **Failures are normal.** Entities keep their last good value and show when it was last confirmed, across restarts too.

## Limits of this version

GET and JSON only. Authentication is none, Bearer, or a key in a header or query parameter. There is no POST, pagination, OAuth or XML, and no templates.

## Installation

Add this repository as a custom repository of type **Integration** in HACS, install REST Mapper and restart. To install by hand, copy `custom_components/rest_mapper` to your `config/custom_components/` folder and restart.

It needs Home Assistant 2026.10 or later. The library [rest-mapper](https://github.com/grozycki/rest-mapper) is included in the integration, so nothing else is installed.

## Setup

**Settings → Devices & services → Add integration → REST Mapper.**

1. Paste the address. A key in the address, such as `?key=abc`, is recognised and stored safely. If the service asks for a key, you are asked for it then.
2. The next screen lists what was found, in plain words, with an example of each. Tick what you want, choose how often to ask, and enter a monthly limit if your API has one.

That is all. Lists become map places, positions, calendars or sensors by what they hold. If a proposal is wrong, every path and limit can be changed under **Advanced options**.

The integration page has buttons to add more later: choose fields by name, and they get a name and unit by themselves. A list is one screen with a ready proposal. A mapping that reads nothing is not saved by accident. Changes send no request.

### What a budget does

A budget is a local count of the requests sent, including failures and `304` responses, in calendar periods in UTC. 10% is kept for manual requests by default. It cannot match the provider's own count exactly, and a provider that resets at another hour can differ by a few hours of spending, so leave a margin.

### Choosing what list items become

| Type | Fits | In the entity registry |
|---|---|---|
| Tracker | Objects that stay, such as a siren or a vehicle | Yes |
| Position (`geo_location`) | Short-lived objects, such as aircraft or incidents | No |
| Sensor, binary sensor | Values, one entity per item | Yes |
| Calendar | Dated lists, one entity for the whole list | Yes |

Trackers and positions appear on the Map dashboard.

### Icons on the map

Under **Advanced options**, choose the field that decides the icon, and list icons by value, for example `8=🚧, 6=🚗, *=⚠️`. `*` stands for any other value. Emoji and short text appear on the Map as they are. `mdi:car` sets the entity's icon, which map cards show with `label_mode: icon`.

## Examples

**Silesia Alert sirens.** URL `https://silesiaalert.pl/dane5.json`, no authentication. The list is found by itself, as 61 places on the map. Add `currentStatus` as an attribute under Advanced options if you want it. This endpoint sends `ETag`, so unchanged polls are cheap.

**Aircraft from adsb.lol.** URL `https://api.adsb.lol/v2/lat/50.26/lon/19.02/dist/100`, no authentication. The aircraft are proposed as short-lived positions. This endpoint sends no validators.

**TomTom Incident Details.** Use a key in the `key` query parameter, and a budget matching your plan. Paste the address with the key in it, and ask for `fields={incidents{type,geometry{type,coordinates},properties{id,iconCategory,startTime}}}`. Incidents are proposed as positions, and a line is shown at its middle point.

## Entities of each endpoint

- **Status:** `ok`, `retrying`, `rate_limited`, `auth_failed`, `budget_exhausted` or `on_demand`.
- **Last confirmed data**, and a **Data problem** binary sensor.
- **Refresh** button. `homeassistant.update_entity` works too, and many calls merge into one request.
- **Requests used** and **remaining**, with a budget. **Next update** is off by default.

Mapped entities carry `last_confirmed`, updated in the state at most every 15 minutes when nothing else changes. Mapped attributes are kept out of history, so map a value as its own sensor if you want its history.

## Compared with other options

| | RESTful (core) | Multiscrape | [HTTP Agent](https://github.com/DSorlov/http_agent) | REST Mapper |
|---|---|---|---|---|
| Set up in the interface | No | No | Yes | Yes |
| POST, XML, HTML | Yes | Yes | Yes | No |
| Templates | Yes | Yes | Yes | No |
| List items as entities | No | No | No | Yes |
| Request budget | No | No | No | Yes |
| `ETag`, `Retry-After` | No | No | No | Yes |
| Values kept through failures | No | No | No | Yes |

If you need POST, XML or templates, use one of the others.

## Removal

Delete the integration on its page, which also deletes its stored data, then remove the folder.

## Development

GitHub runs only the HACS and hassfest checks. Run tests, mypy and Ruff locally.

The library lives in the [rest-mapper](https://github.com/grozycki/rest-mapper) repository. After changing it, copy it in:

```bash
python scripts/vendor_library.py
```

`docker/compose.yaml` starts a test instance on port 8124.
