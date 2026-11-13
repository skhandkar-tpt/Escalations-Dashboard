# Escalations Dashboard - runs server.rb (live ServiceDesk Plus data)
FROM ruby:3.3-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && gem install webrick --no-document \
 && useradd --system --no-create-home app

WORKDIR /app
COPY server.rb ./
COPY public ./public

USER app
ENV HOST=0.0.0.0 PORT=8787
EXPOSE 8787
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
  CMD curl -fsS http://localhost:${PORT:-8787}/healthz || exit 1

CMD ["ruby", "server.rb"]
