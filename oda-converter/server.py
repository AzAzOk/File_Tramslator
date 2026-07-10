"""HTTP API wrapper for ODAFileConverter.

Runs inside the oda-converter Docker container.
Accepts conversion requests via HTTP POST.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

from converter import convert, is_available

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("oda-server")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5000"))


class OdaRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._json_response(200, {
                "status": "ok",
                "available": is_available(),
            })
        else:
            self._json_response(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/convert":
            logger.warning("Unknown POST endpoint: %s", self.path)
            self._json_response(404, {"error": "not found"})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            logger.warning("POST /convert with empty body")
            self._json_response(400, {"error": "empty request"})
            return

        try:
            body = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, Exception) as e:
            logger.error("Failed to parse request body: %s", str(e))
            self._json_response(400, {"error": f"Invalid JSON: {str(e)}"})
            return

        input_rel = body.get("input_path", "")
        output_rel = body.get("output_path", "")
        output_format = body.get("output_format", "DXF")
        version = body.get("version", "ACAD2018")

        logger.info(
            "Received convert request: %s -> %s (format=%s, version=%s)",
            input_rel, output_rel, output_format, version,
        )

        input_path = DATA_DIR / input_rel
        output_path = DATA_DIR / output_rel

        if not input_path.exists():
            logger.error("Input file not found: %s (expected at %s)", input_rel, input_path)
            self._json_response(404, {"error": f"input not found: {input_path}"})
            return

        logger.info(
            "Starting conversion: %s -> %s", input_path.name, output_path.name
        )

        try:
            success = convert(input_path, output_path, output_format, version)
            if success:
                logger.info(
                    "Conversion successful: %s -> %s", input_path.name, output_path.name
                )
                self._json_response(200, {
                    "success": True,
                    "output_path": str(output_path),
                })
            else:
                logger.error(
                    "Conversion failed (return code != 0): %s -> %s", input_path.name, output_path.name
                )
                self._json_response(500, {"error": "conversion failed"})
        except Exception as e:
            logger.exception(
                "Conversion exception: %s -> %s (%s)", input_path.name, output_path.name, str(e)
            )
            self._json_response(500, {"error": f"Exception during conversion: {str(e)}"})

    def _json_response(self, status_code: int, data: dict):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        logger.info(format, *args)


def main():
    logger.info("Starting ODA converter server on %s:%s", HOST, PORT)
    logger.info("ODAFileConverter available: %s", is_available())
    server = HTTPServer((HOST, PORT), OdaRequestHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
