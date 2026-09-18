FROM rust:1.92-bookworm@sha256:e90e846de4124376164ddfbaab4b0774c7bdeef5e738866295e5a90a34a307a2

WORKDIR /workspace

RUN apt-get update && apt-get install -y --no-install-recommends python3-dev python3-venv \
    && python3 -m venv /opt/storage-python-build \
    && /opt/storage-python-build/bin/pip install --no-cache-dir maturin==1.15.0
ENV PATH="/opt/storage-python-build/bin:${PATH}"

COPY . .

RUN cargo build --workspace --all-targets --locked

CMD ["bash", "./scripts/verify.sh"]
