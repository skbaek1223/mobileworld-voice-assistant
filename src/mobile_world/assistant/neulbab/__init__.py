"""늘밥 (Neulbab) support for the in-app voice assistant.

- ``data``     : load the web prototype's data files (``NEULBAB_APP_DIR``)
- ``logic``    : pure rules ported from ``mock_api.js``
- ``adapters`` : mock delivery-platform adapters (가/나/다 배달앱)
- ``mock_app`` : headless app implementing docs/neulbab/tools.json
- ``toolset``  : the mock app as assistant tools for one role, plus role instructions
"""

from mobile_world.assistant.neulbab.data import NeulbabData, load_data
from mobile_world.assistant.neulbab.mock_app import MockNeulbabApp
from mobile_world.assistant.neulbab.toolset import (
    PROMPT_EXAMPLES,
    ROLE_INSTRUCTIONS,
    build_toolset,
)

__all__ = [
    "PROMPT_EXAMPLES",
    "ROLE_INSTRUCTIONS",
    "MockNeulbabApp",
    "NeulbabData",
    "build_toolset",
    "load_data",
]
