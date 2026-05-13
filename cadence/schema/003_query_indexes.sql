-- Post-incident indexes for WP-09 reconstruct hot paths.
--
-- Reconstruct's `_image_first_built_at()` filters container_image by
-- (repository, architecture) before joining container_image_rpm on
-- image_id. Without this index the query path table-scans container_image
-- inside the join — on the OptiPlex's real dataset (11k images, 1.28M
-- RPM rows) that pushed reconstruct toward an estimated ~10-hour runtime.
--
-- The other (package_name, version) join key is already covered by
-- idx_image_rpm_lookup; the (image_id, package_name, arch) PK on
-- container_image_rpm covers the inner half of the join.

CREATE INDEX IF NOT EXISTS idx_container_image_repo_arch
    ON container_image(repository, architecture);

-- Filter index for the not_affected VEX lookup. Runs once per reconstruct,
-- but with 1M+ rhsa_vex rows the scan is a noticeable cold-start cost.
CREATE INDEX IF NOT EXISTS idx_rhsa_vex_status
    ON rhsa_vex(status) WHERE status = 'not_affected';
