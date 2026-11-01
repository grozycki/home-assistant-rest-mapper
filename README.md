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

The repositories are private for now, so nothing is in the Marketplace or on PyPI. Copy `custom_components/rest_mapper` to your `config/custom_components/` folder and restart. It needs Home Assistant 2026.10 or later. The library is included in the integration.

## Setup

**Settings → Devices & services → Add integration → REST Mapper.**

1. Enter the URL and authentication. One test request is sent.
2. Choose how often to poll. The next step shows what this uses per day and per month.
3. Optionally tick the first fields to get sensors right away.

The integration page then has buttons to add a sensor, binary sensor, tracker or collection. Each mapping can be changed or removed on its own, and that sends no request.

### What a budget does

A budget is a local count of the requests sent, including failures and `304` responses, in calendar periods in UTC. 10% is kept for manual requests by default. It cannot match the provider's own count exactly, and a provider that resets at another hour can differ by a few hours of spending, so leave a margin.

### Choosing what list items become

| Type | Fits | Shown on the default Map | In the entity registry |
|---|---|---|---|
| Tracker | Objects that stay, such as a siren or a vehicle | Yes | Yes |
| Position (`geo_location`) | Short-lived objects, such as aircraft or incidents | No, use a map card | No |
| Sensor, binary sensor | Values, one entity per item | No | Yes |
| Calendar | Dated lists, one entity for the whole list | No | Yes |

The default Map does not show `geo_location` entities. Add a map card with the source shown at the end of the flow:

```yaml
type: map
geo_location_sources:
  - rest_mapper_planes
```

## Examples

**Silesia Alert sirens.** URL `https://silesiaalert.pl/dane5.json`, no authentication. Add a collection with list `$`, key `id`, type tracker, name `name`, latitude `lat`, longitude `lon`, and `currentStatus` as an attribute. This endpoint sends `ETag`, so unchanged polls are cheap.

**Aircraft from adsb.lol.** URL `https://api.adsb.lol/v2/lat/50.26/lon/19.02/dist/100`, no authentication. Add a collection with list `$.ac`, key `hex`, type position, name `flight`, latitude `lat`, longitude `lon`. Set removal after 1 response. This endpoint sends no validators.

**TomTom Incident Details.** Use a key in the `key` query parameter, and a budget matching your plan. Add a collection with list `$.incidents`, key `id`, type position, and `geometry` as the GeoJSON geometry. A line is shown at its middle point. This example was checked against a sample of the documented shape and not the live API.

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

The library lives in the `rest-mapper` repository. After changing it, copy it in:

```bash
python scripts/vendor_library.py
```

`docker/compose.yaml` starts a test instance on port 8124.
