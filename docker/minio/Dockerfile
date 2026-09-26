# Rebuild the exact MinIO source used by the original fixture. The upstream
# Quay image is no longer retrievable by the pinned digest on clean runners.
FROM golang:1.24.6-bookworm@sha256:4f8589362626cfa081fdab2b474cda87c3b51d74b1e227c110d2ffde56daad5c AS build
ADD --checksum=sha256:8819e3e7817e46b7b3798f8f200ead208562e571563c2e040352378031abe9f2 https://codeload.github.com/minio/minio/tar.gz/07c3a429bfed433e49018cb0f78a52145d4bedeb /source.tar.gz
RUN mkdir /source && tar -xzf /source.tar.gz --strip-components=1 -C /source
WORKDIR /source
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
RUN go build -mod=readonly -trimpath -buildvcs=false \
    -ldflags='-s -w -X github.com/minio/minio/cmd.Version=RELEASE.2025-09-07T16-13-09Z -X github.com/minio/minio/cmd.CommitID=07c3a429bfed433e49018cb0f78a52145d4bedeb -X github.com/minio/minio/cmd.ReleaseTag=RELEASE.2025-09-07T16-13-09Z' \
    -o /out/minio .

FROM python:3.12-slim-bookworm@sha256:1aaa65a85fda306ffb8b910824d4e93bdce61e212c7e87168123ea3073b41a1a
COPY --from=build /out/minio /usr/local/bin/minio
COPY --from=build /source/LICENSE /usr/share/licenses/minio/LICENSE
COPY init.py /init.py
LABEL org.opencontainers.image.source="https://github.com/minio/minio" \
      org.opencontainers.image.revision="07c3a429bfed433e49018cb0f78a52145d4bedeb" \
      org.opencontainers.image.licenses="AGPL-3.0-only"
ENTRYPOINT ["minio"]
