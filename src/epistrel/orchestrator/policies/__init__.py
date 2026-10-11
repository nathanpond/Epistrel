"""The policy store (FR-GUARD-1,8, NFR-SEC-1): ``store`` reads and resolves effective sets;
``admin`` is the only write path, called only by ``epistrel.Engine``'s ``policies_*`` methods
behind ``/admin/policies`` and the story-settings route (#63)."""
