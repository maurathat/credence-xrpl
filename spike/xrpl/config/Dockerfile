FROM debian:12-slim@sha256:a4672c0cb26fbdde88e38fa2dfb6c681942306680e41e4378b28770b6e79ee91

ARG XRPLD_DEB_URL=https://packages.xrplf.org/repository/deb-stable/pool/x/xrpld/xrpld_3.4.0-1_amd64.deb
ARG XRPLD_DEB_SHA256=4dc40a21b12b7d21f2760b16545c1d842962a96978dd3199eb8de9aa0aff8339

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl \
    && curl --fail --location --output /tmp/xrpld.deb "${XRPLD_DEB_URL}" \
    && echo "${XRPLD_DEB_SHA256}  /tmp/xrpld.deb" | sha256sum --check --strict \
    && apt-get install --yes --no-install-recommends /tmp/xrpld.deb \
    && rm -f /tmp/xrpld.deb \
    && rm -rf /var/lib/apt/lists/*

COPY xrpld.cfg /etc/opt/xrpl/xrpld.cfg

EXPOSE 5005

ENTRYPOINT ["/usr/bin/xrpld"]
CMD ["--conf=/etc/opt/xrpl/xrpld.cfg", "--standalone", "--start"]
