# Denver Group Rides

An interactive map of **cycling group rides across metro Denver, Boulder, Fort Collins and Colorado's mountain
towns**: all clubs, one map. It is the Front Range counterpart of [boulderrides.cc](https://boulderrides.cc),
built from that project's MIT-licensed frontend ([RBergua/boulderrides](https://github.com/RBergua/boulderrides))
plus a new, open backend.

As of November 2026 no equivalent existed for Denver: the closest things are hand-curated lists
(Bike Community, Holy Cow's "Denver Stampedes", Meetup, Bicycle Colorado's calendar), none of which
pull club rides onto a live map.

## What it does

- **7-day calendar** with a ride count per day; the first day with rides loads automatically
- **Routes on a map**, color-coded per ride, solid for paved and dashed for unpaved, with start pins and direction arrows
- **Area chips**: All, Denver, Boulder, Fort Collins, Mountains, each with the day's ride count
- **Day list**: every ride of the selected day, sorted by start time
- **Ride card**: club, time, distance, elevation gain, elevation profile, ride type, unpaved share, stated pace,
  meeting point, description, and a link to the organizer's event page
- **Share**: a link that opens the map on one specific ride (`#ride=<id>`) or day (`#day=YYYY-MM-DD`)
- **Directions** to the meeting point and **Add to calendar** (.ics)
- Rides with no posted route still appear, as a start pin marked "No route posted"
- **Races & Events** page and checkered pins, from BikeReg
- **Weather**: downtown Denver forecast per day, plus forecast along the route when the pace is known
- **GPX/List** page with one-tap GPX downloads
- **Metric/Imperial** toggle, installable as a phone home-screen app
- No analytics, no cookies, no tracking

## Where the rides come from

| Source | What | Credentials |
|---|---|---|
| Ride with GPS | Events of the organizations in `backend/config.json`, with route, elevation and surface | None |
| Meetup | Public group calendars (iCal) plus each event page for the meeting point; a Ride with GPS route link in the description is followed | None |
| Event calendars | WordPress "The Events Calendar" sites (Bicycle Colorado's "Group Rides" category) and plain iCal feeds | None |
| Boulder feed | Every ride published by boulderrides.cc (its public `data` branch) | None |
| Strava | Group events of the clubs in `backend/config.json` | Strava API app (see below) |
| BikeReg | Races and events in Colorado | None |
| National Weather Service | Hourly forecasts | None |
| OpenStreetMap | Paved/unpaved tagging (Overpass) and address lookup (Nominatim) | None |

**Most Denver clubs post their rides only on Strava**, and Strava shows club events only to logged-in members,
so those clubs need Strava API credentials (below). Until then they show as gray badges and contribute no rides.

### Accuracy checks applied to every ride

- The **area** comes from where the ride starts, not where the club is based
- **Distance** is re-measured from the route geometry; if the source's figure is missing or more than 10 % off, the measured value is used and the correction is logged
- **Unpaved share** is measured from the route's surface tags; **ride type** (road / gravel / MTB) is shown only when the event title or the route surface says so
- **Pace** is shown only when the organizer states exactly one pace
- Start points must be in Colorado; a start pin more than 2 miles from its own route is moved to the route start
- Implausible elevation gain is dropped rather than shown
- The same ride posted on two platforms is listed once (the listing with a route wins)
- Phone numbers and email addresses are removed from descriptions
- An address is only placed on the map when the geocoder finds that specific place, never just the town

Every run writes what it found and corrected to `status.json` on the `data` branch.

## How it stays current

The **Update ride data** workflow runs every hour, and on every push to the default branch. It rebuilds the data and force-pushes it as a single commit to the `data` branch,
so the repository does not grow. The site reads `https://raw.githubusercontent.com/100-debug/Denver-CC/data/`
directly, falling back to the snapshot in `data/`, so new rides appear without redeploying the site.

## Setup

### 1. Publish the site (GitHub Pages)

Every workflow run publishes the finished site to the `gh-pages` branch. To serve it at
`https://100-debug.github.io/Denver-CC/`: **Settings → Pages → Build and deployment → Source: Deploy from a branch →
`gh-pages` / root → Save**. This is a one-time step.

### 2. Connect Strava (needed for most Denver clubs)

1. Create an API application at <https://www.strava.com/settings/api>. Strava's June 2026 terms require an
   active Strava subscription for API access; read the current API agreement before publishing club data.
2. Authorize it once for your own account with the `read` scope and note the refresh token.
3. Add three repository secrets (**Settings → Secrets and variables → Actions**):
   `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `STRAVA_REFRESH_TOKEN`.

Never commit these values: this repository is public. The Strava code path follows the club group-events and
route endpoints but has **not been run against live Strava data yet**. Check the first run's log.

### 3. Optional

- **Map style**: the map uses OpenFreeMap (free, no key). For terrain-shaded MapTiler Outdoors, put a
  domain-locked key in `MAPTILER_API_KEY` in `index.html` and `detailed_weather/index.html`.
- **Custom domain**: add a `CNAME` file and configure it under Settings → Pages.
- **Banner**: `assets/banners/banner.json` shows an image over the map between two dates.

## Adding or removing a source

Edit `backend/config.json` (one line per source):

```json
{ "source": "rwgps",  "id": 3446, "name": "Denver Bicycle Touring Club (DBTC)" }
{ "source": "meetup", "id": "Rocky-Mountain-Cycling-Club-RMCC", "name": "Rocky Mountain Cycling Club RMCC" }
{ "source": "tribe",  "id": "https://bicyclecolorado.org", "name": "Bicycle Colorado calendar", "categories": ["Group Rides"] }
{ "source": "ical",   "id": "https://example.org/rides.ics", "name": "Example Club" }
{ "source": "strava", "id": 1059707, "name": "The Great Golden Gravel Grinder Group (5G)" }
```

Optional keys: `"women_only": true`, `"avg_speed_mph": 16` (enables along-the-route weather),
`"enabled": false`, `"url"`. Major events that are not on BikeReg go in `backend/extra_events.json`,
in the `races.json` format below.

## Run locally

```bash
python3 backend/build_data.py          # refresh data/ (Python 3.9+, no packages needed)
python3 -m unittest discover -s backend/tests
python3 -m http.server 8000            # then open http://localhost:8000
```

## Data files

Same formats as boulderrides.cc, with added fields.

- `club_rides.json`: `id`, `source`, `club_id`, `club_name`, `region` (`denver`, `boulder`, `foco`, `mountains`),
  `title`, `date` (`YYYY-MM-DD HH:MM`, Mountain time), `url`, `starting_location`, `start_name`, `start_address`,
  `description`, `women_only`, `route_id`, `has_route`, `dist_mi`, `elev_gain_ft`, `unpaved_pct`, `ride_type`,
  `pace_mph`, `route` (`[lat, lng, altitude_m, surface]`, surface `1` paved, `0` unpaved, `null` unknown; empty when no route was posted)
- `races.json`: `name`, `url`, `reg_link`, `location`, `latitude`, `longitude`, `region`, `type`, `date`
- `weather.json`: per date, two time slots with `temperature_F`, `description`, `symbol`
- `detailed_weather.json`: per ride, forecast samples every 20 minutes along the route
- `clubs.json`, `status.json`, `geocache.json`: source list, last-run report, cached address lookups

## Known limits

- Strava clubs need credentials (above). Strava only lists the next occurrence of a recurring event.
- Sources with no machine-readable feed are not shown: clubs that publish rides only as text on their own
  site or on Facebook/Instagram (for example DBTC's monthly schedule page, Our Mutual Friend, Denver Cruiser
  Ride, and unofficial rides such as The Meridian or the FDR).
- Elevation gain is the source's own figure (Ride with GPS or Strava); it is sanity-checked, not re-derived.
- Ride with GPS surface tags are read from undocumented per-point codes and checked against each route's
  published unpaved percentage; when they disagree the route falls back to OpenStreetMap, and to "unknown"
  (drawn solid) if that is unreachable.
- Boulder rides depend on the boulderrides.cc data feed staying public.
- The weather widget is for downtown Denver, whichever area is selected.
- Ride details belong to the organizing clubs. Always confirm on the club's own event page.

## Credits

- Frontend: [Boulder Group Rides](https://github.com/RBergua/boulderrides) by Roger Bergua, MIT License
- [MapLibre GL JS](https://maplibre.org/), [OpenFreeMap](https://openfreemap.org/), © OpenStreetMap contributors
- Strava, Ride with GPS, BikeReg, National Weather Service
