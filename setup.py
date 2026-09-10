"""Ship the canonical Unity package inside wheels without duplicating source."""
from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWithUnityBridge(build_py):
    def run(self):
        super().run()
        source = Path(__file__).parent / "unity_package" / "com.taiyousun.stackforge"
        if not (source / "LICENSE.md").is_file():
            raise RuntimeError("The source distribution is missing the official Unity bridge.")
        destination = Path(self.build_lib) / "game_stack_planner" / "_unity_bridge"
        # build_lib belongs to this build, not an installed user's Unity project.
        build_root = Path(self.build_lib).resolve()
        if (destination.is_symlink() or destination.is_junction()
                or not destination.resolve().is_relative_to(build_root)
                or destination.resolve() == source.resolve()):
            raise RuntimeError("Unsafe Unity bridge build destination.")
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)


setup(cmdclass={"build_py": BuildWithUnityBridge})
