FROM rust:1.92-bookworm@sha256:e90e846de4124376164ddfbaab4b0774c7bdeef5e738866295e5a90a34a307a2

WORKDIR /workspace

COPY . .

RUN cargo build --workspace --all-targets --locked

CMD ["bash", "./scripts/verify.sh"]
