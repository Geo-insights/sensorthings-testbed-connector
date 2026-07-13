from confluent_kafka import Consumer, KafkaError, KafkaException
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer
from confluent_kafka.schema_registry.error import SchemaRegistryError
from confluent_kafka.serialization import SerializationContext, MessageField
from confluent_kafka.error import SerializationError

import requests
from pprint import pformat

from projectsecrets import KAFKA_TGV_API_KEY, KAFKA_TGV_API_PASSWORD, KAFKA_TGV_BOOTSTRAP_SERVERS, KAFKA_TGV_SCHEMA_REGISTRY_URL, KAFKA_TGV_SCHEMA_REGISTRY_USERNAME, KAFKA_TGV_SCHEMA_REGISTRY_PASSWORD, KAFKA_TGV_CONSUMER_GROUP, KAFKA_TGV_CLIENT_ID_CONSUMER, KAFKA_TGV_TOPIC


if __name__ == '__main__':

    # SchemaRegistryClient will be needed to instantiate AvroDeserializer.
    conf = {
        'url': KAFKA_TGV_SCHEMA_REGISTRY_URL,
        'basic.auth.user.info': f"{KAFKA_TGV_SCHEMA_REGISTRY_USERNAME}:{KAFKA_TGV_SCHEMA_REGISTRY_PASSWORD}"
    }
    schema_registry_client = SchemaRegistryClient(conf)

    # Get the schema from the Schema Registry.
    r = requests.get(f"{KAFKA_TGV_SCHEMA_REGISTRY_URL}/subjects/{KAFKA_TGV_TOPIC}-value/versions/latest",
                     auth=(KAFKA_TGV_SCHEMA_REGISTRY_USERNAME, KAFKA_TGV_SCHEMA_REGISTRY_PASSWORD))
    print(r.status_code)
    schema_str = r.json()['schema']
    print(schema_str)

    # AvroDeserializer and SerializationContext will be needed to deserialize
    # individual messages when a regular Consumer is used.
    # (DeserializingConsumer takes care of the deserialization internally.)
    deserializer = AvroDeserializer(schema_registry_client,schema_str)
    ser_context = SerializationContext(KAFKA_TGV_TOPIC, MessageField.VALUE)

    # Consumer configuration
    # See https://github.com/edenhill/librdkafka/blob/master/CONFIGURATION.md
    conf = {
        # Choose a unique consumer group id and a client id.
        'group.id': KAFKA_TGV_CONSUMER_GROUP,
        'client.id': KAFKA_TGV_CLIENT_ID_CONSUMER,
        'bootstrap.servers': KAFKA_TGV_BOOTSTRAP_SERVERS,
        'sasl.mechanisms': 'PLAIN',
        'security.protocol': 'SASL_SSL',
        'sasl.username': KAFKA_TGV_API_KEY,
        'sasl.password': KAFKA_TGV_API_PASSWORD,
        'auto.offset.reset': 'latest',
        'enable.auto.commit': 'false'
    }

    # Create a Consumer instance.
    consumer = Consumer(conf)

    def print_assignment(consumer, partitions):
        print(f'Assignment: {partitions}')

    def no_offset_assignment (consumer, partitions):
        for p in partitions:
            p.offset = -1
        print('Assignment: ', partitions)
        consumer.assign(partitions)

    # Subscribe to the TOPIC.
    # N.B. If you just want to consume the most recent message, use on_assign=no_offset_assignment
    # If you want to continue where you left off, use on_assign=print_assignment
    consumer.subscribe([KAFKA_TGV_TOPIC], on_assign=no_offset_assignment)


    # Read messages from Kafka.
    try:
        while True:
            try:
                # Instead of using poll that returns messages one-by-one,
                # we use consume to receive a list of messages.
                # In this case, we receive a list of messages every time
                # there are 1000 new messages or 5 seconds elapse.
                msgs = consumer.consume(num_messages=1000, timeout=5.0)
                print(f"Received {len(msgs)} messages.")

            except KafkaError as e:
                print("KafkaError occurred, no offset will be committed: {}".format(e))
                continue

            # Loop over the batch of messages obtained from Kafka.
            # Individual messages will be deserialized.
            # Messages that cannot be deserialized will be ignored.
            # In case any other errors from Kafka occur, no offset will be commited to Kafka.
            msg_err = False
            values = []
            for msg in msgs:

                if msg.error():
                    # On error, break and do not commit any offset to Kafka.
                    msg_err = True
                    raise KafkaException(msg.error())
                    break

#                print('%s [%d] at offset %d' % (msg.TOPIC(), msg.partition(), msg.offset()))

                try:
                    value = deserializer(msg.value(), ser_context)
                    values.append(value)

                except SerializationError as e:
                    # Ignore the messages that cannot be deserialized.
                        print("SerializationError occurred, the message will be ignored and the offset will be committed: {}".format(e))
                        continue

                # On error, do not commit any offset to Kafka and try again.
                if msg_err:
                    continue

            # Process the whole batch of messages here at once.
            print(pformat(values))

            # Commit the last offset to Kafka on success.
            consumer.commit()

    except KeyboardInterrupt:
        print('%% Aborted by user\n')

    finally:
        # Close down consumer to commit final offsets.
        consumer.close()

