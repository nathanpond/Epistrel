"""The repair ladder (FR-REPAIR-1..4, FR-VERIFY-12, FR-GUARD-6; #151): ``ladder.RepairLadder``
corrects a structured beat whose claims ``needs_repair`` (contradicted, undecided under the
``repair`` disposition, or unpermitted), re-verifies every correction before it is accepted and
keeps every rejected text for the audit record. The deterministic rungs and the classification
are the Engine Core's (``core.repair``)."""
