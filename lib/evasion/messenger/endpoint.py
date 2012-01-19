# -*- coding: utf-8 -*-
"""
"""
import json
import uuid
import thread
import logging
import threading

import zmq
from zmq import ZMQError

from . import frames


class MessageOutError(Exception):
    """Raised for attempting to send an invalid message."""


class Transceiver(object):

    def __init__(self, config={}, message_handler=None):
        """Set up a receiver which connects to the messaging hub.

        :param config: This is a dict in the form::

            config = dict(
                incoming='tcp://localhost:15566', # default
                outgoing='tcp://localhost:15567',
                idle_timeout=1000, # milliseconds:
            )

        """
        self.log = logging.getLogger("evasion.messenger.endpoint.Transceiver")

        self.exitTime = False

        self.incoming = None # configured in main().
        self.incoming_uri = config.get("incoming", 'tcp://localhost:15566')
        self.log.info("Recieving on <%s>" % self.incoming_uri)

        self.outgoing_uri = config.get("outgoing", 'tcp://localhost:15567')
        self.log.info("Sending on <%s>" % self.outgoing_uri)

        self.idle_timeout = int(config.get("idle_timeout", 2000))
        self.log.info("Idle Timeout (ms): %d" % self.idle_timeout)

        self.message_handler = message_handler


    def main(self):
        """Running the message receiving loop and on idletime check the exit flag.
        """
        self.exitTime = False

        context = zmq.Context()
        incoming = context.socket(zmq.SUB)
        incoming.setsockopt(zmq.SUBSCRIBE, '')
        incoming.connect(self.incoming_uri)

        try:
            poller = zmq.Poller()
            poller.register(incoming, zmq.POLLIN)

            while not self.exitTime:
                try:
                    events = poller.poll(self.idle_timeout)

                except ZMQError as e:
                    # 4 = 'Interrupted system call'
                    self.log.info("main: sigint or other signal interrupt, exit time <%s>" % e)
                    break

                else:
                    if (events > 0):
                        msg = incoming.recv_multipart()
                        self.message_in(tuple(msg))
        finally:
            incoming.close()
            context.term()


    def start(self):
        """Set up zmq communication and start receiving messages from the hub.
        """
        def _main(notused):
            self.main()
        thread.start_new(_main, (0,))


    def stop(self):
        """Stop receiving messages from the hub and clean up.
        """
        self.log.info("stop: shutting down messaging.")
        self.exitTime = True
        if self.incoming:
            self.incoming.close()
        self.log.info("stop: done.")


    def message_out(self, message):
        """This sends a message to the messagehub for dispatch to all connected
        endpoints.

        :param message: A tuple or list representing a multipart ZMQ message.

        If the message is not a tuple or list then MessageOutError
        will be raised.

        :returns: None.

        """
        #self.log.debug("message_out: to send <%s>" % str(message))
        if isinstance(message, list) or isinstance(message, tuple):
            context = zmq.Context()
            outgoing = context.socket(zmq.PUSH);
            try:
                # send a sync to kick off the hub:
                outgoing.connect(self.outgoing_uri);
                outgoing.send_multipart(frames.sync_message())
                outgoing.send_multipart(message)
            finally:
                outgoing.close()
                context.term()
        else:
            raise MessageOutError("The message must be a list or tuple instead of <%s>" % type(message))



    def message_in(self, message):
        """Called on receipt of an evasion frame to determine what to do.

        The message_handler set in the constructer will be called if one
        was set.

        :param message: A tuple or list representing a multipart ZMQ message.

        """
        if self.message_handler:
            try:
                self.log.debug("message_in: message <%s>" % str(message))
                self.message_handler(message)
            except:
                self.log.exception("message_in: Error handling received message - ")
        else:
            self.log.debug("message_in: message <%s>" % str(message))


class SubscribeError(Exception):
    """Raised for problems subscribing to a signal."""


class Register(object):
    """This is used in a process to a callbacks for signals which
    can be published locally or remotely.
    """
    def __init__(self, config={}, transceiver=None):
        """
        :param config: This is passed to the transceiver.

        The config will only be passed if transceiver argument
        has not been provided.

        :param transceiver: This is an optional transceiver instance.

        This transceiver will be used instead of creating one.

        The Register adds its message_handler method as the
        message handler passed to Transceiver if created internally.

        """
        self.log = logging.getLogger("evasion.messenger.endpoint.Register")

        self.endpoint_uuid = str(uuid.uuid4())
        self._subscriptions = dict()

        if not transceiver:
            self.transceiver = Transceiver(config, self.message_handler)
        else:
            self.transceiver = transceiver



    @classmethod
    def validate_signal(cls, signal):
        """Sanity check the given signal string.

        :param signal: This must be a non empty string.

        ValueError will be raised if signal is not a string
        or empty.

        :returns: For a given string a stripped upper case string.

        >>> Register.signal(' tea_time ')
        >>> 'TEA_TIME'

        """
        if not isinstance(signal, basestring):
            raise ValueError("The signal must be a string and not <%s>" % type(signal))

        signal = signal.strip().upper()
        if not signal:
            raise ValueError("The signal must not be an empty string")

        return signal


    def start(self):
        """Call the transceiver's start()."""
        self.transceiver.start()


    def stop(self):
        """Call the transceiver's stop()."""
        self.transceiver.stop()


    def handle_dispath_message(self, endpoint_uuid, signal, data, reply_to):
        """Handle a DISPATCH message.

        :returns: None.

        """
        #self.log.debug("handle_dispath_message: %s" % str((endpoint_uuid, signal, data, reply_to)))
        signal = self.validate_signal(signal)

        if signal in self._subscriptions:
            for signal_subscriber in self._subscriptions[signal]:
                try:
                    signal_subscriber(endpoint_uuid, data, reply_to if reply_to != '0' else None)
                except:
                    self.log.exception("handle_dispath_message: the callback <%s> for signal <%s> has errored - " % (signal_subscriber, signal))
        else:
            #self.log.debug("handle_dispath_message: no one is subscribed to the signal <%s>. Ignoring." % signal)
            pass


    def handle_hub_present_message(self, payload):
        """Handle a HUB_PRESENT message.

        :param payload: This the content of a HUB_PRESENT message from the hub.

        Currently it is a dict in the form: dict(version='X.Y.Z')

        :returns: None.

        """
        #self.log.debug("handle_hub_present_message: %s" % payload)


    def message_handler(self, message):
        """Called to handle a ZMQ Evasion message received.

        :param message: This must be a message in the Evasion frame format.

        For example::

            'DISPATCH some_string {json object}'

            json_object = json.dumps(dict(
                event='some_string',
                data={...}
            ))

        This will result in the publish being call

        """
        fields_present = len(message)

        if fields_present > 0:
            command = message[0]
            if command and isinstance(command, basestring):
                command = command.strip().lower()

            command_args = []
            if fields_present > 1:
                command_args = message[1:]

            if command == "dispatch":
                try:
                    endpoint_uuid, signal, data, reply_to = command_args
                    data = json.loads(data)
                    self.handle_dispath_message(endpoint_uuid, signal, data, reply_to)
                except IndexError:
                    self.log.error("message_handler: invalid amount of fields given to ")

            elif command == "hub_present":
                try:
                    data = json.loads(command_args[0])
                    self.handle_hub_present_message(data)
                except IndexError:
                    self.log.error("message_handler: no version data found in hub present message!")

            elif command == "sync":
                # Ignore
                pass

            else:
                self.log.error("message_handler: unknown command <%s> no action taken." % command)

        else:
            self.log.error("message_handler: invalid message <%s>" % message)


    def subscribe(self, signal, callback):
        """Called to subscribe to a string signal.

        :param signal: A signal to subscribe too e.g. tea_time.

        The signal must be a string or SubscribeError will be raised.
        The signal will be stripped and uppercased for internal stored.

        Case is not important an us internally is forced to lower case in all
        operations.

        :param callback: This is a function who takes a two arguments.

        If the callback is already subscribed then the subscribe request
        will be ignored.

        The first argument is a data dict representing any data coming with
        the signal. The second is a reply_to argument. If this is not None
        then a reply not expected.

        E.g.::

            def my_handler(data, reply_to=None):
                '''Do something no reply'''


            def my_handler(data, reply_to='uuid string'):
                '''Do something and reply with results'''

        """
        signal = self.validate_signal(signal)

        if signal not in self._subscriptions:
            self._subscriptions[signal] = []

        if callback not in self._subscriptions:
            self._subscriptions[signal].append(callback)
        else:
            self.log.warn("subscribe: The callback<%s> is already subscribed. Ignoring request." % str(callback))



    def unsubscribe(self, signal, callback):
        """Called to remove a callback for a signal.

        :param signal: The signal used in a call to subscribe.
        :param callback: The function to unsubscribe.

        """



    def publish(self, signal, data):
        """Called to publish a signal to all subscribers.

        :param signal: The signal used in a call to subscribe.

        :param data: This is a dictionary of data.

        """
        #self.log.debug("publish: sending <%s> to hub with data <%s>"% (signal, data))

        dispatch_message = frames.dispatch_message(
            self.endpoint_uuid,
            signal,
            data,
        )
        self.transceiver.message_out(dispatch_message)



