"""Isolated localhost UI fixture; no user's catalog, bridge or automatic workers."""
import tempfile
from pathlib import Path
from game_stack_planner.api import GameStackApplication
from game_stack_planner.repository import StackRepository
from game_stack_planner.server import create_server

with tempfile.TemporaryDirectory(prefix="fafnir-review-ui-") as folder:
    app = GameStackApplication(repository=StackRepository(Path(folder) / "fixture.db"))
    server = create_server(app, port=0)
    print(f"http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        app.close()
