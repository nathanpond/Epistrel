"""The Lite pipeline (FR-LITE): one generation call per turn (#57 fills this package).

It must never import ``epistrel.orchestrator.policies.admin``, the policy store's only write
path (import-linter contract ``policy-store-written-only-by-admin``, FR-GUARD-1, NFR-SEC-1).
"""
