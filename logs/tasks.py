# myapp/tasks.py

from __future__ import absolute_import, unicode_literals
from celery import shared_task
from kombu import Connection, Exchange, Queue
import logging
import traceback

from django.conf import settings

if settings.DEBUG:
    from pydebugger.debug import debug
else:
    def debug(*args, **kwargs):
        return None

logger = logging.getLogger(__name__)

@shared_task
def send_log_to_rabbitmq(log_message):
    try:
        debug(log_message = log_message)
        exchange = Exchange('django', type='fanout', durable=True)
        queue = Queue(name='', exchange=exchange, routing_key='', durable=True)
    
        #with Connection('amqp://syslog:DTPdev@2022@localhost:5672//') as conn:
        with Connection('amqp://localhost:5672//') as conn:
            with conn.Producer(serializer='json') as producer:
                producer.publish(
                    log_message,
                    exchange=exchange,
                    routing_key='',
                    delivery_mode=2,
                    mandatory=True,
                    priority=0,
                    expiration=None,
                    headers=None,
                    retry=True,
                    declare=[queue],
                )
    
        logger.info('Sent log message to RabbitMQ: %s', log_message)
        return True
    except:
        return traceback.format_exc()

