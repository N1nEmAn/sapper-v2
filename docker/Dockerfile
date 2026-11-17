# sapper agent image for TSecBench hosted mode ( Throne Protocol qualifier ).
#
# Match rules (2026-10-17/18 online round):
#   - ONE submission attempt, 8h task limit, 50 challenges / 100 flags
#   - ranked by score, then elapsed time, then TOKEN USAGE
#   - no hints; model rate-limits are NOT grounds for a retry
#   - logs/trace are NOT viewable -> the image must fail loud, never hang
#
# Build:  docker build -f docker/Dockerfile -t sapper-agent:latest .
# Export: bash docker/build.sh          ->  dist/sapper-agent.tar.gz
FROM kalilinux/kali-rolling:latest

ENV DEBIAN_FRONTEND=noninteractive \
    PATH="/home/agent/.local/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    SAPPER_MODEL=glm-5.3-flash[1m] \
    SAPPER_TOTAL_MINUTES=470 \
    SAPPER_MINUTES_PER_CHALLENGE=9 \
    SAPPER_MAX_STEPS=10 \
    SAPPER_RETRY_MODEL=3 \
    SAPPER_ROOT=/work/runs

# --- offensive tooling (kali repo, curated to stay < 3GB) -------------------
# Pin a reliable mirror: the default http.kali.org redirect can land on
# mirrors that fail to resolve in build sandboxes. Current kali-rolling is
# deb822-only (`/etc/apt/sources.list.d/kali.sources`); writing the legacy
# /etc/apt/sources.list alone leaves http.kali.org active and apt fails with
# 502 Bad Gateway. Rewrite the deb822 file in place, keeping Signed-By.
# HTTPS is unusable here: the pristine kali image has no usable CA bundle yet
# (apt fails `certificate verify failed`), and ca-certificates itself must come
# from this same repo — so fetch over plain HTTP. TUNA/Huawei/Aliyun/USTC HTTP
# all serve kali-rolling; TUNA is the primary, retries cover transient drops.
RUN printf '%s\n' \
        'Types: deb' \
        'URIs: http://mirrors.tuna.tsinghua.edu.cn/kali/' \
        'Suites: kali-rolling' \
        'Components: main contrib non-free non-free-firmware' \
        'Signed-By: /usr/share/keyrings/kali-archive-keyring.gpg' \
        > /etc/apt/sources.list.d/kali.sources \
    && rm -f /etc/apt/sources.list \
    && printf '%s\n' \
        'Acquire::Retries "10";' \
        'Acquire::http::Timeout "30";' \
        'Acquire::https::Timeout "30";' \
        'Acquire::http::Pipeline-Depth "0";' \
        > /etc/apt/apt.conf.d/80-retries
# cache mount: keep .debs out of the image layer and reuse them across retries
RUN --mount=type=cache,target=/var/cache/apt/archives,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl wget git jq unzip zip file xxd less vim \
        procps psmisc lsof tcpdump iproute2 iputils-ping net-tools \
        python3 python3-pip \
        python3-capstone python3-pycryptodome python3-sympy \
        python3-ropgadget python3-gmpy2 python3-z3 python3-jwt \
        nodejs npm \
        netcat-traditional socat \
        dnsutils whois \
        openssl \
        nmap masscan \
        whatweb wafw00f nikto \
        gobuster ffuf dirb wfuzz \
        sqlmap \
        hydra \
        binwalk steghide exiftool \
        libgmp-dev libmpfr-dev libmpc-dev \
        redis-tools default-mysql-client postgresql-client \
        gdb checksec \
        smbclient smbmap \
        ldap-utils \
        enum4linux \
        responder impacket-scripts \
        proxychains4 \
        seclists \
        sudo \
    && rm -rf /var/lib/apt/lists/* \
    && du -sh /usr/share/seclists \
    && rm -rf /usr/share/seclists/IOCs \
              /usr/share/seclists/Misc /usr/share/seclists/Username_Lists \
              /usr/share/seclists/Discovery/DNS \
    && rm -rf /usr/share/doc /usr/share/man /usr/share/vim/vim*/doc \
    && du -sh /usr/share/seclists

# nuclei (Go release) — CVE-template runner referenced by the kb.
# The zip is downloaded on the host (proxy) and shipped via build context:
# the build sandbox cannot reach github.
COPY docker/nuclei.zip /tmp/nuclei.zip
RUN unzip -o /tmp/nuclei.zip -d /usr/local/bin nuclei \
    && chmod +x /usr/local/bin/nuclei && rm -f /tmp/nuclei.zip

# --- model CLI --------------------------------------------------------------
# --allow-scripts: kali's npm blocks postinstall by default, but claude-code
# ships a native binary that its postinstall (node install.cjs) must fetch.
# registry: both registry.npmjs.org and registry.npmmirror.com drop the TLS
# handshake from the build network; Tencent's mirror serves the package and
# its linux-x64 optional dependency over HTTPS.
RUN npm install -g --registry=https://mirrors.cloud.tencent.com/npm/ \
        --fetch-retries=5 --fetch-timeout=600000 \
        --allow-scripts=@anthropic-ai/claude-code @anthropic-ai/claude-code \
    && claude --version \
    && npm cache clean --force

# --- python extras ----------------------------------------------------------
# pypi.org drops the TLS handshake from the build network (same as npmjs);
# use the TUNA PyPI mirror.
RUN pip3 install --no-cache-dir --break-system-packages --ignore-installed \
        -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 60 --retries 5 \
        requests pyjwt "unicorn==2.0.1.post1" pwntools \
        pycryptodome sympy ROPgadget
# ropper's filebytes dependency calls ast.Str, removed in Python 3.14 (Kali
# rolling ships 3.14); ROPgadget covers the same use case, so keep it optional.
RUN pip3 install --no-cache-dir --break-system-packages \
        -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 60 --retries 5 \
        ropper \
        || echo "WARN: ropper skipped (py3.14 ast.Str removed) — ROPgadget covers it"
# heavy/compiled extras: failure here must not kill the build (kb mentions them
# as optional; stdlib+sympy covers most crypto)
RUN pip3 install --no-cache-dir --break-system-packages \
        -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 60 --retries 5 \
        gmpy2 z3-solver \
        || echo "WARN: gmpy2/z3 unavailable — continuing without them"

# --- sapper -----------------------------------------------------------------
COPY lib/        /opt/sapper/lib/
COPY templates/  /opt/sapper/templates/
COPY kb/         /opt/sapper/kb/
COPY README.md LICENSE SKILL.md /opt/sapper/

# Windows checkouts carry CRLF; a CR in the shebang makes `sapper` fail with
# `env: 'python3\r': No such file or directory`, and CRLF in a shell script
# breaks `#!/usr/bin/env bash` the same way. Normalize inside the image so the
# build works from any host OS.
RUN find /opt/sapper -type f \( -name '*.py' -o -name '*.sh' \) \
        -exec sed -i 's/\r$//' {} +

# --- non-root agent user ----------------------------------------------------
# Claude Code refuses --dangerously-skip-permissions as root; run unprivileged
# with a writable HOME for the model CLI's state.
RUN useradd -m -s /bin/bash agent \
    && mkdir -p /home/agent/.local/bin /work/runs \
    && ln -sf /opt/sapper/lib/sapper.py /home/agent/.local/bin/sapper \
    && chmod +x /opt/sapper/lib/sapper.py \
    && chown -R agent:agent /work /opt/sapper /home/agent

COPY docker/entrypoint.sh /usr/local/bin/sapper-entrypoint
RUN sed -i 's/\r$//' /usr/local/bin/sapper-entrypoint \
    && chmod +x /usr/local/bin/sapper-entrypoint

USER agent
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/sapper-entrypoint"]
