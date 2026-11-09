FROM ghcr.io/home-assistant/base-debian:trixie

ARG BUILD_VERSION=0.1.9
ARG BUILD_ARCH=local

LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}" \
  org.opencontainers.image.source="https://github.com/aoliveira07/tecnohead-audio-local" \
  org.opencontainers.image.description="TecnoHead Áudio Local"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=America/Sao_Paulo \
    APP_VERSION="${BUILD_VERSION}" \
    INGRESS_PORT=18199 \
    MEDIA_PORT=18198

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
      ca-certificates \
      ffmpeg \
      python3 \
      python3-pip \
      tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/tecnohead-audio-local-requirements.txt
COPY requirements-spotdl.txt /tmp/tecnohead-audio-local-requirements-spotdl.txt
RUN python3 -m pip install --break-system-packages --no-cache-dir \
      -r /tmp/tecnohead-audio-local-requirements.txt \
    && python3 -m pip install --break-system-packages --no-cache-dir --no-deps \
      -r /tmp/tecnohead-audio-local-requirements-spotdl.txt \
    && rm /tmp/tecnohead-audio-local-requirements.txt \
      /tmp/tecnohead-audio-local-requirements-spotdl.txt

COPY app /opt/tecnohead_audio_local/app
COPY web /opt/tecnohead_audio_local/web
COPY run.sh /run.sh

RUN chmod 0755 /run.sh \
    && mkdir -p /data/state /data/cache /media/tecnohead-audio-local

WORKDIR /opt/tecnohead_audio_local
ENV PYTHONPATH=/opt/tecnohead_audio_local

CMD ["/run.sh"]

