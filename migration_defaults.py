# =============================================================
# migration_defaults.py — server-side module, added 2026-09-25
#
# One shared constant for the per-license-isolation migration
# (fleet_store.py/bid_history.py/load_store.py all previously had zero
# license_key columns — every account shared one global pool of
# trucks, bid history, live loads, and the blacklist). Every
# pre-existing row across all three databases legitimately belongs to
# the one real account already in production use (confirmed by
# inspection: order_ids/dates in bid_history and load_store match real
# ongoing broker traffic, not test data), so the one-time backfill
# during each module's init_db() migration needs this same value.
# Defined once here rather than repeated as a magic string in three
# separate migration blocks.
# =============================================================

LEGACY_DATA_LICENSE_KEY = "PLTS-FOHD-X6UF-2K27-SEME"
