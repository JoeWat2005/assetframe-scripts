"""Tests for calibrate.py — PAVA monotonicity, shrinkage-to-identity, and the
empty/young-ledger identity guarantees.

Run:  python scripts/test_calibrate.py
"""
import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import calibrate as K


class TestPAVA(unittest.TestCase):
    def test_monotone_non_decreasing_output(self):
        out = K.pava([0.5, 0.2, 0.8], [1, 1, 1])
        self.assertEqual(len(out), 3)
        for i in range(len(out) - 1):
            self.assertLessEqual(out[i], out[i + 1] + 1e-12)

    def test_already_monotone_unchanged(self):
        self.assertEqual(K.pava([0.1, 0.5, 0.9], [1, 1, 1]), [0.1, 0.5, 0.9])

    def test_pooling_averages_violation(self):
        # [0.5, 0.2] violates -> pooled to the weighted mean 0.35 each
        self.assertEqual(K.pava([0.5, 0.2], [1, 1]), [0.35, 0.35])

    def test_weighted_pooling(self):
        # weights bias the pooled mean: (0.6*3 + 0.0*1)/4 = 0.45
        out = K.pava([0.6, 0.0], [3, 1])
        self.assertAlmostEqual(out[0], 0.45, places=6)
        self.assertAlmostEqual(out[1], 0.45, places=6)


class TestMergeDuplicateX(unittest.TestCase):
    def test_combines_same_x(self):
        merged = K._merge_duplicate_x([(50.0, 1.0, 2), (50.0, 0.0, 2), (60.0, 1.0, 1)])
        d = {x: (y, w) for x, y, w in merged}
        self.assertAlmostEqual(d[50.0][0], 0.5, places=6)   # weighted mean
        self.assertEqual(d[50.0][1], 4)                      # weight summed
        self.assertEqual(d[60.0][1], 1)

    def test_sorted_ascending(self):
        merged = K._merge_duplicate_x([(80.0, 1.0, 1), (40.0, 1.0, 1)])
        self.assertEqual([x for x, _, _ in merged], [40.0, 80.0])


class TestBuildMapIdentity(unittest.TestCase):
    def test_empty_is_identity(self):
        m = K.build_map([])
        self.assertEqual(m["method"], "identity")
        self.assertEqual(m["shrinkage_w"], 0.0)
        self.assertEqual(m["knots"], [[0.0, 0.0], [100.0, 100.0]])
        self.assertEqual(m["n_rows"], 0)

    def test_below_min_rows_is_identity(self):
        m = K.build_map([(50.0, 0.6, 1)] * 4, min_rows=5)
        self.assertEqual(m["method"], "identity")
        self.assertEqual(m["shrinkage_w"], 0.0)

    def test_at_min_rows_starts_fitting(self):
        m = K.build_map([(50.0, 0.6, 1)] * 6, min_rows=5, n_full=40)
        self.assertEqual(m["method"], "isotonic+shrinkage")
        # 6/40 = 0.15 -> heavily shrunk toward identity but not pure identity
        self.assertAlmostEqual(m["shrinkage_w"], 0.15, places=3)


class TestBuildMapShape(unittest.TestCase):
    def test_knots_strictly_ascending_x_with_endpoints(self):
        pts = [(40.0, 0.9, 5), (50.0, 0.2, 5), (60.0, 0.5, 5),
               (70.0, 0.4, 5), (80.0, 0.95, 5)]
        m = K.build_map(pts)
        xs = [k[0] for k in m["knots"]]
        self.assertEqual(xs[0], 0.0)
        self.assertEqual(xs[-1], 100.0)
        for i in range(len(xs) - 1):
            self.assertLess(xs[i], xs[i + 1])

    def test_knots_y_monotone_non_decreasing(self):
        pts = [(40.0, 0.9, 5), (50.0, 0.2, 5), (60.0, 0.5, 5),
               (70.0, 0.4, 5), (80.0, 0.95, 5)]
        m = K.build_map(pts)
        ys = [k[1] for k in m["knots"]]
        for i in range(len(ys) - 1):
            self.assertLessEqual(ys[i], ys[i + 1] + 1e-9)

    def test_full_weight_capped_at_one(self):
        m = K.build_map([(50.0, 0.6, 1)] * 100, n_full=40)
        self.assertEqual(m["shrinkage_w"], 1.0)

    def test_conf_version_recorded(self):
        m = K.build_map([])
        self.assertEqual(m["conf_version"], K.CONF_VERSION)


class TestLoadPoints(unittest.TestCase):
    def _write_ledger(self, rows, cols=None):
        cols = cols or ["conf_version", "conf_raw", "confidence", "hits", "misses"]
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow(r)
        self.addCleanup(os.remove, path)
        return path

    def test_missing_file_returns_empty(self):
        self.assertEqual(K.load_points("does_not_exist_xyz.csv", 2), [])

    def test_filters_conf_version(self):
        path = self._write_ledger([
            {"conf_version": "2", "conf_raw": "60", "confidence": "", "hits": "3", "misses": "1"},
            {"conf_version": "1", "conf_raw": "55", "confidence": "", "hits": "2", "misses": "2"},
        ])
        pts = K.load_points(path, 2)
        self.assertEqual(len(pts), 1)
        self.assertEqual(pts[0][0], 60.0)
        self.assertAlmostEqual(pts[0][1], 0.75, places=6)
        self.assertEqual(pts[0][2], 4)

    def test_falls_back_to_confidence_when_no_conf_raw(self):
        path = self._write_ledger([
            {"conf_version": "2", "conf_raw": "", "confidence": "64", "hits": "1", "misses": "1"},
        ])
        pts = K.load_points(path, 2)
        self.assertEqual(pts[0][0], 64.0)

    def test_skips_rows_with_no_outcomes(self):
        path = self._write_ledger([
            {"conf_version": "2", "conf_raw": "60", "confidence": "", "hits": "0", "misses": "0"},
        ])
        self.assertEqual(K.load_points(path, 2), [])

    def test_conf_version_none_keeps_all(self):
        path = self._write_ledger([
            {"conf_version": "2", "conf_raw": "60", "confidence": "", "hits": "3", "misses": "1"},
            {"conf_version": "1", "conf_raw": "55", "confidence": "", "hits": "2", "misses": "2"},
        ])
        self.assertEqual(len(K.load_points(path, None)), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
