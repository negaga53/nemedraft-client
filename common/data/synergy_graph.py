"""Runtime helpers for loading per-set synergy graphs.

These are the inference-side entry points used by ``server.deck_router``
to feed the deckbuild model's GNN delta cache. The training-side
``training/data/synergy_graph.py`` keeps the heavier ``build_synergy_graph``
that emits the ``.npz`` files this module reads.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from scipy import sparse


def load_synergy_graph(path: Path) -> sparse.csr_matrix:
    """Load a saved synergy graph."""
    return sparse.load_npz(path)


def synergy_graph_to_pyg(adj: sparse.csr_matrix):
    """Convert scipy sparse adjacency to PyG edge_index + edge_attr format."""
    coo = adj.tocoo()
    edge_index = np.stack([coo.row, coo.col], axis=0).astype(np.int64)
    edge_attr = coo.data.astype(np.float32)
    return edge_index, edge_attr
