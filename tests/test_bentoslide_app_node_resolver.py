from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = os.name == "nt"


@unittest.skipUnless(WINDOWS, "Windows-only Node resolver tests")
class BentoSlideAppNodeResolverTests(unittest.TestCase):
    def test_common_launcher_decodes_json_body_as_utf8(self) -> None:
        expected_repository = r"C:\日本語 空白\Bento 論文"
        payload = json.dumps(
            {
                "format": "bento/application-api-health/v1",
                "repository": expected_repository,
            },
            ensure_ascii=False,
        ).encode("utf-8")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format: str, *args: object) -> None:
                del args

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            common = ROOT / "scripts" / "bento_editor_launcher.common.ps1"
            with tempfile.TemporaryDirectory() as temporary:
                result_path = Path(temporary) / "repository.txt"
                command = (
                    f". '{common}'; "
                    f"$result = Invoke-BentoUtf8JsonRequest -Uri 'http://127.0.0.1:{server.server_port}/health'; "
                    f"[System.IO.File]::WriteAllText('{result_path}', [string]$result.repository, "
                    "(New-Object System.Text.UTF8Encoding($false)))"
                )
                completed = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                self.assertEqual(result_path.read_text(encoding="utf-8"), expected_repository)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_supported_versions_match_frontend_engine_contract(self) -> None:
        resolver = ROOT / "scripts" / "resolve_bentoslide_app_node.ps1"
        versions = [
            "20.19.0", "22.12.0", "22.22.1", "22.22.2", "24.14.9", "24.15.0", "25.0.0", "26.0.0",
        ]
        command = (
            f". '{resolver}'; "
            "$versions = @('" + "','".join(versions) + "'); "
            "$result = @{}; "
            "foreach ($version in $versions) { $result[$version] = Test-BentoSlideAppNodeVersion -VersionText $version }; "
            "$result | ConvertTo-Json -Compress"
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(
            result,
            {
                "20.19.0": False,
                "22.12.0": False,
                "22.22.1": False,
                "22.22.2": True,
                "24.14.9": False,
                "24.15.0": True,
                "25.0.0": False,
                "26.0.0": True,
            },
        )

        package = json.loads((ROOT / "app/frontend/package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["engines"]["node"], "^22.22.2 || ^24.15.0 || >=26.0.0")

    def test_portable_npm_can_find_node_without_leaking_path_changes(self) -> None:
        resolver = ROOT / "scripts" / "resolve_bentoslide_app_node.ps1"
        with tempfile.TemporaryDirectory() as temporary:
            tools = Path(temporary)
            node = tools / "node.cmd"
            npm = tools / "npm.cmd"
            node.write_text("@echo off\r\nexit /b 0\r\n", encoding="ascii")
            npm.write_text("@echo off\r\nnode --version >nul\r\nexit /b %errorlevel%\r\n", encoding="ascii")
            command = (
                f". '{resolver}'; "
                f"$resolution = [pscustomobject]@{{ Node = '{node}'; Npm = '{npm}'; Source = 'fixture' }}; "
                "$before = $env:Path; "
                "$exitCode = Invoke-BentoSlideAppNpm -NodeResolution $resolution -Arguments @('--probe'); "
                "[pscustomobject]@{ ExitCode = $exitCode; PathRestored = ($env:Path -eq $before) } | ConvertTo-Json -Compress"
            )
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"ExitCode": 0, "PathRestored": True})
