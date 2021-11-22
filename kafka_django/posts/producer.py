from kafka import KafkaProducer
import json



def producer(topic, data):
    try:
        producer = KafkaProducer(bootstrap_servers='127.0.0.1:9092'
        , value_serializer=lambda v: json.dumps(v).encode('utf-8')
        )
        producer.send(topic, data)
        return True
    except Exception:
        return False

