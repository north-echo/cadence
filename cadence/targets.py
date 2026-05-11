"""Default v1 tracked-repository set, per CADENCE-SPEC.md §6.

Each entry records a (source, registry, repository, tier, rationale). The
rationale is preserved on insert into ``tracked_repository`` so the dataset's
selection bias is auditable from the database alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Source = Literal["catalog", "quay"]
Tier = Literal[
    "ubi",
    "ocp_platform",
    "rh_layered",
    "quay_redhat",
    "quay_community",
    "quay_partner",
    "other",
]


@dataclass(frozen=True)
class TrackedRepo:
    repository: str
    source: Source
    registry: str
    tier: Tier
    rationale: str


# Tier: ubi
UBI_REPOS: tuple[TrackedRepo, ...] = (
    TrackedRepo("ubi8/ubi", "catalog", "registry.access.redhat.com", "ubi",
                "Reference UBI 8 standard variant"),
    TrackedRepo("ubi8/ubi-minimal", "catalog", "registry.access.redhat.com", "ubi",
                "UBI 8 minimal, common in slim images"),
    TrackedRepo("ubi8/ubi-micro", "catalog", "registry.access.redhat.com", "ubi",
                "UBI 8 micro, smallest variant"),
    TrackedRepo("ubi9/ubi", "catalog", "registry.access.redhat.com", "ubi",
                "Reference UBI 9 standard variant"),
    TrackedRepo("ubi9/ubi-minimal", "catalog", "registry.access.redhat.com", "ubi",
                "UBI 9 minimal"),
    TrackedRepo("ubi9/ubi-micro", "catalog", "registry.access.redhat.com", "ubi",
                "UBI 9 micro"),
    TrackedRepo("ubi9/ubi-init", "catalog", "registry.access.redhat.com", "ubi",
                "UBI 9 with init system"),
    TrackedRepo("ubi10/ubi", "catalog", "registry.access.redhat.com", "ubi",
                "UBI 10 (launched May 2025)"),
    TrackedRepo("ubi10/ubi-minimal", "catalog", "registry.access.redhat.com", "ubi",
                "UBI 10 minimal"),
    TrackedRepo("ubi10/ubi-micro", "catalog", "registry.access.redhat.com", "ubi",
                "UBI 10 micro"),
)

# Tier: ocp_platform
OCP_PLATFORM_REPOS: tuple[TrackedRepo, ...] = (
    TrackedRepo("openshift4/ose-cli", "catalog", "registry.access.redhat.com", "ocp_platform",
                "Core OpenShift CLI image, broad usage"),
    TrackedRepo("openshift4/ose-installer", "catalog", "registry.access.redhat.com",
                "ocp_platform", "OCP installer, spike-validated 1900+ tags"),
    TrackedRepo("openshift4/ose-haproxy-router", "catalog", "registry.access.redhat.com",
                "ocp_platform", "Default ingress component"),
    TrackedRepo("openshift4/ose-kube-rbac-proxy", "catalog", "registry.access.redhat.com",
                "ocp_platform", "Common platform component"),
)

# Tier: rh_layered
RH_LAYERED_REPOS: tuple[TrackedRepo, ...] = (
    TrackedRepo("rhacm2/console-rhel9", "catalog", "registry.access.redhat.com", "rh_layered",
                "RHACM core component"),
    TrackedRepo("multicluster-engine/cluster-curator-controller-rhel9", "catalog",
                "registry.access.redhat.com", "rh_layered", "MCE core component"),
    TrackedRepo("openshift-logging/cluster-logging-operator-bundle", "catalog",
                "registry.access.redhat.com", "rh_layered", "Logging operator"),
    TrackedRepo("openshift-logging/vector-rhel9", "catalog", "registry.access.redhat.com",
                "rh_layered", "Logging data plane"),
    TrackedRepo("odf4/odf-rhel9-operator", "catalog", "registry.access.redhat.com", "rh_layered",
                "ODF operator"),
    TrackedRepo("odf4/cephcsi-rhel9", "catalog", "registry.access.redhat.com", "rh_layered",
                "ODF Ceph CSI"),
    TrackedRepo("openshift-service-mesh/istio-rhel9-operator", "catalog",
                "registry.access.redhat.com", "rh_layered", "Service Mesh operator"),
)

# Tier: quay_redhat
QUAY_REDHAT_REPOS: tuple[TrackedRepo, ...] = (
    TrackedRepo("redhat/ubi9", "quay", "quay.io", "quay_redhat",
                "Red Hat publishes UBI to Quay; useful comparison vs. "
                "registry.access.redhat.com"),
    TrackedRepo("redhat/ubi9-minimal", "quay", "quay.io", "quay_redhat",
                "Same; UBI 9 minimal variant"),
)

# Tier: quay_community
QUAY_COMMUNITY_REPOS: tuple[TrackedRepo, ...] = (
    TrackedRepo("cilium/cilium", "quay", "quay.io", "quay_community", "Major CNI option"),
    TrackedRepo("cilium/cilium-operator-generic", "quay", "quay.io", "quay_community",
                "Cilium operator"),
    TrackedRepo("argoproj/argocd", "quay", "quay.io", "quay_community", "Major GitOps tool"),
    TrackedRepo("prometheus/prometheus", "quay", "quay.io", "quay_community",
                "Common observability"),
    TrackedRepo("prometheus-operator/prometheus-operator", "quay", "quay.io", "quay_community",
                "Common observability operator"),
    TrackedRepo("jaegertracing/jaeger-operator", "quay", "quay.io", "quay_community", "Tracing"),
    TrackedRepo("kiali/kiali", "quay", "quay.io", "quay_community",
                "Service mesh observability"),
    TrackedRepo("strimzi/strimzi-operator", "quay", "quay.io", "quay_community",
                "Kafka operator"),
    TrackedRepo("kubevirt/virt-operator", "quay", "quay.io", "quay_community", "Virtualization"),
    TrackedRepo("projectquay/quay", "quay", "quay.io", "quay_community", "Quay itself"),
)

# Tier: quay_partner
QUAY_PARTNER_REPOS: tuple[TrackedRepo, ...] = (
    TrackedRepo("crunchydata/postgres-operator", "quay", "quay.io", "quay_partner",
                "Major partner, security-focused"),
    TrackedRepo("bitnami/postgresql", "quay", "quay.io", "quay_partner", "Common Bitnami image"),
    TrackedRepo("bitnami/redis", "quay", "quay.io", "quay_partner", "Common Bitnami image"),
)

ALL_REPOS: tuple[TrackedRepo, ...] = (
    *UBI_REPOS,
    *OCP_PLATFORM_REPOS,
    *RH_LAYERED_REPOS,
    *QUAY_REDHAT_REPOS,
    *QUAY_COMMUNITY_REPOS,
    *QUAY_PARTNER_REPOS,
)


def by_tier(tier: Tier) -> tuple[TrackedRepo, ...]:
    return tuple(r for r in ALL_REPOS if r.tier == tier)


def by_source(source: Source) -> tuple[TrackedRepo, ...]:
    return tuple(r for r in ALL_REPOS if r.source == source)


_BY_REPOSITORY = {r.repository: r for r in ALL_REPOS}


def find(repository: str) -> TrackedRepo | None:
    """Look up a configured TrackedRepo by repository name, or return None."""
    return _BY_REPOSITORY.get(repository)


__all__ = [
    "ALL_REPOS",
    "OCP_PLATFORM_REPOS",
    "QUAY_COMMUNITY_REPOS",
    "QUAY_PARTNER_REPOS",
    "QUAY_REDHAT_REPOS",
    "RH_LAYERED_REPOS",
    "UBI_REPOS",
    "Source",
    "Tier",
    "TrackedRepo",
    "by_source",
    "by_tier",
    "find",
]
