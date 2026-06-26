ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.19
FROM $BUILD_FROM

RUN apk add --no-cache python3 py3-pip jq bash

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY app/ app/
COPY config/ config/
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
