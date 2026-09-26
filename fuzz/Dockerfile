FROM rust:1.92-bookworm@sha256:e90e846de4124376164ddfbaab4b0774c7bdeef5e738866295e5a90a34a307a2
RUN apt-get update && apt-get install -y --no-install-recommends python3 clang \
    && rm -rf /var/lib/apt/lists/*
RUN rustup toolchain install nightly-2026-09-20 --profile minimal
RUN cargo install cargo-fuzz --version 0.13.2 --locked
RUN git config --global --add safe.directory /workspace
WORKDIR /workspace
CMD ["python3", "scripts/fuzz_parsers.py"]
