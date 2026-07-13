
# Table of content
- [Table of content](#table-of-content)
- [Example consumers for The Green Village digital platform](#example-consumers-for-the-green-village-digital-platform)
  - [Architecture overview](#architecture-overview)
  - [Data schema](#data-schema)
  - [Python consumers](#python-consumers)

# Example consumers for The Green Village digital platform

## Architecture overview


| Service | URL |
|---|:---:|
| Kafka cluster in Confluent Cloud | pkc-e8mp5.eu-west-1.aws.confluent.cloud:9092 |
| Schema Registry in Confluent Cloud | https://psrc-1w11j.eu-central-1.aws.confluent.cloud |
| Kafka REST Proxy in Confluent Cloud | https://pkc-e8mp5.eu-west-1.aws.confluent.cloud:443 |
| Grafana Cloud | https://thegreenvillage.grafana.net/ |

The Kafka cluster in Confluent Cloud can be accessed with an API key and a API secret. The same API key and API secret can be used for the Kafka REST Proxy. The Schema Registry can be accessed with a read-only account credentials.
In order to run the examples below, make sure the following environment variables are set in your terminal.

```bash
export KAFKA_TGV_BOOTSTRAP_SERVERS=pkc-e8mp5.eu-west-1.aws.confluent.cloud:9092
export KAFKA_TGV_SCHEMA_REGISTRY_URL=https://psrc-1w11j.eu-central-1.aws.confluent.cloud
export KAFKA_TGV_REST_PROXY_URL=https://pkc-e8mp5.eu-west-1.aws.confluent.cloud:443

export KAFKA_TGV_API_KEY=
export KAFKA_TGV_API_SECRET=
export KAFKA_TGV_CLUSTER=
export KAFKA_TGV_SCHEMA_REGISTRY_USERNAME=
export KAFKA_TGV_SCHEMA_REGISTRY_PASSWORD=

# Topic used as a staging environment
export KAFKA_TGV_TOPIC=tud_gv_test

# Your unique consumer group has to be prefixed with your service account name.
# For example, for service account name "tud_gv_david_salek" the consumer group can be "tud_gv_david_salek_test"
export KAFKA_TGV_CONSUMER_GROUP=
```

## Data schema
The data schema can be found at our [wiki](https://thegreenvillage-wiki-bf9b02.gitlab.io/tgv_digital_platform/data_schema.html).


## Python consumers

Example consumers in Python are available here.
Using consumers in Python (or other languages) is a recommended way for production use.

You can run the examples from a terminal directly or using docker.
Make sure to choose a unique consumer group id for the consumer.

```bash
# Create a python virtual environment (optional)
python -m venv venv
. venv/bin/activate

pip install -U -r requirements.txt

# Run one of the consumer scripts.
python deserializing-consumer.py

```

```bash
docker build -t ccloud-green-village .

# Consume messages from the topic in one terminal.
docker run -e KAFKA_TGV_BOOTSTRAP_SERVERS=$KAFKA_TGV_BOOTSTRAP_SERVERS -e KAFKA_TGV_API_KEY=$KAFKA_TGV_API_KEY -e KAFKA_TGV_API_SECRET=$KAFKA_TGV_API_SECRET -e KAFKA_TGV_TOPIC=$KAFKA_TGV_TOPIC \
  -e KAFKA_TGV_SCHEMA_REGISTRY_URL=$KAFKA_TGV_SCHEMA_REGISTRY_URL -e KAFKA_TGV_SCHEMA_REGISTRY_USERNAME=$KAFKA_TGV_SCHEMA_REGISTRY_USERNAME -e KAFKA_TGV_SCHEMA_REGISTRY_PASSWORD=$KAFKA_TGV_SCHEMA_REGISTRY_PASSWORD \
  ccloud-green-village python deserializing-consumer.py
```
