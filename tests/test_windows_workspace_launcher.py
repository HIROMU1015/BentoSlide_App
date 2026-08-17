from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import yaml
from fastapi.testclient import TestClient

from app.backend.main import create_app
from bento_converter.html_document import embed_bento_doc, extract_bento_doc, load_html
from tests.finalization_fixture import prepare_authoring_fixture, prepare_finalization_fixture


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = os.name == "nt"


@unittest.skipUnless(WINDOWS, "Windows-only workspace launcher tests")
class WindowsWorkspaceLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repositories: list[Path] = []

    def tearDown(self) -> None:
        for repository in self.repositories:
            if (repository / "output/html-preview-session.json").is_file():
                self.run_powershell(repository / "scripts/stop_html_preview.ps1", timeout=20)
            if (repository / "output/work-editor-session.json").is_file():
                self.run_powershell(repository / "scripts/stop_bento_editor.ps1", timeout=20)
        self.temporary.cleanup()

    def copy_repository(self, name: str) -> Path:
        repository = self.root / name
        repository.mkdir(parents=True)
        for directory in ("bento_converter", "scripts", "workflow"):
            shutil.copytree(ROOT / directory, repository / directory, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(ROOT / "tests/fixtures/deck_v1.yaml", repository / "deck.yaml")
        for filename in (
            "REQUEST.md", "demo.bento.html", "start_html_preview.cmd", "stop_html_preview.cmd",
            "start_deck_workspace.cmd", "stop_deck_workspace.cmd", "start_bento_editor.cmd", "stop_bento_editor.cmd",
        ):
            shutil.copy2(ROOT / filename, repository / filename)
        (repository / "chapters").mkdir()
        (repository / "planning").mkdir()
        (repository / "output/diagnostics").mkdir(parents=True)
        shutil.copy2(repository / "demo.bento.html", repository / "output/presentation.generated.bento.html")
        (repository / "output/diagnostics/merged-registry.json").write_text("{}\n", encoding="utf-8")
        self.repositories.append(repository)
        return repository

    @staticmethod
    def free_port() -> int:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    def set_stage(self, repository: Path, stage: str, *, html_port: int | None = None, bento_port: int | None = None) -> dict:
        path = repository / "deck.yaml"
        state = yaml.safe_load(path.read_text(encoding="utf-8"))
        owners = {
            "initialized": ("work", "sources", "pending"), "planning": ("work", "planning", "in_progress"),
            "awaiting_plan_approval": ("work", "planning", "awaiting_approval"),
            "html_authoring": ("work", "chapters", "in_progress"), "html_review": ("work", "chapters", "awaiting_approval"),
            "ready_for_conversion": ("codex", "chapters", "ready"), "converting": ("codex", "chapters", "in_progress"),
            "bento_validation": ("codex", "generated", "in_progress"), "bento_finalization": ("work", "final", "in_progress"),
            "complete": ("codex", "final", "complete"),
        }
        owner, source, status = owners[stage]
        state["workflow"].update(stage=stage, owner=owner, sourceOfTruth=source, status=status, currentChapter=None)
        if html_port is not None:
            state["preview"]["htmlPort"] = html_port
        if bento_port is not None:
            state["preview"]["bentoPort"] = bento_port
        path.write_text(yaml.safe_dump(state, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return state

    def prepare_preview_chapter(self, repository: Path, port: int) -> None:
        state = self.set_stage(repository, "html_review", html_port=port)
        state["chapters"] = {
            "chapter-01": {
                "html": "chapters/chapter-01.preview.html", "registry": "chapters/chapter-01.registry.json",
                "status": "review", "visualApproval": "pending",
            }
        }
        state["workflow"]["currentChapter"] = "chapter-01"
        (repository / "chapters/chapter-01.preview.html").write_text(
            '<!doctype html><section data-slide-id="slide-1"><h1 data-bento-id="title">Preview 日本語</h1></section>', encoding="utf-8"
        )
        (repository / "chapters/chapter-01.registry.json").write_text(
            json.dumps({"format": "bento/html-registry/v1", "chapterId": "chapter-01"}), encoding="utf-8"
        )
        (repository / "deck.yaml").write_text(yaml.safe_dump(state, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def run_without_pipes(self, command: list[str], *, timeout: int = 50) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryFile() as capture:
            result = subprocess.run(
                command, cwd=self.root, stdout=capture, stderr=subprocess.STDOUT, timeout=timeout,
                env={**os.environ, "BENTO_EDITOR_NO_PAUSE": "1"},
            )
            capture.seek(0)
            output = capture.read().decode("utf-8", errors="replace")
        return subprocess.CompletedProcess(result.args, result.returncode, output, "")

    def run_cmd(self, path: Path, *arguments: str, timeout: int = 50) -> subprocess.CompletedProcess[str]:
        return self.run_without_pipes(["cmd.exe", "/d", "/c", str(path), *map(str, arguments)], timeout=timeout)

    def run_powershell(self, path: Path, *arguments: str, timeout: int = 50) -> subprocess.CompletedProcess[str]:
        return self.run_without_pipes(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path), *map(str, arguments)], timeout=timeout,
        )

    @staticmethod
    def wait_status(port: int, *, timeout: float = 10) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1) as response:
                    return json.loads(response.read().decode("utf-8"))
            except (OSError, URLError, json.JSONDecodeError):
                time.sleep(0.1)
        raise AssertionError(f"No status response on port {port}")

    @staticmethod
    def wait_lifecycle(
        client: TestClient, action: str, *, timeout: float = 120,
    ) -> dict:
        deadline = time.monotonic() + timeout
        last: dict = {}
        while time.monotonic() < deadline:
            response = client.get("/api/bento/lifecycle/status")
            if response.status_code == 200:
                last = response.json()
                if last.get("action") == action and last.get("status") in {"succeeded", "failed"}:
                    return last
            time.sleep(0.1)
        raise AssertionError(f"Lifecycle action {action} did not finish: {last}")

    def test_preview_cmd_start_duplicate_traversal_stop(self) -> None:
        repository = self.copy_repository("Bento Preview")
        port = self.free_port()
        self.prepare_preview_chapter(repository, port)
        started = self.run_cmd(repository / "start_html_preview.cmd", "-NoClipboard")
        self.assertEqual(started.returncode, 0, started.stdout)
        status = self.wait_status(port)
        self.assertEqual(status["format"], "bento/html-preview-status/v1")
        session = json.loads((repository / "output/html-preview-session.json").read_text(encoding="utf-8-sig"))
        first_pid = session["pid"]
        self.assertEqual(session["launchMode"], "wmi-detached")
        parent_name = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-Command",
                f"$p=Get-CimInstance Win32_Process -Filter \"ProcessId = {first_pid}\"; "
                "$parent=Get-CimInstance Win32_Process -Filter (\"ProcessId = {0}\" -f $p.ParentProcessId); "
                "$parent.Name",
            ],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(parent_name.lower(), "wmiprvse.exe")
        self.assertIn(
            "BentoSlide HTML preview:",
            (repository / "output/html-preview.stdout.log").read_text(encoding="utf-8-sig"),
        )
        self.assertEqual(status["currentChapter"], "chapter-01")
        duplicate = self.run_cmd(repository / "start_html_preview.cmd", "-NoClipboard")
        self.assertEqual(duplicate.returncode, 0, duplicate.stdout)
        self.assertEqual(json.loads((repository / "output/html-preview-session.json").read_text(encoding="utf-8-sig"))["pid"], first_pid)
        original_session = (repository / "output/html-preview-session.json").read_bytes()
        mismatched = json.loads(original_session.decode("utf-8-sig"))
        mismatched["processStartTimeUtc"] = "2000-01-01T00:00:00Z"
        (repository / "output/html-preview-session.json").write_text(json.dumps(mismatched), encoding="utf-8")
        mismatched_session = (repository / "output/html-preview-session.json").read_bytes()
        refused = self.run_cmd(repository / "start_html_preview.cmd", "-NoClipboard")
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual((repository / "output/html-preview-session.json").read_bytes(), mismatched_session)
        self.assertEqual((repository / "output/html-preview.pid").read_text(encoding="ascii").strip(), str(first_pid))
        self.assertEqual(self.wait_status(port)["currentChapter"], "chapter-01")
        (repository / "output/html-preview-session.json").write_bytes(original_session)
        with self.assertRaises(HTTPError) as traversal:
            urlopen(f"http://127.0.0.1:{port}/chapters/%2e%2e/deck.yaml", timeout=2)
        self.assertEqual(traversal.exception.code, 404)
        stopped = self.run_cmd(repository / "stop_html_preview.cmd")
        self.assertEqual(stopped.returncode, 0, stopped.stdout)
        self.assertFalse((repository / "output/html-preview-session.json").exists())
        self.assertIsNone(yaml.safe_load((repository / "deck.yaml").read_text(encoding="utf-8"))["preview"]["currentUrl"])
        self.assertIsNone(subprocess.run(["powershell.exe", "-NoProfile", "-Command", f"Get-Process -Id {first_pid} -ErrorAction SilentlyContinue"], capture_output=True).stdout or None)
        self.assertEqual(self.run_cmd(repository / "stop_html_preview.cmd").returncode, 0)

    def test_preview_port_conflict_does_not_stop_unrelated_server(self) -> None:
        repository = self.copy_repository("Bento Port")
        port = self.free_port()
        self.prepare_preview_chapter(repository, port)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(200); self.end_headers(); self.wfile.write(b"unrelated")

            def log_message(self, format: str, *args) -> None:  # noqa: A002
                return

        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = self.run_cmd(repository / "start_html_preview.cmd", "-NoClipboard")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("4174", result.stdout)
            self.assertTrue(thread.is_alive())
            with urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                self.assertEqual(response.read(), b"unrelated")
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_initialized_workspace_message_accepts_general_source_materials(self) -> None:
        repository = self.copy_repository("Bento Initialized")
        result = self.run_cmd(repository / "start_deck_workspace.cmd", "-NoClipboard")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("source materials under sources/private/", result.stdout)
        self.assertIn("tell Work what deck you want", result.stdout)
        self.assertNotIn("source manifest", result.stdout)
        self.assertNotIn("primary PDF", result.stdout)

    def test_stage_workspace_launcher_supports_spaced_japanese_path(self) -> None:
        repository = self.copy_repository("Bento Workspace 論文 (Draft)")
        port = self.free_port()
        self.prepare_preview_chapter(repository, port)
        started = self.run_cmd(repository / "start_deck_workspace.cmd", "-NoClipboard")
        self.assertEqual(started.returncode, 0, started.stdout)
        self.assertEqual(self.wait_status(port)["currentPath"], "chapters/chapter-01.preview.html")
        stopped = self.run_cmd(repository / "stop_deck_workspace.cmd")
        self.assertEqual(stopped.returncode, 0, stopped.stdout)
        self.set_stage(repository, "ready_for_conversion", html_port=port)
        source = repository / "output/presentation.generated.bento.html"
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        no_server = self.run_cmd(repository / "start_deck_workspace.cmd", "-NoClipboard")
        self.assertEqual(no_server.returncode, 0, no_server.stdout)
        self.assertFalse((repository / "output/html-preview-session.json").exists())
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_hash)

    def test_bento_finalization_dispatch_preserves_generated_and_existing_final(self) -> None:
        repository = self.copy_repository("Bento Finalization")
        port = self.free_port()
        state = self.set_stage(repository, "bento_finalization", bento_port=port)
        source = repository / "custom artifacts/生成/deck.generated.bento.html"
        source.parent.mkdir(parents=True)
        shutil.copy2(repository / "demo.bento.html", source)
        target = repository / "custom artifacts/最終/deck.final.bento.html"
        target.parent.mkdir(parents=True)
        registry = source.parent / "diagnostics/merged-registry.json"
        registry.parent.mkdir(parents=True)
        registry.write_text("{}\n", encoding="utf-8")
        state["outputs"].update({
            "generatedHtml": "custom artifacts/生成/deck.generated.bento.html",
            "generatedJson": "custom artifacts/生成/deck.generated.bento.json",
            "finalHtml": "custom artifacts/最終/deck.final.bento.html",
            "finalJson": "custom artifacts/最終/deck.final.bento.json",
        })
        (repository / "deck.yaml").write_text(yaml.safe_dump(state, allow_unicode=True, sort_keys=False), encoding="utf-8")
        source_html = load_html(source)
        document = extract_bento_doc(source_html)
        next(element for slide in document["slides"] for element in slide["elements"] if element["type"] == "shape")["x"] += 11
        target.write_text(embed_bento_doc(source_html, document), encoding="utf-8")
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        target_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        started = self.run_cmd(repository / "start_deck_workspace.cmd", "-NoClipboard")
        self.assertEqual(started.returncode, 0, started.stdout)
        status = self.wait_status(port)
        self.assertEqual(status["target"], "custom artifacts/最終/deck.final.bento.html")
        session = json.loads((repository / "output/work-editor-session.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(Path(session["source"]), source.resolve())
        self.assertEqual(Path(session["target"]), target.resolve())
        self.assertEqual(Path(session["registry"]), registry.resolve())
        command_line = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", f"(Get-CimInstance Win32_Process -Filter \"ProcessId = {session['pid']}\").CommandLine"],
            text=True, capture_output=True, encoding="utf-8", errors="replace", check=True,
        ).stdout
        self.assertNotIn("--reset-final", command_line)
        self.assertNotIn("--allow-content-edit", command_line)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_hash)
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), target_hash)
        stopped = self.run_cmd(repository / "stop_deck_workspace.cmd")
        self.assertEqual(stopped.returncode, 0, stopped.stdout)

    def test_v2_finalization_fixture_satisfies_stage_guard(self) -> None:
        repository = self.copy_repository("Bento Finalization 日本語 path")
        port = self.free_port()
        state = prepare_finalization_fixture(
            repository, bento_port=port, confirm_disposable=True,
        )
        outputs = state["outputs"]
        protected_paths = [repository / outputs[field] for field in (
            "generatedHtml", "authoringHtml", "finalHtml", "finalRegistry",
        )]
        protected_hashes = {
            path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected_paths
        }
        arguments = (
            "-Mode", "finalization",
            "-Source", outputs["authoringHtml"],
            "-Target", outputs["finalHtml"],
            "-Registry", outputs["finalRegistry"],
            "-Port", str(port),
            "-NoClipboard",
        )

        started = self.run_cmd(repository / "start_bento_editor.cmd", *arguments)
        self.assertEqual(started.returncode, 0, started.stdout)
        status = self.wait_status(port)
        self.assertEqual(status["editingMode"], "finalization")
        self.assertEqual(status["target"], outputs["finalHtml"])
        first_pid = json.loads(
            (repository / "output/work-editor-session.json").read_text(encoding="utf-8-sig")
        )["pid"]
        duplicate = self.run_cmd(repository / "start_bento_editor.cmd", *arguments)
        self.assertEqual(duplicate.returncode, 0, duplicate.stdout)
        self.assertEqual(json.loads(
            (repository / "output/work-editor-session.json").read_text(encoding="utf-8-sig")
        )["pid"], first_pid)
        stopped = self.run_cmd(repository / "stop_bento_editor.cmd")
        self.assertEqual(stopped.returncode, 0, stopped.stdout)
        self.assertEqual(
            {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected_paths},
            protected_hashes,
        )

    def test_app_api_runs_authoring_review_finalization_complete_and_reopen(self) -> None:
        repository = self.copy_repository("Bento App Lifecycle 日本語 path")
        port = self.free_port()
        prepare_authoring_fixture(
            repository, bento_port=port, confirm_disposable=True,
        )
        started = self.run_cmd(repository / "start_deck_workspace.cmd", "-NoClipboard")
        self.assertEqual(started.returncode, 0, started.stdout)
        self.assertEqual(self.wait_status(port)["editingMode"], "authoring")

        with TestClient(create_app(repository, frontend_dist=repository / "missing-frontend")) as client:
            review = client.post("/api/bento/content/review", json={"confirmed": True})
            self.assertEqual(review.status_code, 202, review.text)
            review_status = self.wait_lifecycle(client, "content-review")
            self.assertEqual(review_status["status"], "succeeded", review_status)
            self.assertEqual(review_status["stage"], "content_review")
            self.assertEqual(self.wait_status(port)["editingMode"], "authoring")

            content = client.post("/api/bento/content/approve", json={"confirmed": True})
            self.assertEqual(content.status_code, 202, content.text)
            content_status = self.wait_lifecycle(client, "content-approve")
            self.assertEqual(content_status["status"], "succeeded", content_status)
            self.assertEqual(content_status["stage"], "bento_finalization")
            self.assertEqual(self.wait_status(port)["editingMode"], "finalization")

            final = client.post("/api/bento/final/approve", json={"confirmed": True})
            self.assertEqual(final.status_code, 202, final.text)
            final_status = self.wait_lifecycle(client, "final-approve")
            self.assertEqual(final_status["status"], "succeeded", final_status)
            self.assertEqual(final_status["stage"], "complete")
            self.assertFalse((repository / "output/work-editor-session.json").exists())
            self.assertFalse((repository / "output/work-editor.pid").exists())

            reopen = client.post("/api/bento/final/reopen", json={"confirmed": True})
            self.assertEqual(reopen.status_code, 202, reopen.text)
            reopen_status = self.wait_lifecycle(client, "final-reopen")
            self.assertEqual(reopen_status["status"], "succeeded", reopen_status)
            self.assertEqual(reopen_status["stage"], "bento_finalization")
            self.assertEqual(self.wait_status(port)["editingMode"], "finalization")

    def test_bento_authoring_dispatch_uses_v2_custom_artifact_paths(self) -> None:
        repository = self.copy_repository("Bento Authoring 日本語")
        port = self.free_port()
        state = yaml.safe_load((repository / "deck.yaml").read_text(encoding="utf-8"))
        generated = repository / "custom artifacts/生成/deck.generated.bento.html"
        generated.parent.mkdir(parents=True)
        shutil.copy2(repository / "demo.bento.html", generated)
        generated_registry = repository / "custom artifacts/生成/deck.generated.registry.json"
        generated_registry.write_text(json.dumps({
            "format": "bento/html-registry/v2", "unitId": "deck", "sources": {},
            "document": {}, "assets": {}, "fonts": {},
            "equations": {"hamiltonian_split": {"latex": "H = H_0 + \\alpha H_1"}},
            "figures": {}, "tables": {}, "charts": {},
            "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
        }), encoding="utf-8")
        state.update({
            "schemaVersion": 2,
            "project": {"kind": "paper_explanation", **state["project"]},
            "sources": {"manifest": "sources/source-manifest.yaml", "authorityMode": "single"},
            "authoring": {"mode": "modular", "entryHtml": None, "registry": None, "currentSection": None},
            "sections": {},
            "migration": {"fromSchemaVersion": 1, "migratedAt": "2026-08-05T00:00:00Z", "lateStageCompatibility": False},
        })
        state["workflow"].update(
            stage="bento_authoring", status="in_progress", owner="work", sourceOfTruth="authoring",
            currentChapter=None, currentSection=None,
        )
        state["approvals"]["bentoContent"] = {
            "status": "pending", "documentRevision": None, "registryRevision": None,
            "approvalDigest": None, "approvedAt": None,
        }
        state["handoff"].update(
            readyForBentoAuthoring=True, readyForContentReview=False,
        )
        state["outputs"] = {
            "generatedHtml": "custom artifacts/生成/deck.generated.bento.html",
            "generatedJson": "custom artifacts/生成/deck.generated.bento.json",
            "generatedRegistry": "custom artifacts/生成/deck.generated.registry.json",
            "authoringHtml": "custom artifacts/編集中/deck.authoring.bento.html",
            "authoringJson": "custom artifacts/編集中/deck.authoring.bento.json",
            "authoringRegistry": "custom artifacts/編集中/deck.authoring.registry.json",
            "finalHtml": "custom artifacts/最終/deck.final.bento.html",
            "finalJson": "custom artifacts/最終/deck.final.bento.json",
            "finalRegistry": "custom artifacts/最終/deck.final.registry.json",
        }
        state["preview"]["bentoPort"] = port
        (repository / "deck.yaml").write_text(yaml.safe_dump(state, allow_unicode=True, sort_keys=False), encoding="utf-8")

        started = self.run_cmd(repository / "start_deck_workspace.cmd", "-NoClipboard")
        self.assertEqual(started.returncode, 0, started.stdout)
        status = self.wait_status(port)
        self.assertEqual(status["editingMode"], "authoring")
        self.assertEqual(status["target"], "custom artifacts/編集中/deck.authoring.bento.html")
        self.assertEqual(Path(status["repository"]), repository.resolve())
        self.assertIn("documentRevision", status)
        self.assertIn("registryRevision", status)
        session = json.loads((repository / "output/work-editor-session.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(session["mode"], "authoring")
        self.assertEqual(Path(session["sourceRegistry"]), generated_registry.resolve())
        self.assertEqual(Path(session["targetRegistry"]), (repository / state["outputs"]["authoringRegistry"]).resolve())
        duplicate = self.run_cmd(repository / "start_deck_workspace.cmd", "-NoClipboard")
        self.assertEqual(duplicate.returncode, 0, duplicate.stdout)
        self.assertEqual(
            json.loads((repository / "output/work-editor-session.json").read_text(encoding="utf-8-sig"))["pid"],
            session["pid"],
        )
        stopped = self.run_cmd(repository / "stop_deck_workspace.cmd")
        self.assertEqual(stopped.returncode, 0, stopped.stdout)

    def test_stop_refuses_reused_or_mismatched_pid_and_cleans_missing_pid(self) -> None:
        repository = self.copy_repository("Bento Preview Stale")
        port = self.free_port()
        self.prepare_preview_chapter(repository, port)
        actual_start = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", f"(Get-Process -Id {os.getpid()}).StartTime.ToUniversalTime().ToString('o')"],
            text=True, capture_output=True, encoding="utf-8", errors="replace", check=True,
        ).stdout.strip()
        session = {
            "format": "bento/html-preview-session/v1", "pid": os.getpid(), "startedAt": "2026-01-01T00:00:00Z",
            "processStartTimeUtc": actual_start, "repository": str(repository.resolve()), "python": os.path.abspath(__file__),
            "host": "127.0.0.1", "port": port, "url": f"http://127.0.0.1:{port}/",
        }
        (repository / "output/html-preview-session.json").write_text(json.dumps(session), encoding="utf-8")
        (repository / "output/html-preview.pid").write_text(str(os.getpid()), encoding="ascii")
        refused = self.run_cmd(repository / "stop_html_preview.cmd")
        self.assertNotEqual(refused.returncode, 0)
        self.assertTrue((repository / "output/html-preview-session.json").exists())
        missing_pid = 2_000_000_000
        session["pid"] = missing_pid
        (repository / "output/html-preview-session.json").write_text(json.dumps(session), encoding="utf-8")
        (repository / "output/html-preview.pid").write_text(str(missing_pid), encoding="ascii")
        cleaned = self.run_cmd(repository / "stop_html_preview.cmd")
        self.assertEqual(cleaned.returncode, 0, cleaned.stdout)
        self.assertFalse((repository / "output/html-preview-session.json").exists())


if __name__ == "__main__":
    unittest.main()
