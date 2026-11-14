FROM rust:1.98.1-bookworm@sha256:93ce27a88655056a51dbdd8f5f2d7ddc071c7b0070fb288a37b5a285fc83971e

WORKDIR /workspace

# Compose mounts the user's committed checkout into this root-owned test image.
RUN git config --global --add safe.directory /workspace

COPY .python-version /tmp/.python-version
COPY scripts/install_python.sh /tmp/install_python.sh
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl xz-utils libssl-dev zlib1g-dev libbz2-dev libreadline-dev \
    libsqlite3-dev libffi-dev liblzma-dev uuid-dev \
    && test "$(cut -d. -f1,2 /tmp/.python-version)" = "3.14" \
    && sh /tmp/install_python.sh /opt/python
ENV LD_LIBRARY_PATH="/opt/python/lib"
ENV PATH="/opt/python/bin:${PATH}"

RUN python3 -m venv /opt/storage-python-build \
    && /opt/storage-python-build/bin/pip install --no-cache-dir maturin==1.16.0
ENV PATH="/opt/storage-python-build/bin:${PATH}"

COPY . .

RUN cargo build --workspace --all-targets --locked

CMD ["bash", "./scripts/verify.sh"]
