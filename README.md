# Denver Group Rides

An interactive map of **cycling group rides across metro Denver and Boulder**: all clubs, one map.
It is the Denver counterpart of [boulderrides.cc](https://boulderrides.cc), built from that project's
MIT-licensed frontend ([RBergua/boulderrides](https://github.com/RBergua/boulderrides)) plus a new, open backend.

As of November 2026 no equivalent existed for Denver: the closest things are hand-curated lists
(Bike Community, Holy Cow's "Denver Stampedes", Meetup, Bicycle Colorado's calendar), none of which
pull club routes onto a live map.

## What it does

- **7-day calendar** with the first day that has rides loaded automatically
- **Routes on a map**, color-coded per ride, solid for paved and dashed for unpaved, with start pins and direction arrows
- **Area button** (new): show All, Denver only, or Boulder only
- **Ride card** with club, title, time, distance, elevation gain and a link to the club's event page
- **Races & Events** page and checkered pins, from BikeReg, within 35 miles of downtown Denver or Boulder
- **Weather**: downtown Denver forecast per day, plus forecast along the route when the pace is known
- **GPX/List** page with one-tap GPX downloads
- **Metric/Imperial** toggle, installable as a phone home-screen app
- No analytics, no cookies, no tracking

## Where the rides come from

| Source | What | Credentials |
|---|---|---|
| Ride with GPS | Events of the organizations in `backend/config.json`, with route, elevation and surface | None |
| Strava | Group events of the clubs in `backend/config.json` | Strava API app (see below) |
| Boulder feed | Every ride published by boulderrides.cc (its public `data` branch), shown as-is | None |
| BikeReg | Races and events in Colorado, filtered by distance and date | None |
| National Weather Service | Hourly forecasts | None |
| OpenStreetMap (Overpass) | Paved/unpaved tagging for routes that come without surface data | None |

Rules carried over from the Boulder site: rides without a route are left out; a ride must start within
35 miles of downtown Denver (15 miles for clubs marked `"home": false`).

**Most Denver clubs post their rides on Strava.** Of the 49 Denver clubs configured, 7 are on Ride with GPS
and 42 are on Strava. Until Strava credentials are added, the 42 Strava clubs show as gray badges and
contribute no rides. Boulder rides appear either way.

## Setup

### 1. Publish the site (GitHub Pages)

1. Merge this branch into `main` (scheduled workflows only run on the default branch).
2. Repository **Settings → Pages → Build and deployment → Source: GitHub Actions**.
3. **Actions → Update ride data and deploy site → Run workflow**.

The workflow then runs twice a day (about 12 PM and 1 AM Mountain), rebuilds `data/*.json`, and deploys.
Nothing is committed back to the repository, so it does not grow.

### 2. Connect Strava (needed for most Denver clubs)

1. Create an API application at <https://www.strava.com/settings/api>. Strava's June 2026 terms require an
   active Strava subscription for API access; read the current API agreement before publishing club data.
2. Authorize it once for your own account with the `read` scope and note the refresh token.
3. Add three repository secrets (**Settings → Secrets and variables → Actions**):
   `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `STRAVA_REFRESH_TOKEN`.

The Strava code path follows the club group-events and route endpoints but has **not been run against live
Strava data yet** (no credentials were available when it was written). Check the first run's log.

### 3. Optional

- **Map style**: the map uses OpenFreeMap (free, no key). For terrain-shaded MapTiler Outdoors, put a
  domain-locked key in `MAPTILER_API_KEY` in `index.html` and `detailed_weather/index.html`.
- **Custom domain**: add a `CNAME` file and configure it under Settings → Pages.
- **Banner**: `assets/banners/banner.json` shows an image over the map between two dates.

## Adding or removing a club

Edit `backend/config.json`:

```json
{ "source": "strava", "id": 1059707, "name": "The Great Golden Gravel Grinder Group (5G)", "region": "denver" }
{ "source": "rwgps",  "id": 3446,    "name": "Denver Bicycle Touring Club (DBTC)", "region": "denver" }
```

Optional keys: `"home": false` (15-mile radius), `"women_only": true`, `"avg_speed_mph": 16`
(enables along-the-route weather), `"enabled": false`, `"url"`.
Major events that are not on BikeReg go in `backend/extra_events.json`, in the `races.json` format below.

## Run locally

```bash
python3 backend/build_data.py          # refresh data/ (Python 3.9+, no packages needed)
python3 -m unittest discover -s backend/tests
python3 -m http.server 8000            # then open http://localhost:8000
```

## Data files

Same formats as boulderrides.cc, with one added field (`region`).

- `data/club_rides.json`: `source`, `club_id`, `club_name`, `region` (`denver` or `boulder`), `title`,
  `date` (`YYYY-MM-DD HH:MM`, Mountain time), `url`, `starting_location`, `women_only`, `route_id`,
  `dist_mi`, `elev_gain_ft`, `route` (`[lat, lng, altitude_m, surface]`, surface `1` paved, `0` unpaved, `null` unknown)
- `data/races.json`: `name`, `url`, `reg_link`, `location`, `latitude`, `longitude`, `type`, `date`
- `data/weather.json`: per date, two time slots with `temperature_F`, `description`, `symbol`
- `data/detailed_weather.json`: per ride, forecast samples every 20 minutes along the route
- `data/clubs.json`, `data/status.json`: club badge list and last-run status for the "More" page

## Known limits

- Strava clubs need credentials (above). Strava only lists the next occurrence of a recurring event.
- Clubs that publish rides only as text on their own site, Meetup or Facebook (for example DBTC's monthly
  schedule page, Our Mutual Friend, Denver Cruiser Ride, and unofficial rides such as The Meridian or the FDR)
  have no machine-readable route and are not shown.
- Ride with GPS surface tags are read from undocumented per-point codes and checked against each route's
  published unpaved percentage; when they disagree the route falls back to OpenStreetMap, and to "unknown"
  (drawn solid) if that is unreachable.
- Boulder rides depend on the boulderrides.cc data feed staying public. If it goes away, add the Boulder
  clubs to `backend/config.json` with `"region": "boulder"`.
- Ride details belong to the organizing clubs. Always confirm on the club's own event page.

## Credits

- Frontend: [Boulder Group Rides](https://github.com/RBergua/boulderrides) by Roger Bergua, MIT License
- [MapLibre GL JS](https://maplibre.org/), [OpenFreeMap](https://openfreemap.org/), © OpenStreetMap contributors
- Strava, Ride with GPS, BikeReg, National Weather Service
