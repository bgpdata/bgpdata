"""
BGPDATA - BGP Data Collection and Analytics Service

This software is part of the BGPDATA project, which is designed to collect, process, and analyze BGP data from various sources.
It helps researchers and network operators get insights into their network by providing a scalable and reliable way to analyze and inspect historical and live BGP data from Route Collectors around the world.

Author: Robin Röper

© 2024 BGPDATA. All rights reserved.

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:
1. Redistributions of source code must retain the above copyright notice, this list of conditions, and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions, and the following disclaimer in the documentation and/or other materials provided with the distribution.
3. Neither the name of BGPDATA nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""
from confluent_kafka import KafkaError, Consumer, TopicPartition, KafkaException
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from protocols.bmp import BMPv3
from bs4 import BeautifulSoup
import queue as queueio
from io import BytesIO
import threading
import rocksdbpy
import fastavro
import requests
import logging
import asyncio
import bgpkit
import signal
import select
import socket
import struct
import time
import json
import os

# Get the hostname and process ID
hostname = socket.gethostname()  # Get the machine's hostname

# Get the log level from an environment variable, default to INFO
log_level = os.getenv('LOG_LEVEL', 'INFO').upper()

# Configure logging
logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
logger.setLevel(getattr(logging, log_level, logging.INFO))  # Set logger level

# Get list of collectors
routeviews_collectors = [tuple(collector.strip().split(':')) for collector in (
    os.getenv('ROUTEVIEWS_COLLECTORS') or '').split(',') if collector]
ris_collectors = [collector.strip() for collector in (
    os.getenv('RIS_COLLECTORS') or '').split(',') if collector]
openbmp_collectors = [tuple(collector.split(':')) for collector in (
    os.getenv('OPENBMP_COLLECTORS') or '').split(',') if collector]

# Route Views Kafka Consumer configuration
routeviews_consumer_conf = {
    'bootstrap.servers': 'stream.routeviews.org:9092',
    'group.id': f"bgpdata-{hostname}",
    'partition.assignment.strategy': 'roundrobin',
    'enable.auto.commit': False,
    'security.protocol': 'SASL_SSL',
    'sasl.mechanism': 'PLAIN',
    'sasl.username': os.getenv('ROUTEVIEWS_USERNAME'),
    'sasl.password': os.getenv('ROUTEVIEWS_PASSWORD'),
    'fetch.max.bytes': 50 * 1024 * 1024, # 50 MB
    'session.timeout.ms': 30000,  # For stable group membership
}

# RIS Kafka Consumer configuration
ris_consumer_conf = {
    'bootstrap.servers': 'node01.kafka-pub.ris.ripe.net:9094,node02.kafka-pub.ris.ripe.net:9094,node03.kafka-pub.ris.ripe.net:9094',
    'group.id': f"bgpdata-{hostname}",
    'partition.assignment.strategy': 'roundrobin',
    'enable.auto.commit': False,
    'security.protocol': 'SASL_SSL',
    'sasl.mechanism': 'PLAIN',
    'sasl.username': os.getenv('RIS_USERNAME'),
    'sasl.password': os.getenv('RIS_PASSWORD'),
    'fetch.max.bytes': 50 * 1024 * 1024, # 50 MB
    'session.timeout.ms': 30000,  # For stable group membership
}

# RIS Avro Encoding schema
ris_avro_schema = {
    "type": "record",
    "name": "RisLiveBinary",
    "namespace": "net.ripe.ris.live",
    "fields": [
        {
            "name": "type",
            "type": {
                "type": "enum",
                "name": "MessageType",
                "symbols": ["STATE", "OPEN", "UPDATE", "NOTIFICATION", "KEEPALIVE"],
            },
        },
        {"name": "timestamp", "type": "long"},
        {"name": "host", "type": "string"},
        {"name": "peer", "type": "bytes"},
        {
            "name": "attributes",
            "type": {"type": "array", "items": "int"},
            "default": [],
        },
        {
            "name": "prefixes",
            "type": {"type": "array", "items": "bytes"},
            "default": [],
        },
        {"name": "path", "type": {"type": "array", "items": "long"}, "default": []},
        {"name": "ris_live", "type": "string"},
        {"name": "raw", "type": "string"},
    ],
}


def on_assign(consumer, partitions, db):
    """
    Callback function to handle partition assigning/rebalancing.

    This function is called when the consumer's partitions are assigned/rebalanced. It logs the
    assigned partitions and handles any errors that occur during the rebalancing process.

    Args:
        consumer: The Kafka consumer instance.
        partitions: A list of TopicPartition objects representing the newly assigned partitions.
        db: The RocksDB database.
    """
    try:
        if partitions[0].error:
            logger.error(f"Rebalance error: {partitions[0].error}")
        else:
            # Set the offset for each partition
            for partition in partitions:
                last_offset = db.get(
                    f'offsets_{partition.topic}_{partition.partition}'.encode('utf-8')) or None
                
                # If the offset is stored, set it
                if last_offset is not None:
                    # +1 because we start from the next message
                    partition.offset = int.from_bytes(last_offset, byteorder='big') + 1
                
                # Log the assigned offset
                logger.info(
                    f"Assigned offset for partition {partition.partition} of {partition.topic} to {partition.offset}")
                
            # Assign the partitions to the consumer
            consumer.assign(partitions)
    except Exception as e:
        logger.error(f"Error handling assignment: {e}", exc_info=True)


def rib_task(queue, db, status, collectors, provider, events):
    """
    Task to inject RIB messages from MRT Data Dumps into the queue.

    Args:
        queue (queue.Queue): The queue to add the messages to.
        db (rocksdbpy.DB): The RocksDB database.
        status (dict): A dictionary containing the following keys:
            - time_lag (dict): A dictionary containing the current time lag of messages for each host.
            - time_preceived (dict): A dictionary containing the latest read timestamp for each host.
            - bytes_sent (int): The number of bytes sent since the last log.
            - bytes_received (int): The number of bytes received since the last log.
            - activity (str): The current activity of the collector.
        collectors (list): A list of tuples containing the host and URL of the RIB Data Dumps.
        provider (str): The provider of the MRT Data Dumps.
        events (dict): A dictionary containing the following keys:
            - route-views_injection (threading.Event): The event to wait for before starting.
            - route-views_provision (threading.Event): The event to wait for before starting.
            - ris_injection (threading.Event): The event to wait for before starting.
            - ris_provision (threading.Event): The event to wait for before starting.
    """

    # If the event is set, the provider is already initialized, skip
    if events[f"{provider}_provision"].is_set():
        return

    # Set the activity
    status['activity'] = "RIB_DUMP"
    logger.info(f"Initiating RIB Injection from {provider} collectors...")

    try:
        for host, url in collectors:
            logger.info(f"Injecting RIB from {provider} of {host} via {url}")

            batch = []

            # Initialize the timestamp if it's not set
            if db.get(f'timestamps_{host}'.encode('utf-8')) is None:
                db.set(f'timestamps_{host}'.encode('utf-8'), b'\x00\x00\x00\x00\x00\x00\x00\x00')  # Store 0 as the initial value

            while True:
                try:
                    # Parse the RIB Data Dump via BGPKit
                    # Learn more at https://bgpkit.com/
                    parser = bgpkit.Parser(url=url, cache_dir='.cache')

                    for elem in parser:
                        # Decode the stored timestamp
                        stored_timestamp = struct.unpack('>d', db.get(f'timestamps_{host}'.encode('utf-8')))[0]

                        # Update the timestamp if it's the freshest
                        if float(elem['timestamp']) > stored_timestamp:
                            db.set(f'timestamps_{host}'.encode('utf-8'), struct.pack('>d', float(elem['timestamp'])))

                        # Construct the BMP message
                        messages = BMPv3.construct(
                            host,
                            elem['peer_ip'],
                            elem['peer_asn'],
                            elem['timestamp'],
                            "UPDATE",
                            [
                                [int(asn) for asn in part[1:-1].split(',')] if part.startswith('{') and part.endswith('}')
                                else int(part)
                                for part in elem['as_path'].split()
                            ],
                            elem['origin'],
                            [
                                # Only include compliant communities with 2 or 3 parts that are all valid integers
                                [int(part) for part in comm.split(
                                    ":")[1:] if part.isdigit()]
                                for comm in (elem.get("communities") or [])
                                if len(comm.split(":")) in {2, 3} and all(p.isdigit() for p in comm.split(":")[1:])
                            ],
                            [
                                {
                                    "next_hop": elem["next_hop"],
                                    "prefixes": [elem["prefix"]]
                                }
                            ],
                            [],
                            None,
                            0
                        )

                        # Update the bytes received counter
                        status['bytes_received'] += sum(len(message) for message in messages)

                        # Add the messages to the batch
                        batch.extend(messages)

                    break  # Exit retry loop when successful

                except Exception as e:
                    logger.warning(
                        f"Retrieving RIB from {provider} {host} via {url} failed, retrying...", exc_info=True)
                    time.sleep(10)  # Wait 10 seconds before retrying

            # Add the messages to the queue
            for message in batch:
                queue.put((message, 0, provider, host, -1))

    except Exception as e:
        logger.error(
            f"Error injecting RIB from {provider} collectors: {e}", exc_info=True)
        raise e

    events[f"{provider}_injection"].set()


def kafka_task(configuration, collectors, topics, queue, db, status, batch_size, provider, events):
    """
    Task to poll a batch of messages from Kafka and add them to the queue.

    Args:
        configuration (dict): The configuration of the Kafka consumer.
        collectors (list): A list of tuples containing the host and topic of the collectors.
        topics (list): A list of topics to subscribe to.
        queue (queue.Queue): The queue to add the messages to.
        db (rocksdbpy.DB): The RocksDB database.
        status (dict): A dictionary containing the following keys:
            - time_lag (dict): A dictionary containing the current time lag of messages for each host.
            - time_preceived (dict): A dictionary containing the latest read timestamp for each host.
            - bytes_sent (int): The number of bytes sent since the last log.
            - bytes_received (int): The number of bytes received since the last log.
            - activity (str): The current activity of the collector.
        batch_size (int): Number of messages to fetch at once.
        provider (str): The provider of the messages.
        events (dict): A dictionary containing the following keys:
            - route-views_injection (threading.Event): The event to wait for before starting.
            - route-views_provision (threading.Event): The event to wait for before starting.
            - ris_injection (threading.Event): The event to wait for before starting.
            - ris_provision (threading.Event): The event to wait for before starting.
    """

    # Wait for possible RIB injection to finish
    for key in events.keys():
        if key.endswith("_injection"):
            events[key].wait()

    # Set the activity
    status['activity'] = "BMP_STREAM"
    logger.info(f"Subscribing to {provider} Kafka Consumer...")

    # Create Kafka Consumer
    consumer = Consumer(configuration)

    # Subscribe to Kafka Consumer
    consumer.subscribe(
        topics,
        on_assign=lambda c, p: on_assign(c, p, db),
        on_revoke=lambda c, p: logger.info(
            f"Revoked partitions: {[part.partition for part in p]}")
    )

    # If RIBs are injected but not yet provisioned
    if not events[f"{provider}_provision"].is_set():
        # Seek to desired offsets based on timestamps
        # Define a time delta (e.g., 15 minutes)
        time_delta = timedelta(minutes=15)

        # Keep track of the oldest timestamp for each topic
        # Why? In case multiple collectors stream to the same topic
        oldest_timestamps = {}

        for host, topic in collectors:
            # Assure the oldest timestamp for the topic (see comment above)
            oldest_timestamps[topic] = min(
                oldest_timestamps.get(
                    topic,
                    struct.unpack('>d', db.get(f'timestamps_{host}'.encode('utf-8')) or b'\x00\x00\x00\x00\x00\x00\x00\x00')[0]
                ),
                struct.unpack('>d', db.get(f'timestamps_{host}'.encode('utf-8')) or b'\x00\x00\x00\x00\x00\x00\x00\x00')[0]
            )

        for host, topic in collectors:
            # Get the oldest timestamp for the topic
            timestamp = datetime.fromtimestamp(oldest_timestamps[topic])

            # Calculate the target time
            target_time = timestamp - time_delta
            # Convert to milliseconds
            target_timestamp_ms = int(target_time.timestamp() * 1000)

            # Get metadata to retrieve all partitions for the topic
            metadata = consumer.list_topics(topic, timeout=10)
            partitions = metadata.topics[topic].partitions.keys()

            # Get offsets based on the timestamp
            offsets = consumer.offsets_for_times([TopicPartition(topic, p, target_timestamp_ms) for p in partitions])

            # Check if the offset is valid and not -1 (which means no valid offset was found for the given timestamp)
            valid_offsets = [tp for tp in offsets if tp.offset != -1]

            if valid_offsets:
                # Apply the offsets to RocksDB
                for tp in valid_offsets:
                    db.set(
                        f'offsets_{tp.topic}_{tp.partition}'.encode('utf-8'),
                        tp.offset.to_bytes(16, byteorder='big')
                    )

                # Inform the consumer about the assigned offsets
                consumer.assign(valid_offsets)
            else:
                # No valid offsets found for the given timestamp
                raise Exception("No valid offsets found for the given timestamp")

            # Mark Consumer as provisioned
            events[f"{provider}_provision"].set()

            # Wait for all consumers to be provisioned
            for key in events.keys():
                if key.endswith("_provision"):
                    events[key].wait()

            # Mark the RIBs injection as fulfilled
            db.set(b'ready', b'\x01')

    # Log the provision
    logger.info(f"Subscribed to {provider} Kafka Consumer")

    # Poll messages from Kafka
    while True:
        # Poll a batch of messages
        msgs = consumer.consume(batch_size, timeout=0.1)

        if not msgs:
            continue

        for msg in msgs:
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    # We are too fast for Kafka, sleep a bit
                    logger.info(f"End of partition reached: {msg.error()}")
                    time.sleep(1)
                    continue
                elif msg.error().code() == KafkaError._OFFSET_OUT_OF_RANGE:
                    # We were too slow for Kafka, this needs to be fixed manually
                    logger.critical("Offset out of range error encountered")
                    raise KafkaException(msg.error())
                else:
                    logger.error(f"Kafka error: {msg.error()}", exc_info=True)
                    raise KafkaException(msg.error())

            # Process the message
            value = msg.value()
            topic = msg.topic()
            offset = msg.offset()
            partition = msg.partition()

            # Initialize the messages list
            messages = []

            # Update the bytes received counter
            status['bytes_received'] += len(value)

            match provider:
                case 'route-views':
                    # Skip the raw binary header (we don't need the fields)
                    value = value[76 + struct.unpack("!H", value[54:56])[
                        0] + struct.unpack("!H", value[72:74])[0]:]

                    # TODO (1): Skip messages from unknown collectors.

                    # TODO (2): Parse the message and replace the peer_distinguisher with our own hash representation
                    #           Of the Route Views Collector name (SHA256) through the BMPv3.construct() function (e.g. the host).

                    # TODO (3): We need to keep track of the timestamp of the message
                    #           We do this to be able to show the time lag of the messages.

                    # HACK: Using a mock timestamp for initial testing
                    timestamp = datetime.now()

                    # HACK: Dummy approximated time lag preceived by the consumer
                    status['time_lag']["example.host"] = datetime.now() - datetime.fromtimestamp(timestamp)

                    # HACK: Untempered message for now
                    messages.append(value)
                case 'ris':
                    # Remove the first 5 bytes (we don't need them)
                    value = value[5:]

                    # Parse the Avro encoded exaBGP message
                    parsed = fastavro.schemaless_reader(
                        BytesIO(value), ris_avro_schema)
                    # Cast from int to datetime float
                    timestamp = parsed['timestamp'] / 1000
                    host = parsed['host']  # Extract Host

                    # Skip unknown collectors
                    # TODO: We should probably log this, but not as an error, and not for every message
                    if db.get(f'timestamps_{host}'.encode('utf-8')) is None:
                        continue
                    
                    # Update the approximated time lag preceived by the consumer
                    status['time_lag'][host] = datetime.now() - datetime.fromtimestamp(timestamp)
                    status['time_preceived'][host] = datetime.fromtimestamp(timestamp)

                    # Skip messages before the ingested collector's RIB or before the collector was seen
                    latest_route = struct.unpack('>d', db.get(f'timestamps_{host}'.encode('utf-8')) or b'\x00\x00\x00\x00\x00\x00\x00\x00')[0]
                    if timestamp < latest_route:
                        # TODO: We are estimating the time gap between the message and the ingested RIB very statically,
                        #       but we should approach this more accurately, e.g. approximate the time gap through reverse graph analysis.
                        continue

                    # Parse to BMP messages and add to the queue
                    # JSON Schema: https://ris-live.ripe.net/manual/
                    marshal = json.loads(parsed['ris_live'])

                    messages.extend(BMPv3.construct(
                        host,
                        marshal['peer'],
                        int(marshal['peer_asn']), # Cast to int from string
                        marshal['timestamp'],
                        marshal['type'],
                        marshal.get('path', []),
                        marshal.get('origin', 'INCOMPLETE'),
                        marshal.get('community', []),
                        marshal.get('announcements', []),
                        marshal.get('withdrawals', []),
                        marshal.get('state', None),
                        marshal.get('med', None)
                    ))

            for message in messages:
                queue.put((message, offset, provider, topic, partition))


def sender_task(queue, host, port, db, status):
    """
    Task to transmit messages from the queue to the TCP socket.
    Only updates offset in RocksDB once message is successfully sent.

    Args:
        queue (queue.Queue): The queue containing the messages to send.
        host (str): The host of the OpenBMP collector.
        port (int): The port of the OpenBMP collector.
        db (rocksdbpy.DB): The RocksDB database to store the offset.
        status (dict): A dictionary containing the current status and statistics.
    """
    sent_message = False
    backpressure_threshold = 1.0  # Threshold in seconds to detect backpressure
    send_delay = 0.0  # Initial delay between sends

    try:
        with socket.create_connection((host, port), timeout=60) as sock:
            logger.info(f"Connected to OpenBMP collector at {host}:{port}")

            while True:
                try:
                    # Ensure the connection is alive
                    ready_to_read, _, _ = select.select([sock], [], [], 0)
                    if ready_to_read and not sock.recv(1, socket.MSG_PEEK):
                        raise ConnectionError("TCP connection closed by the peer")

                    start_time = time.time()  # Start timing the send operation

                    # Get the message from the queue
                    message, offset, _, topic, partition = queue.get()

                    # Send the message
                    sock.sendall(message)
                    status['bytes_sent'] += len(message)

                    # Measure the time taken for sending the message
                    send_time = time.time() - start_time
                    if send_time > backpressure_threshold:
                        logger.warning(f"Detected backpressure: sending took {send_time:.2f}s")
                        send_delay = min(send_delay + 0.1, 5.0)  # Increase delay up to 5 seconds
                    else:
                        send_delay = max(send_delay - 0.05, 0.0)  # Gradually reduce delay if no backpressure

                    # Apply the delay before the next send if needed
                    if send_delay > 0:
                        time.sleep(send_delay)

                    if not sent_message:
                        sent_message = True  # Mark that a message has been sent
                        db.set(b'started', b'\x01')

                    if partition != -1:
                        key = f'offsets_{topic}_{partition}'.encode('utf-8')
                        db.set(key, offset.to_bytes(16, byteorder='big'))

                    queue.task_done()  # Mark the message as processed

                except queueio.Empty:
                    time.sleep(0.1)  # Sleep a bit if the queue is empty
                except ConnectionError as e:
                    logger.error("TCP connection lost", exc_info=True)
                    raise e
                except Exception as e:
                    logger.error("Error sending message over TCP", exc_info=True)
                    raise e

    except Exception as e:
        logger.error(f"Connection to OpenBMP collector at {host}:{port} failed", exc_info=True)
        raise e


def logging_task(status, queue, db, routeviews_hosts, ris_hosts):
    """
    Task to periodically log the most recent timestamp, time lag, current poll interval, and consumption rate.
    This task runs within the main event loop.

    Args:
        status (dict): A dictionary containing the following keys:
            - time_lag (dict): A dictionary containing the current time lag of messages for each host.
            - time_preceived (dict): A dictionary containing the latest read timestamp for each host.
            - bytes_sent (int): The number of bytes sent since the last log.
            - bytes_received (int): The number of bytes received since the last log.
            - activity (str): The current activity of the collector.
        queue (queue.Queue): The queue containing the messages to send.
        db (rocksdbpy.DB): The RocksDB database.
        routeviews_hosts (list): A list of the Route Views hosts.
        ris_hosts (list): A list of the RIS hosts.
    """
    while True:
        seconds = 10
        time.sleep(seconds)  # Sleep for n seconds before logging

        # Compute kbit/s
        bytes_sent = status['bytes_sent']
        bytes_received = status['bytes_received']
        # Convert bytes to kilobits per second
        kbps_sent = (bytes_sent * 8) / seconds / 1000
        kbps_received = (bytes_received * 8) / seconds / 1000

        if status['activity'] == "INITIALIZING":
            # Initializing
            logger.info(f"{status['activity']}{(17 - len(status['activity'])) * ' '}| ***")

        elif status['activity'] == "RIB_DUMP":
            # RIB Injection
            logger.info(f"{status['activity']}{(17 - len(status['activity'])) * ' '}| "
                        f"Receiving at ~{kbps_received:.2f} kbit/s, "
                        f"Sending at ~{kbps_sent:.2f} kbit/s, "
                        f"Queue size: ~{queue.qsize()}")
            
        elif status['activity'] == "BMP_STREAM":
            # Kafka Polling
            logger.info(f"{status['activity']}{(17 - len(status['activity'])) * ' '}| "
                        f"Receiving at ~{kbps_received:.2f} kbit/s, "
                        f"Sending at ~{kbps_sent:.2f} kbit/s, "
                        f"Queue size: ~{queue.qsize()}")
        
            for host in routeviews_hosts + ris_hosts:
                time_lag = status['time_lag'].get(host, timedelta(0))
                latest_route = struct.unpack('>d', db.get(f'timestamps_{host}'.encode('utf-8')) or b'\x00\x00\x00\x00\x00\x00\x00\x00')[0]
                hours, remainder = divmod(time_lag.total_seconds(), 3600)
                minutes, seconds = divmod(remainder, 60)
                logger.debug(f"{status['activity']}{(16 - len(status['activity'])) * ' '}| "
                             f"{host}{(22 - len(host)) * ' '}| "
                             f"At: ~{status['time_preceived'].get(host, '(not measured yet)').strftime('%Y-%m-%d %H:%M:%S') if host in status['time_preceived'] else '(not measured yet)'}, "
                             f"Time lag: ~{int(hours)}h {int(minutes)}m {int(seconds)}s, "
                             f"Sends after: {datetime.fromtimestamp(latest_route).strftime('%Y-%m-%d %H:%M:%S')}")
                
        elif status['activity'] == "TERMINATING":
            # Terminating
            logger.info(f"{status['activity']}{(17 - len(status['activity'])) * ' '}| "
                        f"Receiving at ~{kbps_received:.2f} kbit/s, "
                        f"Sending at ~{kbps_sent:.2f} kbit/s, "
                        f"Queue size: ~{queue.qsize()}")

        # Reset trackers
        status['bytes_sent'] = 0
        status['bytes_received'] = 0


async def main():
    """
    Main entry point for the BGPDATA collector service.

    This function coordinates the initialization and execution of multiple tasks that:
    1. Sets up and configures a RocksDB database for storing Kafka offsets and state tracking.
    2. Creates and manages a queue for buffering BGP messages to be sent to an OpenBMP collector.
    3. Initializes Kafka consumers for RIS and Route Views data sources, with configured batch polling.
    4. Handles MRT RIB data injection for synchronization and recovery from RIB dump files.
    5. Establishes TCP connections for transmitting processed BGP messages to OpenBMP collectors.
    6. Logs statistics and tracks system performance, such as message processing rates and time lag.
    7. Monitors and gracefully handles shutdowns, ensuring proper resource cleanup.

    This function runs indefinitely and can handle interruptions or unexpected errors, resuming
    from the last known state using RocksDB for persistence.

    Note:
        - The function is designed to be fault-tolerant, with retry mechanisms and event handling.
        - It registers signal handlers to manage graceful exits on receiving termination signals.
    """

    # Log the start
    logger.info("Initializing")

    # Wait to not stress the system
    time.sleep(5)

    QUEUE_SIZE = 10000000 # Number of messages to queue to the OpenBMP collector (1M is ~1GB Memory)
    BATCH_SIZE = 100000   # Number of messages to fetch at once from Kafka

    # Log the collectors
    logger.info(f"RIS collectors: {ris_collectors}")
    logger.info(f"Route Views collectors: {routeviews_collectors}")
    logger.info(f"OpenBMP collectors: {openbmp_collectors}")

    try:
        # Get the running loop
        loop = asyncio.get_running_loop()

        # Create RocksDB database
        db = rocksdbpy.open_default("rocks.db")

        # Define queue with a limit
        queue = queueio.Queue(maxsize=QUEUE_SIZE)

        # Initialize a status dictionary for logging and system performance tracking
        status = {
            'time_lag': {},                  # Initialize time lag
            'time_preceived': {},            # Initialize time preceived
            'bytes_sent': 0,                 # Initialize bytes sent counter
            'bytes_received': 0,             # Initialize bytes received counter
            'activity': "INITIALIZING",      # Initialize activity
        }

        # Create readyness events
        events = {}

        if len(routeviews_collectors) > 0:
            events["route-views_injection"] = threading.Event()
            events["route-views_provision"] = threading.Event()

        if len(ris_collectors) > 0:
            events["ris_injection"] = threading.Event()
            events["ris_provision"] = threading.Event()

        # Define shutdown function
        def shutdown(signum, _=None):
            # Set the activity
            status['activity'] = "TERMINATING"

            # Log the shutdown signal and frame information
            logger.warning(
                f"Shutdown signal ({signum}) received, exiting...")
            
            # Close the database
            db.close()

            # Shutdown the executor
            executor.shutdown(wait=False)

            # Exit the program
            os._exit(signum)

        # Register signal handlers
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, shutdown)

        # Create a ThreadPoolExecutor for sender tasks
        workers = (2 if len(ris_collectors) > 0 else 0) + \
            (2 if len(routeviews_collectors) > 0 else 0) + len(openbmp_collectors) + 1
        executor = ThreadPoolExecutor(max_workers=workers)

        # Whether the RIBs injection started
        started = True if db.get(
            b'started') == b'\x01' else False
        # Whether the RIBs injection finished
        ready = True if db.get(
            b'ready') == b'\x01' else False

        # Set the events
        if started and ready:
            if len(routeviews_collectors) > 0:
                events['route-views_injection'].set()
                events['route-views_provision'].set()
            if len(ris_collectors) > 0:
                events['ris_injection'].set()
                events['ris_provision'].set()

        elif started and not ready:
            # Database is corrupted, we need to exit
            logger.error("RIBs injection is corrupted, exiting...")
            raise Exception("Database is corrupted, exiting...")

        if len(routeviews_collectors) > 0:
            # HACK: For Route Views, we need to manually fetch the latest RIBs
            rv_rib_urls = []
            for host, _ in routeviews_collectors:
                if host == "route-views2":
                    # Route Views 2 is hosted on the root folder
                    index = f"https://archive.routeviews.org/bgpdata/{datetime.now().year}.{datetime.now().month}/RIBS/"
                else:
                    # Construct the URL for the latest RIBs
                    index = f"https://archive.routeviews.org/{host}/bgpdata/{datetime.now().year}.{datetime.now().month}/RIBS/"

                # Crawl the index page to find the latest RIB file (with beautifulsoup its also a apache file server)
                response = requests.get(index, timeout=30)
                response.raise_for_status()
                soup = BeautifulSoup(response.text, 'html.parser')
                latest_rib = soup.find_all('a')[-1].text
                rv_rib_urls.append(f"{index}{latest_rib}")

        # Initialize futures
        futures = []

        # RIB Tasks
        if len(routeviews_collectors) > 0:
            # Only if there are Route Views collectors
            future = loop.run_in_executor(executor, rib_task, queue, db, status, list(zip(list(set(
                [f"{host}.routeviews.org" for host, _ in routeviews_collectors])), rv_rib_urls)), 'route-views', events)
            futures.append(asyncio.wrap_future(future))

        if len(ris_collectors) > 0:  
            # Only if there are RIS collectors
            future = loop.run_in_executor(executor, rib_task, queue, db, status, list(zip(list(set(
                [f"{host}.ripe.net" for host in ris_collectors])), [f"https://data.ris.ripe.net/{i}/latest-bview.gz" for i in ris_collectors])), 'ris', events)
            futures.append(asyncio.wrap_future(future))

        # Kafka Tasks
        if len(routeviews_collectors) > 0:
            # Only if there are Route Views collectors
            future = loop.run_in_executor(executor, kafka_task, routeviews_consumer_conf, list(zip([f"{host}.routeviews.org" for host, _ in routeviews_collectors],
                [f'routeviews.{host}.{peer}.bmp_raw' for host, peer in routeviews_collectors])), [f'routeviews.{host}.{peer}.bmp_raw' for host, peer in routeviews_collectors], queue, db, status, BATCH_SIZE, 'route-views', events)
            futures.append(asyncio.wrap_future(future))

        if len(ris_collectors) > 0:
            # Only if there are RIS collectors
            future = loop.run_in_executor(executor, kafka_task, ris_consumer_conf, list(zip([f"{host}.ripe.net" for host in ris_collectors],
                ['ris-live' for _ in ris_collectors])), ['ris-live'], queue, db, status, BATCH_SIZE, 'ris', events)
            futures.append(asyncio.wrap_future(future))

        # Sender Tasks
        for host, port in openbmp_collectors:
            future = loop.run_in_executor(
                executor, sender_task, queue, host, port, db, status)
            futures.append(asyncio.wrap_future(future))

        # Logging Task
        future = loop.run_in_executor(executor, logging_task, status, queue, db, [f"{host}.routeviews.org" for host, _ in routeviews_collectors], [f"{host}.ripe.net" for host in ris_collectors])
        futures.append(asyncio.wrap_future(future))

        # Keep loop alive
        await asyncio.gather(*futures)
    except Exception as e:
        logger.critical("Fatal error", exc_info=True)
    finally:
        shutdown(1)
