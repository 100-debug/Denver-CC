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

    def test_in_range(self):
        cfg = {"regions": {"denver": {"center": [39.7392, -104.9903]}}, "home_radius_mi": 35, "other_radius_mi": 15}
        golden = [39.7555, -105.2211]
        boulder = [40.01499, -105.27055]
        self.assertTrue(b.in_range({"region": "denver"}, golden, cfg))
        self.assertTrue(b.in_range({"region": "denver"}, boulder, cfg))
        self.assertFalse(b.in_range({"region": "denver", "home": False}, boulder, cfg))

    def test_make_ride_shape(self):
        club = {"id": 1, "name": "Test Club", "region": "denver"}
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
