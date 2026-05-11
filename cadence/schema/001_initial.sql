-- CADENCE initial schema. See CADENCE-SPEC.md §4 for the authoritative model.

CREATE TABLE rhsa (
    rhsa_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    severity TEXT NOT NULL,
    published_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP,
    source_url TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    collected_at TIMESTAMP NOT NULL
);

CREATE TABLE rhsa_cve (
    rhsa_id TEXT NOT NULL,
    cve_id TEXT NOT NULL,
    cvss3_score REAL,
    cvss3_vector TEXT,
    PRIMARY KEY (rhsa_id, cve_id),
    FOREIGN KEY (rhsa_id) REFERENCES rhsa(rhsa_id)
);

CREATE TABLE rhsa_package_fix (
    rhsa_id TEXT NOT NULL,
    package_name TEXT NOT NULL,
    fixed_version TEXT NOT NULL,
    arch TEXT NOT NULL,
    product TEXT NOT NULL,
    PRIMARY KEY (rhsa_id, package_name, fixed_version, arch, product),
    FOREIGN KEY (rhsa_id) REFERENCES rhsa(rhsa_id)
);

CREATE TABLE rhsa_vex (
    rhsa_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    status TEXT NOT NULL,  -- not_affected | affected | fixed | under_investigation
    justification TEXT,
    PRIMARY KEY (rhsa_id, product_id),
    FOREIGN KEY (rhsa_id) REFERENCES rhsa(rhsa_id)
);

CREATE TABLE repo_observation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id TEXT NOT NULL,
    observed_at TIMESTAMP NOT NULL,
    repomd_revision TEXT NOT NULL,
    primary_xml_sha256 TEXT NOT NULL
);

CREATE TABLE repo_package (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL,
    package_name TEXT NOT NULL,
    version TEXT NOT NULL,
    arch TEXT NOT NULL,
    build_time TIMESTAMP,
    file_time TIMESTAMP,
    FOREIGN KEY (observation_id) REFERENCES repo_observation(id)
);

CREATE INDEX idx_repo_package_lookup ON repo_package(package_name, version, arch);

-- Catalog images (registry.access.redhat.com) and Quay images coexist here;
-- the `source` column distinguishes them.
CREATE TABLE container_image (
    image_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,                 -- 'catalog' | 'quay'
    registry TEXT NOT NULL,
    repository TEXT NOT NULL,
    tier TEXT NOT NULL,                   -- 'ubi' | 'ocp_platform' | 'rh_layered' |
                                          -- 'quay_community' | 'quay_partner' |
                                          -- 'quay_redhat' | 'other'
    tag TEXT NOT NULL,
    digest TEXT NOT NULL,
    architecture TEXT NOT NULL,
    build_date TIMESTAMP NOT NULL,
    parsed_version TEXT,
    parsed_build_num INTEGER,
    raw_json TEXT NOT NULL,
    collected_at TIMESTAMP NOT NULL
);

CREATE INDEX idx_container_image_repo_tag ON container_image(repository, tag);
CREATE INDEX idx_container_image_build ON container_image(repository, build_date);
CREATE INDEX idx_container_image_tier ON container_image(tier, build_date);

CREATE TABLE container_image_rpm (
    image_id TEXT NOT NULL,
    package_name TEXT NOT NULL,
    version TEXT NOT NULL,
    arch TEXT NOT NULL,
    PRIMARY KEY (image_id, package_name, arch),
    FOREIGN KEY (image_id) REFERENCES container_image(image_id)
);

CREATE INDEX idx_image_rpm_lookup ON container_image_rpm(package_name, version);

-- Legacy advisory_rpm_mapping (catalog only, pre-November 2024). Used for
-- cross-validating CADENCE-computed RHSA→image mappings against the field that
-- the Container Catalog API stopped populating in November 2024.
CREATE TABLE catalog_advisory_mapping (
    image_id TEXT NOT NULL,
    advisory_id TEXT NOT NULL,
    nvra TEXT NOT NULL,
    PRIMARY KEY (image_id, advisory_id, nvra),
    FOREIGN KEY (image_id) REFERENCES container_image(image_id)
);

CREATE TABLE gap_measurement (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rhsa_id TEXT NOT NULL,
    repository TEXT NOT NULL,
    tier TEXT NOT NULL,
    architecture TEXT NOT NULL,
    package_name TEXT NOT NULL,
    fixed_version TEXT NOT NULL,
    rhsa_published_at TIMESTAMP NOT NULL,
    repo_first_seen_at TIMESTAMP,
    image_first_built_at TIMESTAMP,
    image_id TEXT,
    gap_a_seconds INTEGER,
    gap_b_seconds INTEGER,
    gap_c_seconds INTEGER,
    computed_at TIMESTAMP NOT NULL,
    methodology_version TEXT NOT NULL,
    FOREIGN KEY (rhsa_id) REFERENCES rhsa(rhsa_id),
    FOREIGN KEY (image_id) REFERENCES container_image(image_id)
);

CREATE INDEX idx_gap_lookup ON gap_measurement(repository, rhsa_id);
CREATE INDEX idx_gap_tier ON gap_measurement(tier, rhsa_published_at);

-- Inter-build interval as a first-class metric.
CREATE TABLE rebuild_interval (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository TEXT NOT NULL,
    tier TEXT NOT NULL,
    architecture TEXT NOT NULL,
    prior_image_id TEXT NOT NULL,
    next_image_id TEXT NOT NULL,
    prior_build_date TIMESTAMP NOT NULL,
    next_build_date TIMESTAMP NOT NULL,
    interval_seconds INTEGER NOT NULL,
    computed_at TIMESTAMP NOT NULL,
    FOREIGN KEY (prior_image_id) REFERENCES container_image(image_id),
    FOREIGN KEY (next_image_id) REFERENCES container_image(image_id)
);

CREATE INDEX idx_rebuild_tier ON rebuild_interval(tier, next_build_date);

-- Configured tracking targets. `rationale` documents *why* the repo is tracked,
-- so the dataset's selection bias is auditable from the database alone.
CREATE TABLE tracked_repository (
    repository TEXT PRIMARY KEY,
    source TEXT NOT NULL,                 -- 'catalog' | 'quay'
    registry TEXT NOT NULL,
    tier TEXT NOT NULL,
    rationale TEXT NOT NULL,
    added_at TIMESTAMP NOT NULL
);
