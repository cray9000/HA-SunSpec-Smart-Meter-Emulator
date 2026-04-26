ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apk add --no-cache python3 py3-pip

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

COPY app /app/app
COPY run.sh /
COPY entity_map.yaml /defaults/entity_map.yaml
COPY meters.yaml /defaults/meters.yaml
COPY entity_map_sm1.yaml /defaults/entity_map_sm1.yaml
COPY entity_map_sm2.yaml /defaults/entity_map_sm2.yaml
COPY entity_map_sm3.yaml /defaults/entity_map_sm3.yaml
COPY entity_map_sm4.yaml /defaults/entity_map_sm4.yaml
COPY entity_map_sm5.yaml /defaults/entity_map_sm5.yaml
COPY entity_map_sm6.yaml /defaults/entity_map_sm6.yaml

RUN chmod a+x /run.sh
CMD [ "/run.sh" ]
