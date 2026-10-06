FROM rust:1.98.1-bookworm@sha256:93ce27a88655056a51dbdd8f5f2d7ddc071c7b0070fb288a37b5a285fc83971e
RUN apt-get update && apt-get install -y --no-install-recommends python3 clang \
    && rm -rf /var/lib/apt/lists/*
RUN rustup toolchain install nightly-2026-09-20 --profile minimal
RUN cargo install cargo-fuzz --version 0.13.2 --locked
RUN git config --global --add safe.directory /workspace
WORKDIR /workspace
CMD ["python3", "scripts/fuzz_parsers.py"]
