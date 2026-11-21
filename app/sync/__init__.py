"""Shared base for the API, the phone/site synchronisation and edit conflict checks.

fields.py: the abstract SyncModel (uid, version, updated_at, updated_by) and the tracked manager.
capture.py: who is writing (actor), and the change log (SyncChange) fed by signals.
"""
