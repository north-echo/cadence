# CADENCE container image — rootless Podman, Fedora 42 base
#
# Build:   podman build -t cadence .
# Run:     podman run --rm -it \
#            -v "$HOME/.local/share/cadence:/data:Z" \
#            -v "$HOME/.cache/cadence:/cache:Z" \
#            -e CADENCE_DB_PATH=/data/cadence.db \
#            -e CADENCE_CACHE_DIR=/cache \
#            cadence cadence --help

FROM registry.fedoraproject.org/fedora:42

RUN dnf install -y --setopt=install_weak_deps=False \
        python3.12 \
        python3.12-devel \
        python3-rpm \
        skopeo \
        ca-certificates \
        tzdata \
    && dnf clean all \
    && rm -rf /var/cache/dnf

# uv for dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /opt/cadence

# Install dependencies first to maximize layer cache hits
COPY pyproject.toml ./
RUN uv venv --python 3.12 /opt/cadence/.venv \
    && uv pip install --python /opt/cadence/.venv/bin/python -e . \
    || true  # pyproject-only install at this stage; full install after copy

# Copy project sources
COPY cadence ./cadence
COPY README.md LICENSE DATASET-LICENSE ./

# Install the project
RUN uv pip install --python /opt/cadence/.venv/bin/python -e .

ENV PATH="/opt/cadence/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Non-root user for rootless execution inside the container
RUN useradd --create-home --uid 1000 cadence
USER cadence
WORKDIR /home/cadence

ENTRYPOINT ["cadence"]
CMD ["--help"]
