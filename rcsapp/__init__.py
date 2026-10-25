"""RCS template analysis app."""
import os

__version__ = "1.0.0"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config")
KNOWLEDGE = os.path.join(ROOT, "knowledge")
