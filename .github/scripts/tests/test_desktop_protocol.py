"""Exercise the desktop startup protocol with a Windows-like stdout encoding."""

import contextlib
import importlib.util
import io
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
APP_NAME = "企业知识库文档整理与问答系统"


class DesktopProtocolTests(unittest.TestCase):
    def test_ready_message_survives_cp1252_stdout_and_preserves_app_name(self):
        # The packaged server writes to a pipe. Simulate the Western Windows
        # code page that caused the actual build failure, using the real entry
        # point with only its server and process guards replaced.
        package = types.ModuleType("_desktop_protocol_test")
        package.__path__ = [str(ROOT / "backend" / "app")]
        config = types.ModuleType("_desktop_protocol_test.config")
        config.DATA_DIR = "."
        app_main = types.ModuleType("_desktop_protocol_test.main")
        app_main.app = object()
        app_main.APP_NAME = APP_NAME
        app_main.VERSION = "1.0.22"
        services = types.ModuleType("_desktop_protocol_test.services")
        services.procguard = types.SimpleNamespace(
            acquire_data_lock=lambda *args, **kwargs: {"pid": 1, "port": 12345},
            release_data_lock=lambda: None,
            watch_parent=lambda *args: None,
        )

        class FakeServer:
            started = True

            def __init__(self, config):
                pass

            def run(self):
                pass

        class ImmediateThread:
            def __init__(self, target, daemon=False):
                self.target = target
                self.daemon = daemon

            def start(self):
                self.target()

        uvicorn = types.ModuleType("uvicorn")
        uvicorn.Server = FakeServer
        uvicorn.Config = lambda *args, **kwargs: object()
        modules = {
            "uvicorn": uvicorn,
            package.__name__: package,
            config.__name__: config,
            app_main.__name__: app_main,
            services.__name__: services,
        }
        spec = importlib.util.spec_from_file_location(
            package.__name__ + ".desktop_entry", ROOT / "backend" / "app" / "desktop_entry.py"
        )
        entry = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules), patch.object(sys, "argv", ["desktop_entry"]):
            spec.loader.exec_module(entry)
            buffer = io.BytesIO()
            stdout = io.TextIOWrapper(buffer, encoding="cp1252", errors="strict")
            with patch.dict(os.environ, {"WRK_WATCH_STDIN": "0", "WRK_PORT": "0"}), \
                    patch.object(entry, "_pick_port", return_value=12345), \
                    patch.object(entry, "_parent_pids", return_value=[]), \
                    patch.object(entry.threading, "Thread", ImmediateThread), \
                    patch.object(entry.signal, "signal"), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(entry.main(), 0)
                stdout.flush()
            line = buffer.getvalue().decode("cp1252").strip()
            self.assertTrue(line.startswith("WRK_READY "))
            message = json.loads(line.removeprefix("WRK_READY "))
            self.assertEqual(message["app"], APP_NAME)
            self.assertEqual(message["port"], 12345)


if __name__ == "__main__":
    unittest.main()
