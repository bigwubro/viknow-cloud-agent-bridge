"""Session lifecycle on the nested agent routes: submit, replay, delete (docs: start/api/runs.html)."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("VIKNOW_ACCESS_DSN", "postgresql://test:test@127.0.0.1:5432/access")
os.environ.setdefault("VIKNOW_AGENT_DSN", "postgresql://test:test@127.0.0.1:5432/agent")
os.environ.setdefault("VIKNOW_HYPERRAG_DSN", "postgresql://test:test@127.0.0.1:5432/hyperrag")
os.environ.setdefault("VIKNOW_USE_SCRIPT_MODEL", "1")

from fastapi.testclient import TestClient

from viknow3.server import app
from viknow3.store import STORE, RunRef, _rows

AUTH = {"Authorization": "Bearer dev-app-key"}
AGENT = "/api/v1/agent/agents/expertbuddy-deepagents"


def setUpModule() -> None:
    from agents.runtimes.deepagents.agent_adapters.checkpointer import reset_checkpoint_tables
    from viknow3.pg import AGENT, dsn, require_test_database

    STORE.reset()
    require_test_database(AGENT)
    reset_checkpoint_tables(dsn(AGENT))
    from hyperrag_stub import install_script_engine

    install_script_engine()


def _parse_sse(raw: str) -> list[dict]:
    import json

    return [json.loads(line[len("data: ") :]) for line in raw.splitlines() if line.startswith("data: ")]


class SessionLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def setUp(self) -> None:
        self.user = self.client.post("/api/v1/users", json={"display_name": "删会话"}, headers=AUTH).json()

    def start(self, message: str = "你好", files: list[tuple[str, bytes]] | None = None) -> dict:
        if files:
            response = self.client.post(
                f"{AGENT}/sessions",
                data={"user_id": self.user["id"], "message": message, "library_ids": "library-1"},
                files=[("files", (name, content, "text/plain")) for name, content in files],
                headers=AUTH,
            )
        else:
            response = self.client.post(
                f"{AGENT}/sessions",
                json={"user_id": self.user["id"], "message": message, "library_ids": ["library-1"]},
                headers=AUTH,
            )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def finish(self, submitted: dict) -> list[dict]:
        raw = self.client.get(
            f"{AGENT}/sessions/{submitted['session_id']}/runs/{submitted['run_id']}/events", headers=AUTH
        ).text
        return _parse_sse(raw)

    def running_ref(self, session_id: str) -> RunRef:
        """A run marked running that no task in this process owns."""
        from viknow3.clock import new_id, now

        moment = now()
        ref = RunRef(new_id(), session_id, self.user["id"], "expertbuddy-deepagents", [], "running", moment, input="x", updated_at=moment)
        STORE.runs[ref.id] = ref
        return ref

    def test_submit_result_and_history_replay_the_stored_events(self) -> None:
        submitted = self.start()
        self.assertEqual(
            sorted(submitted), ["agent_id", "run_id", "session_id", "uploaded_file_ids", "workspace_id"]
        )
        self.assertEqual(submitted["uploaded_file_ids"], [])
        streamed = self.finish(submitted)
        self.assertEqual(streamed[-1]["type"], "run_completed")
        self.assertEqual([item["sequence"] for item in streamed], list(range(1, len(streamed) + 1)))

        base = f"{AGENT}/sessions/{submitted['session_id']}/runs/{submitted['run_id']}"
        history = self.client.get(f"{base}/history", headers=AUTH).json()
        self.assertEqual(history, streamed)
        run = self.client.get(base, headers=AUTH).json()
        self.assertEqual(run["status"], "completed")
        self.assertTrue(run["output"])
        self.assertEqual(run["citations"], {})

        tail = _parse_sse(self.client.get(f"{base}/events?after_sequence={len(streamed) - 1}", headers=AUTH).text)
        self.assertEqual([item["type"] for item in tail], ["run_completed"])

    def test_the_final_run_state_survives_a_reload(self) -> None:
        submitted = self.start()
        self.finish(submitted)
        STORE._load()
        run = STORE.runs[submitted["run_id"]]
        self.assertEqual(run.status, "completed")
        self.assertTrue(run.output)
        stored = _rows(
            "SELECT type FROM run_events WHERE run_id = %s ORDER BY sequence DESC LIMIT 1", (submitted["run_id"],)
        )
        self.assertEqual(stored[0][0], "run_completed")

    def test_history_rejects_a_running_run(self) -> None:
        submitted = self.start()
        self.finish(submitted)
        ref = self.running_ref(submitted["session_id"])
        response = self.client.get(f"{AGENT}/sessions/{submitted['session_id']}/runs/{ref.id}/history", headers=AUTH)
        self.assertEqual((response.status_code, response.json()), (409, {"detail": "run_not_finished"}))
        STORE.fail_runs([ref.id], "test cleanup")

    def test_continue_keeps_the_first_run_libraries(self) -> None:
        submitted = self.start()
        self.finish(submitted)
        response = self.client.post(
            f"{AGENT}/sessions/{submitted['session_id']}/runs",
            json={"user_id": self.user["id"], "message": "继续", "library_ids": ["library-2"]},
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 200, response.text)
        second = response.json()
        self.assertEqual(second["session_id"], submitted["session_id"])
        self.finish(second)
        session = self.client.get(f"{AGENT}/sessions/{submitted['session_id']}", headers=AUTH).json()
        self.assertEqual(session["library_ids"], ["library-1"])
        runs = self.client.get(f"{AGENT}/sessions/{submitted['session_id']}/runs", headers=AUTH).json()
        self.assertEqual([run["input"] for run in runs], ["你好", "继续"])

    def test_delete_session_soft_deletes_everything_under_it(self) -> None:
        submitted = self.start(files=[("note.txt", b"hello")])
        self.assertEqual(len(submitted["uploaded_file_ids"]), 1)
        file_id = submitted["uploaded_file_ids"][0]
        self.finish(submitted)
        session_id = submitted["session_id"]
        self.assertEqual(STORE.run_file_ids(submitted["run_id"]), [file_id])

        missing_user = self.client.delete(f"{AGENT}/sessions/{session_id}", headers=AUTH)
        self.assertEqual(missing_user.status_code, 422)
        other = self.client.post("/api/v1/users", json={"display_name": "别人"}, headers=AUTH).json()
        self.assertEqual(self.client.delete(f"{AGENT}/sessions/{session_id}?user_id={other['id']}", headers=AUTH).status_code, 404)
        self.assertEqual(
            self.client.delete(
                f"/api/v1/agent/agents/not-registered/sessions/{session_id}?user_id={self.user['id']}",
                headers=AUTH,
            ).status_code,
            404,
        )

        response = self.client.delete(f"{AGENT}/sessions/{session_id}?user_id={self.user['id']}", headers=AUTH)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")

        base = f"{AGENT}/sessions/{session_id}"
        run_base = f"{base}/runs/{submitted['run_id']}"
        for path in (base, f"{base}/runs", f"{base}/files", run_base, f"{run_base}/history", f"{run_base}/events"):
            self.assertEqual(self.client.get(path, headers=AUTH).status_code, 404, path)
        listed = self.client.get(f"{AGENT}/sessions?user_id={self.user['id']}", headers=AUTH).json()
        self.assertNotIn(session_id, [item["id"] for item in listed])
        again = self.client.post(f"{base}/runs", json={"user_id": self.user["id"], "message": "还在吗"}, headers=AUTH)
        self.assertEqual(again.status_code, 404)
        self.assertEqual(self.client.delete(f"{base}?user_id={self.user['id']}", headers=AUTH).status_code, 404)

        session = STORE.sessions[session_id]
        stamps = {
            session.deleted_at,
            STORE.workspaces[session.workspace_id].deleted_at,
            STORE.files[file_id].deleted_at,
            STORE.runs[submitted["run_id"]].deleted_at,
            *[row[0] for row in _rows("SELECT deleted_at FROM run_events WHERE run_id = %s", (submitted["run_id"],))],
            *[row[0] for row in _rows("SELECT deleted_at FROM run_files WHERE run_id = %s", (submitted["run_id"],))],
        }
        self.assertEqual(len(stamps), 1)
        self.assertIsNotNone(stamps.pop())

    def test_delete_session_refuses_while_a_run_is_running(self) -> None:
        submitted = self.start()
        self.finish(submitted)
        ref = self.running_ref(submitted["session_id"])
        response = self.client.delete(f"{AGENT}/sessions/{submitted['session_id']}?user_id={self.user['id']}", headers=AUTH)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "session_busy")
        STORE.fail_runs([ref.id], "test cleanup")
        response = self.client.delete(f"{AGENT}/sessions/{submitted['session_id']}?user_id={self.user['id']}", headers=AUTH)
        self.assertEqual(response.status_code, 204)

    def test_delete_run_keeps_the_session_and_other_runs(self) -> None:
        first = self.start()
        self.finish(first)
        second = self.client.post(
            f"{AGENT}/sessions/{first['session_id']}/runs",
            json={"user_id": self.user["id"], "message": "第二轮"},
            headers=AUTH,
        ).json()
        self.finish(second)
        base = f"{AGENT}/sessions/{first['session_id']}"
        self.assertEqual(self.client.delete(f"{base}/runs/{first['run_id']}", headers=AUTH).status_code, 204)
        runs = self.client.get(f"{base}/runs", headers=AUTH).json()
        self.assertEqual([run["id"] for run in runs], [second["run_id"]])
        self.assertEqual(self.client.get(f"{base}/runs/{first['run_id']}/history", headers=AUTH).status_code, 404)
        self.assertEqual(self.client.get(base, headers=AUTH).status_code, 200)
        live = _rows("SELECT count(*) FROM run_events WHERE run_id = %s AND deleted_at IS NULL", (first["run_id"],))
        self.assertEqual(live[0][0], 0)

        ref = self.running_ref(first["session_id"])
        response = self.client.delete(f"{base}/runs/{ref.id}", headers=AUTH)
        self.assertEqual((response.status_code, response.json()["code"]), (409, "run_busy"))
        STORE.fail_runs([ref.id], "test cleanup")

    def test_user_route_deletes_a_session(self) -> None:
        submitted = self.start()
        self.finish(submitted)
        path = f"/api/v1/users/{self.user['id']}/sessions/{submitted['session_id']}"
        self.assertEqual(self.client.delete(path, headers=AUTH).status_code, 204)
        self.assertEqual(self.client.delete(path, headers=AUTH).status_code, 404)
        self.assertEqual(self.client.get(f"{AGENT}/sessions/{submitted['session_id']}", headers=AUTH).status_code, 404)

    def test_delete_user_deletes_the_sessions_under_it(self) -> None:
        submitted = self.start()
        self.finish(submitted)
        self.assertEqual(self.client.delete(f"/api/v1/users/{self.user['id']}", headers=AUTH).status_code, 204)
        session = STORE.sessions[submitted["session_id"]]
        self.assertTrue(session.deleted)
        self.assertTrue(STORE.workspaces[session.workspace_id].deleted)
        self.assertTrue(STORE.runs[submitted["run_id"]].deleted)
        self.assertEqual(STORE.users[self.user["id"]].deleted_at, session.deleted_at)

    def test_runs_left_running_by_a_previous_process_are_closed(self) -> None:
        from viknow3.access.agent.service import close_lost_runs

        submitted = self.start()
        self.finish(submitted)
        ref = self.running_ref(submitted["session_id"])
        self.assertIn(ref.id, close_lost_runs())
        self.assertEqual(STORE.runs[ref.id].status, "failed")
        history = self.client.get(f"{AGENT}/sessions/{submitted['session_id']}/runs/{ref.id}/history", headers=AUTH).json()
        self.assertEqual([item["type"] for item in history], ["run_failed"])
        self.assertEqual(history[0]["payload"]["code"], "run_lost")

    def test_nested_interrupt_returns_the_run_and_session(self) -> None:
        submitted = self.start()
        self.finish(submitted)
        base = f"{AGENT}/sessions/{submitted['session_id']}/runs/{submitted['run_id']}"
        response = self.client.post(f"{base}/interrupt", headers=AUTH)
        self.assertEqual((response.status_code, response.json()["code"]), (409, "run_not_running"))


if __name__ == "__main__":
    unittest.main()
