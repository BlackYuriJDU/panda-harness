"""Entry point: serve the Panda PLE harness over the evaluator's WebSocket protocol."""

import harness
import taskbridge

if __name__ == "__main__":
    taskbridge.run(harness.solve)
