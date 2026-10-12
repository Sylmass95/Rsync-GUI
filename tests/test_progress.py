"""Tests de l'avancement d'une copie (chronomètres, totaux, octets), sans lancer rsync.

    python3 -m unittest discover tests
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import app  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make_run(clock):
    with mock.patch("time.time", clock):
        run = app.Run({"id": "t", "name": "t", "source": "/nonexistent-src", "dest": "/nonexistent-dst"})
    run.attempt_start = clock.now
    return run


def finish_file(run, size):
    """Simule la ligne finale de progression de rsync pour le fichier en cours."""
    run.cur_size = size
    run.parse("%s 100%%    1.00MB/s    0:00:01 (xfr#1, to-chk=0/1)" % format(size, ","))


def copy_file(run, name, size):
    """Simule rsync : le nom du fichier puis la ligne finale de progression."""
    run.parse(name)
    finish_file(run, size)


class ProgressAccuracyTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        patcher = mock.patch("time.time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.run_ = make_run(self.clock)

    def metrics(self):
        return self.run_.metrics()

    def test_current_file_timer_stops_when_the_file_completes(self):
        run = self.run_
        run.parse("a.bin")
        self.clock.now += 4
        finish_file(run, 100)
        self.assertEqual(self.metrics()["cur_elapsed"], 4)
        self.clock.now += 30          # la copie continue ou est terminée : le chronomètre ne bouge plus
        self.assertEqual(self.metrics()["cur_elapsed"], 4)

    def test_timer_stops_when_the_whole_run_ends(self):
        run = self.run_
        run.parse("a.bin")
        self.clock.now += 3
        run.finished = self.clock.now   # fin de l'exécution, même sans ligne finale
        self.clock.now += 60
        self.assertEqual(self.metrics()["cur_elapsed"], 3)

    def test_timer_ignores_pauses(self):
        run = self.run_
        run.parse("a.bin")
        self.clock.now += 3
        run.pause_started = self.clock.now        # pause
        self.clock.now += 100
        self.assertEqual(self.metrics()["cur_elapsed"], 3)   # figé pendant la pause
        run.paused_total += self.clock.now - run.pause_started   # reprise
        run.pause_started = None
        self.clock.now += 2
        self.assertEqual(self.metrics()["cur_elapsed"], 5)

    def test_a_retransferred_file_is_counted_once(self):
        run = self.run_
        run.plan_files, run.plan_dirs = {"big": 1000}, set()
        copy_file(run, "big", 1000)
        copy_file(run, "big", 1000)               # reprise : le même fichier est terminé une seconde fois
        m = self.metrics()
        self.assertEqual(m["files_done"], 1)
        self.assertEqual(m["done_bytes"], 1000)
        self.assertEqual(m["total_bytes"], 1000)

    def test_totals_grow_when_new_files_appear(self):
        run = self.run_
        run.plan_files, run.plan_dirs = {"a": 100}, {"d/"}
        copy_file(run, "a", 100)
        copy_file(run, "late-file", 40)           # apparu après l'analyse
        run.parse("newdir/")
        m = self.metrics()
        self.assertEqual((m["total_files"], m["files_done"]), (2, 2))
        self.assertEqual((m["total_bytes"], m["done_bytes"]), (140, 140))
        self.assertEqual((m["total_dirs"], m["dirs_done"]), (2, 1))
        self.assertLessEqual(m["done_bytes"], m["total_bytes"])
        self.assertLessEqual(m["files_done"], m["total_files"])

    def test_overall_percent_stays_below_100_until_the_run_is_done(self):
        run = self.run_
        run.plan_files, run.plan_dirs = {"a": 100}, set()
        copy_file(run, "a", 100)
        self.assertEqual(self.metrics()["overall"], 99)
        run.status = "done"
        self.assertEqual(self.metrics()["overall"], 100)

    def test_without_any_analysis_the_totals_are_what_was_copied_at_the_end(self):
        run = self.run_
        copy_file(run, "a", 100)
        self.assertIsNone(self.metrics()["total_files"])
        run.plan_files, run.plan_dirs = {}, set()     # ce que fait la fin d'une exécution réussie
        run.status = "done"
        m = self.metrics()
        self.assertEqual((m["total_files"], m["total_bytes"], m["overall"]), (1, 100, 100))


if __name__ == "__main__":
    unittest.main()
