FROM rust:1.98.1-bookworm@sha256:93ce27a88655056a51dbdd8f5f2d7ddc071c7b0070fb288a37b5a285fc83971e
COPY .python-version /tmp/.python-version
COPY scripts/install_python.sh /tmp/install_python.sh
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl xz-utils libssl-dev zlib1g-dev libbz2-dev libreadline-dev \
    libsqlite3-dev libffi-dev liblzma-dev uuid-dev \
    && test "$(cut -d. -f1,2 /tmp/.python-version)" = "3.14" \
    && sh /tmp/install_python.sh /opt/python
ENV LD_LIBRARY_PATH="/opt/python/lib"
ENV PATH="/opt/python/bin:${PATH}"
RUN apt-get update && apt-get install -y --no-install-recommends clang \
    && rm -rf /var/lib/apt/lists/*
RUN rustup toolchain install nightly-2026-09-20 --profile minimal
RUN cargo install cargo-fuzz --version 0.13.2 --locked
RUN git config --global --add safe.directory /workspace
WORKDIR /workspace
CMD ["python3", "scripts/fuzz_parsers.py"]
