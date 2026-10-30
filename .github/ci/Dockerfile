# What the suite assumes of its host: Python 3.14 with requirements.txt installed, git, ps, a zsh login shell, an installed claude,
# an `agy` on PATH (a stand-in: the suite resolves it and never runs it), and node for the page reload check (#337, #855).
FROM python:3.14-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git zsh procps curl ca-certificates nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/zsh ci
COPY --chmod=755 .github/ci/agy /usr/local/bin/agy
# The engine's pinned packages (requirements.txt), installed at image build, which has network; the test run has none.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
USER ci
ENV PATH=/home/ci/.local/bin:$PATH
RUN curl -fsSL https://claude.ai/install.sh | bash
WORKDIR /home/ci/src
