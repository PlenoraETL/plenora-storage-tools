FROM rust:1.98.1-bookworm@sha256:93ce27a88655056a51dbdd8f5f2d7ddc071c7b0070fb288a37b5a285fc83971e

WORKDIR /workspace

# Compose mounts the user's committed checkout into this root-owned test image.
RUN git config --global --add safe.directory /workspace

RUN apt-get update && apt-get install -y --no-install-recommends python3-dev python3-venv \
    && python3 -m venv /opt/storage-python-build \
    && /opt/storage-python-build/bin/pip install --no-cache-dir maturin==1.15.0
ENV PATH="/opt/storage-python-build/bin:${PATH}"

COPY . .

RUN cargo build --workspace --all-targets --locked

CMD ["bash", "./scripts/verify.sh"]
