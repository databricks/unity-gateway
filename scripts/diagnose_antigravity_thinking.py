"""Capture native request metadata; deliberately never return successful inference."""

import json
import os
import runpy
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class CaptureHandler(BaseHTTPRequestHandler):
    captured = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        metadata = {"path": self.path, "fields": sorted(payload)}
        for config_key in ("generationConfig", "generation_config"):
            config = payload.get(config_key)
            if isinstance(config, dict):
                metadata[config_key] = {"fields": sorted(config)}
                for thinking_key in ("thinkingConfig", "thinking_config"):
                    if thinking_key in config:
                        metadata[config_key][thinking_key] = config[thinking_key]
        self.captured.append(metadata)
        print(json.dumps(metadata), flush=True)
        response = json.dumps(
            {
                "error": {
                    "code": 400,
                    "message": "Diagnostic captured request; no inference performed.",
                }
            }
        ).encode()
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


def main():
    process = runpy.run_path(
        str(
            Path(__file__).resolve().parents[1]
            / "src/ucode/os_compatibility/subprocess_cross_os.py"
        )
    )
    installer = runpy.run_path(str(Path(__file__).with_name("run_integration.py")))
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        installed = installer["install_native_agent"](
            "antigravity", "1.5.4-5037094733742080", root / "native"
        )
        binary = installed["path"]
        server = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for model in ("gemini-4-carbon", "gemini-3.8-flash"):
                home = root / model / "home"
                workdir = root / model / "work"
                home.mkdir(parents=True)
                workdir.mkdir()
                environment = {
                    "PATH": os.defpath,
                    "HOME": str(home),
                    "TERM": "dumb",
                    "AGY_CLI_DISABLE_AUTO_UPDATE": "true",
                    "AGY_LLM_GATEWAY_URL": f"http://127.0.0.1:{server.server_port}/ai-gateway/gemini",
                    "AGY_LLM_GATEWAY_API_KEY": "capture-only-not-a-credential",
                    "AGY_LLM_GATEWAY_WIRE_PROTOCOL": "genai",
                    "AGY_LLM_GATEWAY_MODELS": "gemini-4-carbon,gemini-3.8-flash",
                }
                count_before = len(CaptureHandler.captured)
                result = process["run"](
                    [
                        binary,
                        "--model",
                        model,
                        "-p",
                        "Say hello.",
                        "--output-format",
                        "stream-json",
                        "--print-timeout",
                        "45s",
                    ],
                    env=environment,
                    cwd=workdir,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=60,
                )
                print(
                    json.dumps(
                        {
                            "model": model,
                            "exit_code": result.returncode,
                            "requests": len(CaptureHandler.captured) - count_before,
                        }
                    ),
                    flush=True,
                )
                if len(CaptureHandler.captured) == count_before:
                    raise RuntimeError(f"No native request captured for {model}")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    main()
