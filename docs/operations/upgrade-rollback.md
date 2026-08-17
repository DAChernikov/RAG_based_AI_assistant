# Upgrade and rollback

1. Review release notes, image/SBOM/scanner reports and migration compatibility.
2. Back up PostgreSQL and verify a recent restore drill.
3. Run upgrade/downgrade/upgrade migrations on a staging copy; execute owner acceptance.
4. Apply the migration one-shot job, then rolling-update stateless services. Watch readiness, DB saturation, queue age and DLQ.
5. If application rollback is schema-compatible, restore previous image digests. Otherwise stop traffic and restore the pre-upgrade database backup.

Knowledge source, index, model and prompt versions use their admin activation endpoints for atomic rollback. Do not delete active or pinned versions. Failed deployment recovery must not reset Redis or force Git history.
