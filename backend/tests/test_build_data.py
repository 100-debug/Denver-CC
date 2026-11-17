"""Offline tests for the pure helpers in build_data.py (no network)."""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_data as b  # noqa: E402


class Helpers(unittest.TestCase):
    def test_haversine_denver_to_boulder(self):
        # downtown Denver to downtown Boulder is about 24 miles in a straight line
        mi = b.miles_between([39.7392, -104.9903], [40.01499, -105.27055])
        self.assertAlmostEqual(mi, 24.1, delta=0.5)

    def test_decode_polyline(self):
        # reference example from the Google polyline algorithm documentation
        pts = b.decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
        self.assertEqual(pts, [[38.5, -120.2], [40.7, -120.95], [43.252, -126.453]])

    def test_bikereg_date(self):
        # midnight Eastern on 2026-11-14
        self.assertEqual(b.bikereg_date("/Date(1794632400000-0500)/"), dt.date(2026, 11, 14))
        self.assertIsNone(b.bikereg_date("nope"))

    def test_pace_from_text(self):
        self.assertEqual(b.pace_from_text("No-drop, 16-18 mph on the flats"), 17.0)
        self.assertEqual(b.pace_from_text("Rolling at 15 mph."), 15.0)
        self.assertIsNone(b.pace_from_text("A group 18-20 mph, B group 15-17 mph"))  # ambiguous
        self.assertIsNone(b.pace_from_text("Speed limit 55 mph on this road"))
        self.assertIsNone(b.pace_from_text(None))

    def test_weather_symbol(self):
        self.assertEqual(b.weather_symbol("Slight Chance Showers And Thunderstorms"), "🌩️")
        self.assertEqual(b.weather_symbol("Sunny"), "☀️")
        self.assertEqual(b.weather_symbol("Mostly Sunny"), "🌤️")
        self.assertEqual(b.weather_symbol("Chance Snow Showers"), "🌨️")

    def test_hour_label(self):
        self.assertEqual([b.hour_label(h) for h in (0, 9, 12, 16)], ["12 AM", "9 AM", "12 PM", "4 PM"])

    def test_rwgps_surfaces_checked_against_unpaved_pct(self):
        pts = [{"x": 0, "y": 0, "d": i * 100.0, "S": 3 if i < 75 else 63} for i in range(101)]
        tags = b.rwgps_surfaces(pts, 25)
        self.assertEqual(tags[10], 1)
        self.assertEqual(tags[90], 0)
        # our reading disagrees with what Ride with GPS reports -> do not trust it
        self.assertIsNone(b.rwgps_surfaces(pts, 80))
        # mostly unknown surface ids -> do not trust it
        self.assertIsNone(b.rwgps_surfaces([{"x": 0, "y": 0, "d": i, "S": 95} for i in range(10)], 0))

    def test_thin_route_keeps_ends_and_surface_changes(self):
        route = [[40 + i * 1e-5, -105, 1600, 1 if i < 2500 else 0] for i in range(5000)]
        thin = b.thin_route(route, limit=1000)
        self.assertLessEqual(len(thin), 1010)
        self.assertEqual(thin[0], route[0])
        self.assertEqual(thin[-1], route[-1])
        self.assertIn(route[2500], thin)

    def test_point_at(self):
        route = [[40.0, -105.0], [40.0, -104.0]]
        cum = b.cumulative_m(route)
        lat, lng = b.point_at(route, cum, cum[-1] / 2)
        self.assertAlmostEqual(lng, -104.5, places=3)

    def test_sun_times_denver(self):
        # published sunrise/sunset for Denver on 2026-11-17: about 6:47 AM / 4:43 PM MST
        rise, sset = b.sun_times(dt.date(2026, 11, 17), 39.7392, -104.9903)
        self.assertEqual((rise.hour, sset.hour), (6, 16))
        self.assertAlmostEqual(rise.minute, 47, delta=4)
        self.assertAlmostEqual(sset.minute, 43, delta=4)

    CFG = {"regions": {"denver": {"center": [39.7392, -104.9903], "radius_mi": 30},
                       "boulder": {"center": [40.01499, -105.27055], "radius_mi": 22},
                       "foco": {"center": [40.5853, -105.0844], "radius_mi": 30},
                       "mountains": {}},
           "mountain_line_lng": -105.6, "mountain_edge_lng": -105.2}

    def test_region_for(self):
        r = lambda lat, lng: b.region_for([lat, lng], self.CFG)  # noqa: E731
        self.assertEqual(r(39.7555, -105.2211), "denver")     # Golden
        self.assertEqual(r(39.9778, -105.1319), "boulder")    # Louisville
        self.assertEqual(r(40.1672, -105.1019), "boulder")    # Longmont
        self.assertEqual(r(40.3978, -105.0750), "foco")       # Loveland
        self.assertEqual(r(39.6403, -106.3742), "mountains")  # Vail
        self.assertEqual(r(39.4817, -106.0384), "mountains")  # Breckenridge
        self.assertEqual(r(40.4850, -106.8317), "mountains")  # Steamboat Springs
        self.assertEqual(r(39.6333, -105.3172), "denver")     # Evergreen stays a Denver foothills ride
        self.assertIsNone(r(41.14, -104.82))                  # Cheyenne, WY: outside coverage
        self.assertIsNone(r(35.68, -105.94))                  # Santa Fe, NM

    def test_clean_text_removes_contact_details_and_links(self):
        t = b.clean_text("<p>Meet <b>Sam</b> (720-555-0199) or sam@example.org. Route: [map](https://x.y/z) https://a.b</p>")
        self.assertNotIn("720", t)
        self.assertNotIn("@", t)
        self.assertNotIn("http", t)
        self.assertIn("Meet Sam", t)
        self.assertTrue(len(b.clean_text("word " * 500)) <= 601)

    def test_ride_type_needs_firm_evidence(self):
        self.assertEqual(b.ride_type_for("Sunday Gravel Ride", "", None), "gravel")
        self.assertEqual(b.ride_type_for("Thursday MTB", "", None), "mtb")
        self.assertEqual(b.ride_type_for("Saturday Roll", "", 0), "road")
        self.assertEqual(b.ride_type_for("Saturday Roll", "", 35), "gravel")
        # words in a description are not evidence
        self.assertIsNone(b.ride_type_for("Tuesday Night Climb", "road conditions may vary, some gravel", None))

    def test_looks_like_ride(self):
        self.assertTrue(b.looks_like_ride("Northside Winter Road Ride", ""))
        self.assertFalse(b.looks_like_ride("Annual Banquet", "Join us for dinner"))
        self.assertFalse(b.looks_like_ride("Board Meeting", "we will discuss rides"))
        self.assertTrue(b.looks_like_ride("Phil's Circuit", "social B ride on the Greenway"))

    def test_parse_ical(self):
        text = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART;TZID=America/Denver:20261121T094500\r\n"
                "SUMMARY:Winter Ride\\, north\r\nDESCRIPTION:line one\\nline\r\n  two\r\n"
                "URL;VALUE=URI:https://www.meetup.com/x/events/1/\r\nEND:VEVENT\r\n"
                "BEGIN:VEVENT\r\nDTSTART;VALUE=DATE:20261122\r\nSUMMARY:All day\r\nEND:VEVENT\r\nEND:VCALENDAR")
        evs = b.parse_ical(text)
        self.assertEqual(len(evs), 2)
        self.assertEqual(evs[0]["SUMMARY"], "Winter Ride, north")
        self.assertEqual(evs[0]["DESCRIPTION"], "line one\nline two")
        self.assertEqual(evs[0]["URL"], "https://www.meetup.com/x/events/1/")
        self.assertEqual(b.fmt_ride_date(b.ical_start(evs[0])), "2026-11-21 09:45")
        self.assertIsNone(b.ical_start(evs[1]))   # all-day entries have no start time

    def test_find_venue(self):
        data = {"props": {"a": [{"venue": {"name": "Park", "lat": 39.7, "lng": -105.2, "address": "1 Main"}}]}}
        self.assertEqual(b._find_venue(data)["name"], "Park")
        self.assertIsNone(b._find_venue({"x": {"lat": 0, "lng": 0, "name": "online"}}))

    def _ride(self, **kw):
        base = {"source": "Meetup", "club_name": "Club", "title": "Saturday Ride", "date": "2026-11-21 09:00",
                "url": "https://x/1", "starting_location": [39.75, -105.0], "women_only": False,
                "route_id": None, "dist_mi": None, "elev_gain_ft": None, "route": []}
        base.update(kw)
        return base

    def test_finish_rides_measures_distance_and_flags_mismatch(self):
        # 0.1 degree of latitude is 6.9 miles; the source claims 20
        route = [[39.75 + i * 0.001, -105.0, 1600, 1] for i in range(101)]
        rides, warnings = b.finish_rides([self._ride(route=route, dist_mi=20.0, route_id=7, elev_gain_ft=100)], self.CFG)
        self.assertAlmostEqual(rides[0]["dist_mi"], 6.9, delta=0.1)
        self.assertTrue(any("distance corrected" in w for w in warnings))
        self.assertTrue(rides[0]["has_route"])
        self.assertEqual(rides[0]["unpaved_pct"], 0)
        self.assertEqual(rides[0]["region"], "denver")
        self.assertEqual(len(rides[0]["id"]), 10)

    def test_finish_rides_pin_only_and_outside_coverage(self):
        rides, warnings = b.finish_rides([self._ride(), self._ride(title="Far", starting_location=[41.14, -104.82])], self.CFG)
        self.assertEqual(len(rides), 1)
        self.assertFalse(rides[0]["has_route"])
        self.assertIsNone(rides[0]["dist_mi"])
        self.assertTrue(any("outside coverage" in w for w in warnings))

    def test_finish_rides_removes_cross_platform_duplicates(self):
        route = [[39.75 + i * 0.001, -105.0, 1600, 1] for i in range(50)]
        a = self._ride()                                             # Meetup listing, no route
        c = self._ride(source="Ride with GPS", url="https://y/2", route=route, route_id=9, dist_mi=3.4)
        rides, _ = b.finish_rides([a, c], self.CFG)
        self.assertEqual(len(rides), 1)
        self.assertEqual(rides[0]["source"], "Ride with GPS")        # the listing with a route wins
        # two route options of the same event are both kept
        d = self._ride(source="Ride with GPS", url="https://y/2", route=route, route_id=10, dist_mi=3.4)
        rides, _ = b.finish_rides([c, d], self.CFG)
        self.assertEqual(len(rides), 2)

    def test_make_ride_shape(self):
        club = {"id": 1, "name": "Test Club"}
        start = dt.datetime(2026, 11, 21, 9, 0, tzinfo=b.TZ)
        r = b.make_ride("Ride with GPS", club, " Saturday ", start, "https://x", [39.7, -105.0], 5,
                        16093.44, 100.0, [[39.7, -105.0, 1600, 1]], None)
        self.assertEqual(r["date"], "2026-11-21 09:00")
        self.assertEqual(r["dist_mi"], 10.0)
        self.assertEqual(r["elev_gain_ft"], 328)
        self.assertEqual(r["title"], "Saturday")
        self.assertFalse(r["women_only"])


if __name__ == "__main__":
    unittest.main()
