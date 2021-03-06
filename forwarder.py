#!/usr/bin/env python
# -*- coding: utf-8 -*-

# forwarder.py - forwards IoT sensor data from MQTT to InfluxDB
#
# Copyright (C) 2016 Michael Haas <haas@computerlinguist.org>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software Foundation,
# Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301  USA

import argparse
import paho.mqtt.client as mqtt
from influxdb import InfluxDBClient
import json
import re
import logging
import sys
import requests.exceptions


class MessageStore(object):
    def store_msg(self, node_name, measurement_name, value):
        raise NotImplementedError()


class InfluxStore(MessageStore):
    logger = logging.getLogger("forwarder.InfluxStore")

    def __init__(self, host, port, username, password_file, database):
        password = open(password_file).read().strip()
        self.influx_client = InfluxDBClient(
            host=host, port=port, username=username, password=password, database=database)
        # influx_client.create_database('sensors')

    def store_msg(self, node_name, measurement_name, data):
        if not isinstance(data, dict):
            raise ValueError('data must be given as dict!')
        influx_msg = {
            'measurement': measurement_name,
            'tags': {
                'sensor_node': node_name,
            },
            'fields': data
        }
        self.logger.debug("Writing InfluxDB point: %s", influx_msg)
        try:
            self.influx_client.write_points([influx_msg])
        except requests.exceptions.ConnectionError as e:
            self.logger.exception(e)


class MessageSource(object):
    def register_store(self, store):
        if not hasattr(self, '_stores'):
            self._stores = []
        self._stores.append(store)

    @property
    def stores(self):
        # return copy
        return list(self._stores)


class MQTTSource(MessageSource):
    logger = logging.getLogger("forwarder.MQTTSource")

    def __init__(self, host, port, username, password_file, client_id, transport, node_names, topic_prefix,
                 stringify_values_for_measurements):
        self.host = host
        self.port = int(port)
        self.username = username
        if password_file is not None:
            self.password = open(password_file).read().strip()
        self.client_id = client_id
        self.transport = transport
        self.node_names = node_names
        self.topic_prefix = topic_prefix
        self.stringify = stringify_values_for_measurements
        self._setup_handlers()

    def _setup_handlers(self):
        self.client = mqtt.Client(client_id=self.client_id, transport=self.transport)
        if self.username is not None:
            self.client.username_pw_set(self.username, password=self.password)

        # Construct a prefix topic path
        if self.topic_prefix:
            if not self.topic_prefix.startswith("/"):
                self.topic_prefix = "/" + self.topic_prefix
            if self.topic_prefix.endswith("/"):
                self.topic_prefix = self.topic_prefix[:-1]

        def on_connect(client, userdata, flags, rc):
            self.logger.info("Connected with result code  %s", rc)

            # subscribe to /node_name/wildcard
            for node_name in self.node_names:
                topic = "{topic_prefix}/{node_name}/#".format(topic_prefix=self.topic_prefix, node_name=node_name)
                self.logger.info(
                    "Subscribing to topic %s for node_name %s", topic, node_name)
                client.subscribe(topic)

        def on_message(client, userdata, msg):
            self.logger.debug(
                "Received MQTT message for topic %s with payload %s", msg.topic, msg.payload)
            token_pattern = r'(?:\w|-|\.)+'
            regex = re.compile(
                r"{topic_prefix}/(?P<node_name>{token_pattern})/(?P<measurement_name>{token_pattern})/?".format(
                    topic_prefix=self.topic_prefix, token_pattern=token_pattern))
            match = regex.match(msg.topic)
            if match is None:
                self.logger.warning(
                    "Could not extract node name or measurement name from topic %s", msg.topic)
                return
            node_name = match.group('node_name')
            if node_name not in self.node_names:
                self.logger.warning(
                    "Extract node_name %s from topic, but requested to receive messages for node_names %s", node_name,
                    str(self.node_names))
            measurement_name = match.group('measurement_name')

            value = msg.payload

            is_value_json_dict = False
            try:
                stored_message = json.loads(value)
                is_value_json_dict = isinstance(stored_message, dict)
            except ValueError:
                pass

            if is_value_json_dict:
                for key in stored_message.keys():
                    try:
                        stored_message[key] = float(stored_message[key])
                    except ValueError:
                        pass
            else:
                # if message is not a JSON DICT, only then check if we should stringify the value
                if measurement_name in self.stringify:
                    value = str(value)
                else:
                    try:
                        value = float(value)
                    except ValueError:
                        pass
                stored_message = {'value': value}

            for store in self.stores:
                store.store_msg(node_name, measurement_name, stored_message)

        self.client.on_connect = on_connect
        self.client.on_message = on_message

    def start(self):
        self.client.connect(self.host, self.port)
        # Blocking call that processes network traffic, dispatches callbacks and
        # handles reconnecting.
        # Other loop*() functions are available that give a threaded interface and a
        # manual interface.
        self.client.loop_forever()


def main():
    parser = argparse.ArgumentParser(
        description='MQTT to InfluxDB bridge for IOT data.')
    parser.add_argument('--mqtt-host', required=True, help='MQTT host')
    parser.add_argument('--mqtt-port', default="1883", help='MQTT port')
    parser.add_argument('--mqtt-user', required=False, help='MQTT username')
    parser.add_argument('--mqtt-pass-file', required=False, help='MQTT user password file')
    parser.add_argument('--mqtt-client-id', default="", help='MQTT client id')
    parser.add_argument('--mqtt-transport', default="tcp", help='MQTT transport')
    parser.add_argument('--mqtt-topic-prefix', default="", help='MQTT topic prefix')
    parser.add_argument('--influx-host', required=True, help='InfluxDB host')
    parser.add_argument('--influx-port', default="8086", help='InfluxDB port')
    parser.add_argument('--influx-user', required=True,
                        help='InfluxDB username')
    parser.add_argument('--influx-pass-file', required=True,
                        help='InfluxDB password file')
    parser.add_argument('--influx-db', required=True, help='InfluxDB database')
    parser.add_argument('--node-name', required=True,
                        help='Sensor node name', action="append")
    parser.add_argument('--stringify-values-for-measurements', required=False, default="",
                        help='Force str() on measurements of the given name', action="append")
    parser.add_argument('--verbose', help='Enable verbose output to stdout',
                        default=False, action='store_true')
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
    else:
        logging.basicConfig(stream=sys.stdout, level=logging.INFO)

    store = InfluxStore(host=args.influx_host, port=args.influx_port,
                        username=args.influx_user, password_file=args.influx_pass_file, database=args.influx_db)
    source = MQTTSource(host=args.mqtt_host, port=args.mqtt_port,
                        username=args.mqtt_user, password_file=args.mqtt_pass_file,
                        client_id=args.mqtt_client_id, transport=args.mqtt_transport,
                        node_names=args.node_name, topic_prefix=args.mqtt_topic_prefix,
                        stringify_values_for_measurements=args.stringify_values_for_measurements)
    source.register_store(store)
    source.start()


if __name__ == '__main__':
    main()
