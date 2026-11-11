import unittest
from datetime import datetime, timezone
from unittest import mock

import monitor as m


def resp(available, maximum=805, pid="410650", day="2026-12-05"):
    return {"success": True, "availabilityByDate": {f"{pid}-{day}": {
        "date": day, "start": "08:00", "end": "16:30", "available": available, "max": maximum, "status": "success"}}}


class Parse(unittest.TestCase):
    def test_real_shape(self):
        self.assertEqual(m.parse_availability(resp(0), "410650", "2026-12-05"), (0, 805))
        self.assertEqual(m.parse_availability(resp(37, 900), "410650", "2026-12-05"), (37, 900))

    def test_rejects_bad_payloads(self):
        for bad in [None, [], {"success": False}, {"success": True},
                    resp(0, day="2026-12-06"), resp("12"), resp(-1), resp(True)]:
            with self.assertRaises(m.SiteError):
                m.parse_availability(bad, "410650", "2026-12-05")


class Evaluate(unittest.TestCase):
    def test_transitions(self):
        S = m.ProductState
        self.assertEqual(m.evaluate(S(), 0, 805, 1), "baseline")
        self.assertEqual(m.evaluate(S(), 5, 805, 1), "release")  # start mid-drop -> alert
        self.assertEqual(m.evaluate(S(0, 805), 0, 805, 1), None)
        self.assertEqual(m.evaluate(S(0, 805), 120, 925, 1), "release")
        self.assertEqual(m.evaluate(S(40, 925), 30, 925, 1), "change")
        self.assertEqual(m.evaluate(S(10, 925), 90, 1005, 1), "restock")
        self.assertEqual(m.evaluate(S(3, 925), 0, 925, 1), "sold_out")
        self.assertEqual(m.evaluate(S(0, 805), 1, 805, 2), "change")  # below MIN_TICKETS
        self.assertEqual(m.evaluate(S(0, 805), 2, 805, 2), "release")

    def test_alerts_only_on_release(self):
        cfg = m.Config.from_env()
        st = m.ProductState()
        now = datetime(2026, 11, 12, 7, 5, tzinfo=timezone.utc)
        with mock.patch.object(m, "notify") as n, mock.patch.object(m, "log_event"):
            m.handle("410650", st, 0, 805, cfg, now)
            n.assert_not_called()
            m.handle("410650", st, 100, 905, cfg, now)
            self.assertTrue(n.call_args.kwargs["urgent"])
            m.handle("410650", st, 50, 905, cfg, now)
            self.assertEqual(n.call_count, 1)
            m.handle("410650", st, 0, 905, cfg, now)
            self.assertFalse(n.call_args.kwargs["urgent"])


class TimeRules(unittest.TestCase):
    def test_windows(self):
        cfg = m.Config.from_env()
        wed_0815 = datetime(2026, 11, 11, 7, 15, tzinfo=timezone.utc)   # 08:15 Zurich
        wed_1200 = datetime(2026, 11, 11, 11, 0, tzinfo=timezone.utc)   # 12:00 Zurich
        sat_0815 = datetime(2026, 11, 14, 7, 15, tzinfo=timezone.utc)
        night = datetime(2026, 11, 11, 3, 0, tzinfo=timezone.utc)       # 04:00 Zurich
        self.assertEqual(m.poll_interval(wed_0815, cfg), 20)
        self.assertEqual(m.poll_interval(wed_1200, cfg), 60)
        self.assertEqual(m.poll_interval(sat_0815, cfg), 180)
        self.assertFalse(m.in_window(night, cfg.active_hours))
        self.assertTrue(m.within_campaign(wed_0815, cfg))
        self.assertFalse(m.within_campaign(datetime(2026, 12, 5, 8, tzinfo=timezone.utc), cfg))


if __name__ == "__main__":
    unittest.main()
