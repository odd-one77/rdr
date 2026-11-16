# RDR in a Debian container image. Build from the unpacked rdr-debian-<version>.tar.gz
# or a checkout:
#     docker build -t rdr .
#     docker run -d --name rdr --restart unless-stopped --network host -v rdr-data:/data rdr
# Host networking lets phones and mirror screens use the machine's own LAN address,
# which is also the address the HTTPS certificate is issued for.
FROM debian:13-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 python3-venv ca-certificates ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --user-group --uid 990 --home-dir /opt/rdr --no-create-home --shell /usr/sbin/nologin rdr \
    && install -d -o rdr -g rdr /opt/rdr /data

USER rdr
WORKDIR /opt/rdr
COPY --chown=rdr:rdr requirements.txt ./
RUN python3 -m venv .venv && .venv/bin/python -m pip install --no-cache-dir --disable-pip-version-check -r requirements.txt
COPY --chown=rdr:rdr app ./app
COPY --chown=rdr:rdr run.py README.md ./

ENV RDR_DATA_DIR=/data PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080 8443
STOPSIGNAL SIGTERM
CMD [".venv/bin/python", "run.py", "--no-browser"]
