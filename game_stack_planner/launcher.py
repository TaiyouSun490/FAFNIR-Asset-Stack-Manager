"""Local UI launcher."""

from __future__ import annotations

import threading
import webbrowser

from .api import GameStackApplication
from .server import create_server


def run_local_ui(
    *,
    port: int = 8770,
    db_path: str | None = None,
    open_browser: bool = True,
) -> None:
    application = GameStackApplication(db_path)
    application.enable_automatic_maintenance(sync_my_assets=db_path is None)
    server = create_server(application, port=port)
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}/"
    print(f"Fafnir Asset Stack Manager: {url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        application.close()


__all__ = ["run_local_ui"]
