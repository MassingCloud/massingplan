"""Domain services and the three adapter seams.

`entitlement`, `identity` and `storage` are the *entire* external surface of
this product (SPEC.md 3.2). Each has an ABC, a local default that ships and is
exercised by the default path, and an optional remote implementation imported
lazily. The `no-adapters` CI job deletes every remote one and re-runs the suite.
"""
