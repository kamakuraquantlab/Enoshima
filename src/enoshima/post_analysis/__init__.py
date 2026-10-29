"""Analyses of a finished sweep, computed from its runs in the lake -- nothing is replayed.

| Module | Asks |
|---|---|
| `walk_forward` | How the selection rule would have done, by window, history and how often it reselects |

Each writes its report under the environment's `post_analysis/` and logs a
table to `env.log`, like every other command.
"""
