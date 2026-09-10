"""Opt-in distribution check: python tests/check_bridge_wheel.py path/to/release.whl.

Uses installed dependencies, but imports Fafnir only from the extracted wheel.
Never changes an installed Fafnir or a user's Unity project.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


def main():
    wheel = Path(sys.argv[1]).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="fafnir-wheel-check-") as directory:
        root = Path(directory).resolve()
        installed = root / "installed"
        installed.mkdir()
        with zipfile.ZipFile(wheel) as archive:
            for member in archive.namelist():
                if not (installed / member).resolve().is_relative_to(installed):
                    raise RuntimeError("Unsafe wheel member")
            archive.extractall(installed)
        project = root / "Consumer Game 日本語"
        for name in ("Assets", "Packages", "ProjectSettings"):
            (project / name).mkdir(parents=True)
        manifest = project / "Packages/manifest.json"
        manifest.write_text('{"dependencies":{}}\n')
        original_manifest = manifest.read_bytes()
        (project / "ProjectSettings/ProjectVersion.txt").write_text("m_EditorVersion: 6000.7.0a5")
        env = {**os.environ, "PYTHONPATH": str(installed),
               "PYTHONIOENCODING": "utf-8", "FAFNIR_BRIDGE_ROOT": str(root / "bridge")}

        def run(*arguments):
            result = subprocess.run([sys.executable, "-m", "game_stack_planner",
                "--db", str(root / "catalog.db"), "--json", *arguments],
                cwd=root, env=env, text=True, encoding="utf-8", capture_output=True, timeout=60)
            if result.returncode:
                raise RuntimeError(result.stderr or result.stdout)
            return json.loads(result.stdout)

        identity = subprocess.run([sys.executable, "-c",
            "import game_stack_planner; from game_stack_planner.bridge_setup import bundled_bridge; "
            "print(game_stack_planner.__file__); print(bundled_bridge())"],
            cwd=root, env=env, text=True, capture_output=True, check=True)
        assert str(installed) in identity.stdout, identity.stdout
        assert "_unity_bridge" in identity.stdout, identity.stdout
        diagnosis = run("bridge-doctor", "--project", str(project))
        assert diagnosis["installation"] == "not_installed", diagnosis
        plan = run("bridge-plan", "--project", str(project))
        result = run("bridge-apply", plan["plan"]["id"], "--approval-nonce", plan["approval_nonce"])
        package = project / "Packages/com.taiyousun.stackforge"
        assert (package / "LICENSE.md").is_file()
        assert (package / "Editor/FafnirBridgeDiagnostics.cs").is_file()
        assert not result["diagnosis"]["setup_verified"]
        run("bridge-status", result["job"]["id"])
        restored = run("bridge-rollback", result["job"]["id"],
                       "--rollback-nonce", result["rollback_nonce"])
        assert restored["job"]["status"] == "rolled_back"
        assert manifest.read_bytes() == original_manifest
        assert not package.exists()
        print("PASS: wheel-only import, diagnosis, CLI plan/apply/status/rollback; no source checkout dependency.")


if __name__ == "__main__":
    main()
