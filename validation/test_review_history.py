#!/usr/bin/env python3
"""Review-history tests. No MapRoulette writes."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from maproulette import MapRouletteError
from review_history import (
    STRAVA_DB_NAI,
    canonical_status,
    discover_production_challenges,
    is_not_an_issue,
    is_smoke_challenge,
    load_history,
    nai_ids_from_sqlite,
    register_production_challenge,
    sync_review_history,
    tasks_db_path,
)
from workflow import detector_command

REGRESSION_NAI = [
    "14/8304/6231/287/876",
    "14/8305/6230/507/1013",
    "14/8312/6217/88/51",
    "14/8314/6220/590/849",
    "14/8318/6212/145/346",
    "14/8323/6227/38/62",
    "14/8323/6221/518/94",
]
REGRESSION_FIXED = "14/8304/6232/652/980"
REGRESSION_TOO_HARD = "14/8300/6220/1/1"


def _task(name, status, task_id=1, parent=56735):
    return {"id": task_id, "name": name, "status": status, "parent": parent, "completedBy": "tester"}


def challenge_56735_tasks():
    tasks = []
    for index, name in enumerate(REGRESSION_NAI, start=1):
        tasks.append(_task(name, 2, 100 + index))
    for index in range(50):
        name = REGRESSION_FIXED if index == 0 else f"14/fixed/{index}/0/0"
        tasks.append(_task(name, 1, 200 + index))
    for index in range(13):
        name = REGRESSION_TOO_HARD if index == 0 else f"14/hard/{index}/0/0"
        tasks.append(_task(name, 6, 300 + index))
    return tasks


class FakeClient:
    def __init__(self, by_id, challenges=None, fail=False):
        self.by_id = {int(key): list(value) for key, value in by_id.items()}
        self.challenges = challenges or {
            56735: {"id": 56735, "name": "Strava Mallorca Run 2026-09-03 1753", "parent": 54842},
        }
        self.fail = fail
        self.calls = 0

    def get_challenge(self, challenge_id):
        self.calls += 1
        if self.fail:
            raise MapRouletteError("HTTP 503: unavailable")
        data = self.challenges.get(int(challenge_id))
        if data is None:
            raise MapRouletteError(f"GET /challenge/{challenge_id} fehlgeschlagen (HTTP 404)")
        return data

    def list_tasks(self, challenge_id, **_kwargs):
        self.calls += 1
        if self.fail:
            raise MapRouletteError("HTTP 503: unavailable")
        if int(challenge_id) not in self.by_id:
            raise MapRouletteError(f"GET /challenge/{challenge_id}/tasks fehlgeschlagen (HTTP 404)")
        return self.by_id[int(challenge_id)]


class StatusTests(unittest.TestCase):
    def test_api_integer_is_nai(self):
        self.assertTrue(is_not_an_issue(2))
        self.assertTrue(is_not_an_issue("2"))
        self.assertEqual(canonical_status(2), "Not_An_Issue")

    def test_csv_and_strava_spellings(self):
        self.assertTrue(is_not_an_issue("Not_An_Issue"))
        self.assertTrue(is_not_an_issue("Not_an_Issue"))
        self.assertTrue(is_not_an_issue("false_positive"))
        self.assertEqual(STRAVA_DB_NAI, "Not_an_Issue")

    def test_other_statuses_are_not_nai(self):
        for value in (1, 5, 6, 3, "Fixed", "Already_Fixed", "Too_Hard", "Skipped", 0, "Created"):
            self.assertFalse(is_not_an_issue(value), value)
        self.assertEqual(canonical_status(1), "Fixed")
        self.assertEqual(canonical_status(6), "Too_Hard")
        self.assertEqual(canonical_status(5), "Already_Fixed")


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def seed_challenge(self, cid=56735, layer="run", name="Strava Mallorca Run 2026-09-03 1753"):
        register_production_challenge("mallorca", {
            "id": cid,
            "layer": layer,
            "name": name,
            "task_count": 70,
            "project_id": 54842,
        }, state_dir=self.state)

    def test_sync_only_stores_nai(self):
        self.seed_challenge()
        client = FakeClient({56735: challenge_56735_tasks()})
        result = sync_review_history("mallorca", client, state_dir=self.state)
        self.assertTrue(result.ok)
        self.assertEqual(result.challenges_checked, 1)
        self.assertEqual(result.nai_count, 7)
        history = load_history("mallorca", self.state)
        self.assertEqual(set(history["not_an_issue"]), set(REGRESSION_NAI))
        self.assertNotIn(REGRESSION_FIXED, history["not_an_issue"])
        self.assertNotIn(REGRESSION_TOO_HARD, history["not_an_issue"])
        db = tasks_db_path("mallorca", self.state)
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = {name: status for name, status in con.execute("SELECT TaskName, TaskStatus FROM tasks")}
        finally:
            con.close()
        self.assertEqual(set(rows), set(REGRESSION_NAI))
        self.assertTrue(all(status == "Not_an_Issue" for status in rows.values()))
        nai = con_row(db, REGRESSION_NAI[0])
        self.assertEqual(nai[0], "Not_an_Issue")
        self.assertIsNone(con_row(db, REGRESSION_FIXED))
        self.assertIsNone(con_row(db, REGRESSION_TOO_HARD))

    def test_repeated_sync_is_idempotent(self):
        self.seed_challenge()
        client = FakeClient({56735: challenge_56735_tasks()})
        first = sync_review_history("mallorca", client, state_dir=self.state)
        second = sync_review_history("mallorca", client, state_dir=self.state)
        self.assertEqual(first.nai_count, second.nai_count)
        self.assertEqual(
            set(load_history("mallorca", self.state)["not_an_issue"]),
            set(REGRESSION_NAI),
        )
        self.assertEqual(nai_ids_from_sqlite("mallorca", self.state), set(REGRESSION_NAI))

    def test_api_failure_preserves_history(self):
        self.seed_challenge()
        sync_review_history(
            "mallorca",
            FakeClient({56735: challenge_56735_tasks()}),
            state_dir=self.state,
        )
        failed = sync_review_history(
            "mallorca",
            FakeClient({56735: []}, fail=True),
            state_dir=self.state,
        )
        self.assertTrue(failed.used_cache)
        self.assertEqual(set(load_history("mallorca", self.state)["not_an_issue"]), set(REGRESSION_NAI))
        self.assertEqual(nai_ids_from_sqlite("mallorca", self.state), set(REGRESSION_NAI))

    def test_api_failure_without_history_is_not_ok(self):
        self.seed_challenge()
        result = sync_review_history(
            "mallorca",
            FakeClient({}, fail=True),
            state_dir=self.state,
        )
        self.assertFalse(result.ok)
        self.assertFalse((self.state / "mallorca" / "tasks.sqlite").exists())

    def test_fake_challenge_99999_excluded(self):
        register_production_challenge("mallorca", {
            "id": 99999,
            "layer": "ride",
            "name": "Strava Mallorca Ride 2026-09-03 1742",
            "task_count": 15,
        }, state_dir=self.state)
        result_dir = self.state / "mallorca" / "runs" / "fake"
        result_dir.mkdir(parents=True)
        (result_dir / "result.json").write_text(json.dumps({
            "challenges": [{
                "layer": "ride",
                "challenge_name": "Strava Mallorca Ride 2026-09-03 1742",
                "created": True,
                "challenge_id": 99999,
            }],
        }), encoding="utf-8")
        found = discover_production_challenges("mallorca", self.state)
        self.assertNotIn("99999", found)

    def test_smoke_test_excluded(self):
        register_production_challenge("mallorca", {
            "id": 56733,
            "layer": "ride",
            "name": "Strava Mallorca API Test 2026-09-03 1733",
            "task_count": 2,
        }, state_dir=self.state)
        self.seed_challenge()
        discovered = discover_production_challenges("mallorca", self.state)
        self.assertNotIn("56733", discovered)
        self.assertIn("56735", discovered)
        self.assertTrue(is_smoke_challenge({"name": "Strava Mallorca API Test 2026-09-03 1733"}))
        client = FakeClient({
            56733: [_task("14/should-not-appear/0/0/0", 2)],
            56735: challenge_56735_tasks(),
        }, challenges={
            56733: {"id": 56733, "name": "Strava Mallorca API Test 2026-09-03 1733", "parent": 54842},
            56735: {"id": 56735, "name": "Strava Mallorca Run 2026-09-03 1753", "parent": 54842},
        })
        result = sync_review_history("mallorca", client, state_dir=self.state)
        self.assertNotIn("14/should-not-appear/0/0/0", load_history("mallorca", self.state)["not_an_issue"])
        self.assertEqual(result.nai_count, 7)

    def test_new_run_does_not_erase_old_challenges(self):
        self.seed_challenge()
        register_production_challenge("mallorca", {
            "id": 56738,
            "layer": "all",
            "name": "Strava Mallorca All 2026-09-03 1908",
            "task_count": 40,
        }, state_dir=self.state)
        ui = {
            "phase1": {"challenges": [{
                "layer": "ride",
                "challenge_name": "Strava Mallorca Ride 2026-09-04 1000",
                "created": True,
                "challenge_id": 57001,
            }]},
            "phase2": None,
        }
        path = self.state / "mallorca" / "ui.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ui), encoding="utf-8")
        found = discover_production_challenges("mallorca", self.state)
        self.assertIn("56735", found)
        self.assertIn("56738", found)
        self.assertIn("57001", found)

    def test_detector_command_includes_tasks_db(self):
        self.seed_challenge()
        sync_review_history(
            "mallorca",
            FakeClient({56735: challenge_56735_tasks()}),
            state_dir=self.state,
        )
        db = tasks_db_path("mallorca", self.state)
        cmd = detector_command(
            {
                "boundary": "boundaries/mallorca.geojson",
                "extract_xml": "osm-data/mallorca/current.osm",
                "id": "mallorca",
            },
            "run",
            "run.raw.geojson",
            "run-stats.json",
            14,
            tasks_db=db,
        )
        self.assertIn("-b", cmd)
        self.assertEqual(cmd[cmd.index("-b") + 1], str(db))
        self.assertIn("-z", cmd)
        self.assertEqual(cmd[cmd.index("-z") + 1], "14")


def con_row(db, name):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT TaskStatus,Mapper,TaskLink FROM tasks WHERE TaskName=?",
            (name,),
        ).fetchone()
    finally:
        con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
