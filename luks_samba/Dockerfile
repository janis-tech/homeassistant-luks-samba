ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.21
FROM ${BUILD_FROM}

RUN apk add --no-cache \
      samba-server \
      samba-common-tools \
      cryptsetup \
      blkid \
      util-linux-misc \
      psmisc \
      python3 \
      py3-aiohttp

COPY rootfs /

# host_pid means we are never PID 1, so s6-overlay's /init cannot be used;
# the Python process supervises smbd/nmbd itself.
ENV PYTHONPATH=/usr/src/app PYTHONUNBUFFERED=1
WORKDIR /usr/src/app
ENTRYPOINT []
CMD ["python3", "-m", "luks_samba", "run"]
