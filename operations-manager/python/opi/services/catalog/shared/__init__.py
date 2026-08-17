"""Definitions shared by more than one service.

A service package (``catalog/<service>/``) owns everything specific to that one
service. When two services genuinely share a definition -- e.g. persistent-storage
and temp-storage share the same storage-mount config shape -- it lives here rather
than being duplicated or dumped in a central forms/config module. Keep this small:
it is a home for real cross-service sharing, not a catch-all.
"""
