# What the suite assumes of its host: Python 3.14, git, ps, node (the board clock runs in it),
# a zsh login shell, an installed claude, and an `agy` on PATH (a stand-in: the suite resolves it and never runs it).
FROM python:3.14-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git zsh procps curl ca-certificates nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/zsh ci
COPY --chmod=755 agy /usr/local/bin/agy
USER ci
ENV PATH=/home/ci/.local/bin:$PATH
RUN curl -fsSL https://claude.ai/install.sh | bash
WORKDIR /home/ci/src
