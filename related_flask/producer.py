from kafka import KafkaProducer
import json



def producer(data):
    try:
        producer = KafkaProducer(bootstrap_servers='0.0.0.0:9092'
        , value_serializer=lambda v: json.dumps(v).encode('utf-8')
        )
        producer.send('posts', data)

        return True
    except Exception:
        return False

