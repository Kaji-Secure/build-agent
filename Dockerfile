# The builder agent, compiled with Nuitka: users get a binary, not the Python source. Build from the repository root: docker build -t kaji-agent .
FROM python:3.13-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev patchelf ccache && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY . ./agent
ARG VERSION=
# what setuptools-scm would read from git (the build context has none): the release tag, or `python -m setuptools_scm` of the checkout
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION}
RUN pip install --no-cache-dir ./agent nuitka zstandard \
 && python -m nuitka --standalone --onefile --output-filename=kaji-agent --include-package=kaji_agent --include-package=websockets --output-dir=/out agent/entry.py

FROM debian:stable-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*   # the agent verifies the gateway's certificate
COPY --from=build /out/kaji-agent /usr/local/bin/kaji-agent
USER 65534
ENTRYPOINT ["kaji-agent"]
