"""Local-first Unity game stack recommendation tool."""

from .models import Candidate, GameRequirement, ProjectSnapshot
from .repository import StackRepository
from .service import GameStackPlanner

__all__ = [
    "Candidate",
    "GameRequirement",
    "GameStackPlanner",
    "ProjectSnapshot",
    "StackRepository",
]

__version__ = "0.5.0"
