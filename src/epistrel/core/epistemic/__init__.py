"""The epistemic model: per-character belief partitions, separate from objective canon (FR-EPI;
docs/02 §4.4; #159).

``partitions`` holds what each modeled character believes (``beliefs``) and the queryable
projection of it (``belief_state``); #160's ``transitions`` records why. Canon (``core.canon``)
never reads a belief and a partition read never reads a fact (FR-EPI-1; ``tools.gates
check-epistemic-separation``)."""
