#!/usr/bin/env python3

# File: logs/handler.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-10-20
# Description: 
# License: MIT

import logging
import logging.handlers
import json
import os
import sys
import socket
# from django.utils import timezone
from django.conf import settings
from pathlib import Path
from datetime import datetime

if settings.DEBUG:
    from pydebugger.debug import debug
else:
    def debug(*args, **kwargs):
        return None

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'wgetpanel.settings')


class RabbitMQHandler1(logging.Handler):
    import pika
    import tenacity

    if not hasattr(settings, 'RABBITMQ_EXCHANGE_NAME'): settings.RABBITMQ_EXCHANGE_NAME = None
    if not hasattr(settings, 'RABBITMQ_EXCHANGE_TYPE'): settings.RABBITMQ_EXCHANGE_TYPE = None
    if not hasattr(settings, 'RABBITMQ_DURABLE'): settings.RABBITMQ_DURABLE = None
    if not hasattr(settings, 'RABBITMQ_USERNAME'): settings.RABBITMQ_USERNAME = None
    if not hasattr(settings, 'RABBITMQ_PASSWORD'): settings.RABBITMQ_PASSWORD = None
    if not hasattr(settings, 'RABBITMQ_HOST'): settings.RABBITMQ_HOST = None
    if not hasattr(settings, 'RABBITMQ_PORT'): settings.RABBITMQ_PORT = None


    def __init__(self, exchange_name='django', exchange_type='fanout', durable=True, username='guest', password='guest', host=None, port=None):
        super().__init__()
        self.exchange_name = settings.RABBITMQ_EXCHANGE_NAME or exchange_name
        self.exchange_type = settings.RABBITMQ_EXCHANGE_TYPE or exchange_type
        self.durable = settings.RABBITMQ_DURABLE or durable
        self.username = settings.RABBITMQ_USERNAME or username or 'guest'
        self.password = settings.RABBITMQ_PASSWORD or password or 'guest'
        self.host = settings.RABBITMQ_HOST or host or '127.0.0.1'
        self.port = settings.RABBITMQ_PORT or port or 5672
        self.connection = None
        self.channel = None
        self.connect()

    @tenacity.retry(wait=tenacity.wait_exponential(multiplier=1, min=4, max=10), stop=tenacity.stop_after_attempt(3), reraise=True)
    def connect(self):
        credentials = pika.PlainCredentials(self.username, self.password)
        parameters = pika.ConnectionParameters(host=self.host, port=self.port, credentials=credentials)
        self.connection = pika.BlockingConnection(parameters)
        self.channel = self.connection.channel()
        self.channel.exchange_declare(
            exchange=self.exchange_name,
            exchange_type=self.exchange_type,
            durable=self.durable,
        )

    def emit(self, record):
        log_entry = self.format(record)
        debug(log_entry = log_entry)
        self.channel.basic_publish(
            exchange=self.exchange_name,
            routing_key='',
            body=log_entry.encode('utf-8'),
        )

    def close(self):
        try:
            self.connection.close()
            super().close()
        except:
            pass

class RabbitMQHandler2(logging.Handler):
    import pika
    from tenacity import retry, stop_after_delay, wait_fixed

    def __init__(self, host = '127.0.0.1', port = 5672, username = 'syslog', password = 'DTPdev@2022@', exchange = 'django', routing_key = '', max_retries=3):
        logging.Handler.__init__(self)
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.exchange = exchange
        self.routing_key = routing_key
        self.max_retries = max_retries
        self.connection = None
        self.channel = None

    def connect(self):
        credentials = pika.PlainCredentials(self.username, self.password)
        parameters = pika.ConnectionParameters(self.host, self.port, '/', credentials, heartbeat=0)
        self.connection = pika.BlockingConnection(parameters)
        self.channel = self.connection.channel()
        self.channel.exchange_declare(exchange=self.exchange, durable=True, exchange_type='fanout')

    def close(self):
        if self.connection:
            self.connection.close()

    def emit(self, record):
        logentry = self.format(record)
        try:
            self.retry_send(logentry)
        except Exception as e:
            print(f'Error while sending log to RabbitMQ: {e}')

    @retry(stop=stop_after_delay(60), wait=wait_fixed(10))
    def retry_send(self, logentry):
        if not self.connection or self.connection.is_closed:
            self.connect()
        self.channel.basic_publish(
            exchange=self.exchange,
            routing_key=self.routing_key,
            body=logentry,
            properties=pika.BasicProperties(delivery_mode=2)
        )


class RabbitMQHandler(logging.Handler):
    def __init__(self):
        super().__init__()

    def emit(self, record):
        from . tasks import send_log_to_rabbitmq
        message = self.format(record)
        send_log_to_rabbitmq.delay({'message': message})

class AMQPHandler(logging.Handler):
    from amqp import Connection, Message
    def __init__(self, connection_url, exchange_name, exchange_type, username, password, durable):
        logging.Handler.__init__(self)
        self.exchange_name = exchange_name
        self.connection = Connection(connection_url, userid=username, password=password)
        self.channel = self.connection.channel()
        self.channel.exchange_declare(exchange_name, exchange_type, durable=durable)

    def emit(self, record):
        message = Message(self.format(record))
        self.channel.basic_publish(message, exchange=self.exchange_name)

class SysLogJSONHandler1(logging.handlers.SysLogHandler):
    def __init__(self, *args, **kwargs):
        self.env = kwargs.get('env')
        #self.server_host = kwargs.get('server_host')
        kwargs.pop('env', None)
        #kwargs.pop('server_host', None)
        super().__init__(*args, **kwargs)
        self.sock = None

    def send(self, message):
        if self.sock is None:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        message = json.dumps(message)
        if isinstance(message, str):
            message = message.encode('utf-8')
        self.sock.sendto(message, self.address)

    def emit(self, record):
        import re
        pattern = r"^\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2},\d{3}"
        timestamp = re.findall(pattern, self.format(record))
        if timestamp:
            timestamp = timestamp[0]
        else:
            timestamp = self.format(record)
        try:
            log_entry = {
                'timestamp': timestamp,
                'levelname': record.levelname,
                'pid': record.process,
                'tid': record.thread,
                'filename': record.filename,
                'lineno': record.lineno,
                'message': record.getMessage(),
                'logmessage': self.format(record),
                'env': self.env,
                #'client_ip': self.get_client_ip(),
                #'server_ip': self.get_server_ip(),
            }
            self.send_json(log_entry)
        except Exception:
            self.handleError(record)

    def send_json(self, log_entry):
        message = json.dumps(log_entry)
        self.send(message)

    def get_client_ip(self):
        try:
            return socket.gethostbyaddr(self.address[0])[0]
        except Exception:
            return None

    def get_server_ip(self):
        try:
            return socket.gethostbyname(self.server_host)
        except Exception:
            return None

class SysLogTextHandler(logging.handlers.SysLogHandler):
    def __init__(self, *args, **kwargs):
        self.env = kwargs.pop('env', 'production')
        self.app_name = kwargs.pop('app_name', 'django-gunicorn')
        super().__init__(*args, **kwargs)
        self.sock = None

    def emit(self, record):
        try:
            # 1. Calculation of Priority <PRI> of the default Facility and Severity level
            priority = self.encodePriority(self.facility, self.mapPriority(record.levelname))
            
            # 2. BSD/Syslog Classic Time Format (RFC 3164)
            # Example: Oct 20 12:59:46
            # %b = Month (abbreviation), %d = Date, %H:%M:%S = 24 hour time
            # In classic syslog, days 1-9 use an extra nonzero space (e.g., "Oct 5" instead of "Oct 05")
            # We use lstrip('0') then rjust(2) to comply with that.
            day = datetime.now().strftime("%d").lstrip('0').rjust(2)
            timestamp = datetime.now().strftime(f"%b {day} %H:%M:%S")
            
            # 3. Get a Hostname
            hostname = socket.gethostname()
            
            # 4. Retrieve the original message you want to log
            # Record.getMessage() ensures formatting strings (such as %s) are executed
            message = record.getMessage()

            # (Optional) Add file or thread info if necessary in the message text
            # If not needed, delete the formatting string [ENV/PID/etc] below.
            context_message = f"[{self.env}] [{record.filename}:{record.lineno}] {message}"

            # 5. Chain into RFC 3164 format
            # Format baku: <PRI>TIMESTAMP HOSTNAME APPNAME[PID]: MESSAGE
            syslog_msg = f"<{priority}>{timestamp} {hostname} {self.app_name}[{record.process}]: {context_message}"

            # Send the compiled log
            self.send(syslog_msg)
            
        except Exception:
            self.handleError(record)

    def send(self, message):
        # Create a UDP socket if not already created
        if self.sock is None:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            
        # Convert a Python string to byte format (UTF-8) for sockets
        if isinstance(message, str):
            message = message.encode('utf-8')
            
        # Send data byte via UDP to (Host, Port)
        self.sock.sendto(message, self.address)

if __name__ == '__main__':
    logger = logging.getLogger()
    logger.addHandler(RabbitMQHandler())
    logger.setLevel(logging.INFO)

    logger.info('Hello, World!')
