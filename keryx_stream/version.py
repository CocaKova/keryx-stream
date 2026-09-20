"""Single source for the plugin version and the feature list /keryx/health
advertises. The app reads the list to tell "this install predates the panel"
from "the panel is broken"."""
__version__ = "0.3.0"

FEATURES = (
    "stream", "stream.chat_key", "publish", "toolsets",
    "capabilities", "reasoning", "commands", "config", "config.raw", "brains",
    "logs", "kanban", "skills", "skills.trash", "sessions.prune", "pets",
    "update", "git",
)
