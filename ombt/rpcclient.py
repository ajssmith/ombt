#!/usr/bin/env python
#
#    Licensed to the Apache Software Foundation (ASF) under one
#    or more contributor license agreements.  See the NOTICE file
#    distributed with this work for additional information
#    regarding copyright ownership.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#
import eventlet
eventlet.monkey_patch()

import pdb

import argparse
import os
import socket
import sys
import threading
import time
import uuid
import logging
import math
try:
    from monotonic import monotonic as now
except ImportError:
    from time import time as now

from oslo_config import cfg
import oslo_messaging as om

RPC_CLIENT = 'RPCClient'
RPC_SERVER = 'RPCServer'
LISTENER = 'Listener'
NOTIFIER = 'Notifier'
DEFAULT_LEN = 1024


class Stats(object):
    """Manage a single statistic"""
    def __init__(self):
        self.min = None
        self.max = None
        self.total = 0
        self.count = 0
        self.average = None
        self.std_deviation = None
        self._sum_of_squares = 0

    def update(self, value):
        self._update(value)

    def merge(self, stats):
        self._update(stats.total, min_value=stats.min, max_value=stats.max, count=stats.count, squared=stats._sum_of_squares)

    def _update(self, value, min_value=None, max_value=None, count=1, squared=None):
        min_value = min_value or value
        max_value = max_value or value
        squared = squared or (value**2)

        if not self.min or min_value < self.min:
            self.min = min_value
        if not self.max or max_value > self.max:
            self.max = max_value
        self.total += value
        self.count += count
        self._sum_of_squares += squared
        n = float(self.count)
        self.average = self.total / n
        self.std_deviation = math.sqrt((self._sum_of_squares / n)
                                       - (self.average ** 2))

    def __str__(self):
        return "min=%i, max=%i, avg=%f, std-dev=%f" % (self.min, self.max, self.average, self.std_deviation)


class _Base(object):
    def __init__(self, transport, exchange, topic, name=None):
        super(_Base, self).__init__()
        self._finished = threading.Event()
        self._name = name
        self._transport = transport
        self._exchange = exchange
        target = om.Target(exchange=exchange, topic=topic, server=name)
        self._control_server = om.get_rpc_server(transport,
                                                 target=target,
                                                 endpoints=[self])
        self._control_server.start()
        ready = False
        attempts = 0
        client = om.RPCClient(transport, target=target, timeout=0.2)
        while not ready and attempts < 25:
            try:
                ready = client.call({}, 'self_ready')
            except om.MessagingTimeout:
                attempts += 1
        if not ready:
            raise Exception("Unable to contact message bus")
        logging.debug("%s is listening", self._name)

    def wait(self, timeout=None):
        return self._finished.wait(timeout)

    def _do_shutdown(self):
        self._control_server.stop()
        self._control_server.wait()
        self._finished.set()
        logging.debug("%s has shut down", self._name)

    #
    # RPC calls:
    #

    def shutdown(self, ctxt):
        # cannot synchronously shutdown server since this call is dispatched by
        # the server...
        threading.Thread(target=self._do_shutdown).start()

    def self_ready(self, ctxt):
        return True


class _Controller(_Base):
    def __init__(self, transport, topic):
        # listen on 'controller-$topic' for client responses:
        self._topic = "controller-%s" % topic
        super(_Controller, self).__init__(transport, 'ombt', self._topic,
                                          name='ombt-controller-%s' % topic)
        self._minions = dict()
        self._total_minions = 0
        target = om.Target(exchange='ombt', topic='client-%s' % topic,
                           fanout=True)
        self._clients = om.RPCClient(transport, target=target)
        self._clients.cast({}, 'client_ping')
        time.sleep(0.2)  # allow some clients to respond

    def shutdown(self):
        self._clients.cast({}, 'shutdown')
        time.sleep(0.5)
        super(_Controller, self).shutdown({})

    #
    # RPC calls:
    #

    def client_pong(self, ctxt, kind):
        if kind not in self._minions:
            self._minions[kind] = 0
        self._minions[kind] += 1
        self._total_minions += 1



class _Client(_Base):
    def __init__(self, transport, topic, kind):
        # listen on 'client-$topic' for controller commands:
        self._topic = "client-%s" % topic
        self._kind = kind
        name = 'ombt-client-%s-%s-%s-%s' % (topic,
                                            socket.gethostname(),
                                            os.getpid(),
                                            uuid.uuid4().hex)
        super(_Client, self).__init__(transport, 'ombt', self._topic, name)
        target = om.Target(exchange='ombt', topic='controller-%s' % topic)
        self.controller = om.RPCClient(transport, target=target, timeout=30)

    def client_ping(self, ctxt):
        self.controller.call({}, 'client_pong', kind=self._kind)


class RPCController(_Controller):
    def __init__(self, transport, topic, timeout=None):
        super(RPCController, self).__init__(transport, topic)
        self._throughput = Stats()
        self._latency = Stats()
        self._done = threading.Event()
        self._timeout = timeout
        target = om.Target(exchange='ombt',
                           topic='rpc-client-%s' % topic,
                           fanout=True)
        self._rpc_clients = om.RPCClient(transport, target)

    def _print_stats(self):
        print("\n")
        print("Latency (millisecs):    %s" % self._latency)
        print("Throughput (calls/sec): %s" % self._throughput)
        print("  Averaged over %s client(s)" % self._result_count)

    def _run_test(self, meth, count, data, verbose):
        if (RPC_CLIENT not in self._minions
                or self._minions[RPC_CLIENT] == 0):
            print("No RPC clients visible")
            return -1

        self._result_count = 0
        self._done.clear()
        self._rpc_clients.cast({}, meth, count=count, data=data,
                               verbose=verbose)
        if not self._done.wait(self._timeout):
            print("%s test timed out!" % meth)
        else:
            self._print_stats()

    #
    # Tests:
    #

    def run_call_test(self, count, data, verbose=False):
        self._run_test('test_call', count, data, verbose)

    def run_cast_test(self, count, data, verbose=False):
        self._run_test('test_cast', count, data, verbose)

    #
    # RPC Calls
    #

    def client_result(self, ctxt, results):
        self._result_count += 1
        t = results['throughput']
        self._throughput.update(t)
        l = Stats()
        l.__dict__.update(results['latency'])
        self._latency.merge(l)
        logging.debug("  result %i of %i"
                      " - Throughput: %i, Latency:%s", self._result_count,
                      self._minions[RPC_CLIENT], t, l)
        if self._result_count == self._minions[RPC_CLIENT]:
            self._done.set()



# class ControlledClient(object):

#     def __init__(self, transport,
#                  command_target,
#                  control_target):
#         super(ControlledClient, self).__init(self)
#         self._event = threading.Event()
#         self._transport = transport
#         self._control = messaging.RPCClient(transport, control_target,
#                                             timeout=30)
#         self._command = messaging.get_rpc_server(transport, command_target,
#                                                  self)
#         self._command.start()
#         time.sleep(0.5) # give server time to setup
#         self._setup()
#         self._event.wait()
#         self._teardown()
#         self._command.stop()
#         self._command.wait()

#     def kill(self):
#         self._event.set()

#     def ping(self, ctxt, **kwargs):
#         pass

#     def self._setup():
#         pass

#     def self._teardown():
#         pass



class RPCTestClient(_Client):
    def __init__(self, transport, topic):
        super(RPCTestClient, self).__init__(transport,
                                            topic,
                                            RPC_CLIENT)
        # listen for commands from the rpc Controller:
        target = om.Target(exchange='ombt',
                           topic='rpc-client-%s' % topic,
                           server=self._name)
        self._command_server = om.get_rpc_server(transport,
                                                 target,
                                                 [self])
        self._command_server.start()

        # for calling the test RPC server:
        target = om.Target(exchange='ombt',
                           topic='rpc-server-%s' % topic)
        self._server = om.RPCClient(transport, target=target,
                                    timeout=30)

    def _run_rpc_test(self, func, count, verbose):
        latency = Stats()
        calls = 0
        t_start = now()
        stop = False
        while not stop:
            t = now()
            try:
                func()
            except Exception as ex:
                logging.error("Test failure: %s", str(ex))
                raise
            latency.update((now() - t) * 1000)
            calls += 1
            if (verbose and count and (calls % (max(10, count)/10) == 0)):
                logging.info("Call %i of %i completed", calls, count)
            if count and calls >= count:
                stop = True
        results = {'latency': latency.__dict__,
                   'throughput': calls/(now() - t_start),
                   'calls': calls}

        self.controller.cast({}, 'client_result', results=results)

    #
    # RPC Calls:
    #

    def shutdown(self, ctxt):
        self._command_server.stop()
        self._command_server.wait()
        super(RPCTestClient, self).shutdown(ctxt)

    def test_call(self, ctxt, count, data, verbose):
        self._run_rpc_test(lambda: self._server.call({}, 'echo', data=data),
                           count, verbose)

    def test_cast(self, ctxt, count, data, verbose):
        self._run_rpc_test(lambda: self._server.cast({}, 'noop', data=data),
                           count, verbose)


class RPCTestServer(_Client):
    def __init__(self, transport, topic, executor):
        super(RPCTestServer, self).__init__(transport,
                                            topic,
                                            RPC_SERVER)
        target = om.Target(exchange='ombt',
                           topic='rpc-server-%s' % topic,
                           server=self._name)
        self._rpc_server = om.get_rpc_server(transport,
                                             target,
                                             [self],
                                             executor=executor)
        self._rpc_server.start()

    #
    # RPC Calls:
    #

    def shutdown(self, ctxt):
        self._rpc_server.stop()
        self._rpc_server.wait()
        super(RPCTestServer, self).shutdown(ctxt)

    def noop(self, ctxt, data):
        # for cast testing
        pass

    def echo(self, ctxt, data):
        # for call testing
        return data


class TestNotifier(_Client):
    def __init__(self, transport, topic):
        super(TestNotifier, self).__init__(transport,
                                           topic,
                                           NOTIFIER)
        # listen for commands from the Controller:
        target = om.Target(exchange='ombt',
                           topic='notifier-%s' % topic,
                           server=self._name)
        self._command_server = om.get_rpc_server(transport,
                                                 target,
                                                 [self])
        self._command_server.start()

        # for calling the test RPC server:

        self._notifier = om.notify.notifier.Notifier(transport,
                                                     self._name,
                                                     driver='messaging',
                                                     topics=[topic])

    #
    # RPC Calls:
    #

    def shutdown(self, ctxt):
        self._command_server.stop()
        self._command_server.wait()
        super(TestNotifier, self).shutdown(ctxt)

    def test_notify(self, ctxt, count, data, severity, verbose):
        latency = Stats()
        calls = 0
        func = getattr(self._notifier, severity)
        payload = {'payload': data}
        stop = False
        t_start = now()
        while not stop:
            t = now()
            try:
                func({}, "notification-test", payload)
            except Exception as ex:
                logging.error("Test failure: %s", str(ex))
                raise
            latency.update((now() - t) * 1000)
            calls += 1
            if (verbose and count and (calls % (max(10, count)/10) == 0)):
                logging.info("Call %i of %i completed", calls, count)
            if count and calls >= count:
                stop = True
        results = {'latency': latency.__dict__,
                   'throughput': calls/(now() - t_start),
                   'calls': calls}

        self.controller.cast({}, 'client_result', results=results)





class TestListener(_Client):
    def __init__(self, transport, topic, executor):
        super(TestListener, self).__init__(transport,
                                           topic,
                                           LISTENER)
        target = om.Target(exchange='ombt',
                           topic=topic,
                           server=self._name)
        self._listener = om.get_notification_listener(transport,
                                                      [target],
                                                      [self],
                                                      executor=executor)
        self._listener.start()

    #
    # RPC Calls:
    #

    def shutdown(self, ctxt):
        self._listener.stop()
        self._listener.wait()
        super(TestListener, self).shutdown(ctxt)

    #
    # Notifications:
    #

    def _report(self, severity, ctx, publisher, event_type, payload, metadata):
        logging.debug("%s Notification %s:%s:%s:%s:%s", self._name, severity,
                      publisher, event_type, payload, metadata)

    def debug(self, ctx, publisher, event_type, payload, metadata):
        self._report("debug", ctx, publisher, event_type, payload, metadata)

    def audit(self, ctx, publisher, event_type, payload, metadata):
        self._report("audit", ctx, publisher, event_type, payload, metadata)

    def critical(self, ctx, publisher, event_type, payload, metadata):
        self._report("critical", ctx, publisher, event_type, payload, metadata)

    def error(self, ctx, publisher, event_type, payload, metadata):
        self._report("error", ctx, publisher, event_type, payload, metadata)

    def info(self, ctx, publisher, event_type, payload, metadata):
        self._report("info", ctx, publisher, event_type, payload, metadata)

    def warn(self, ctx, publisher, event_type, payload, metadata):
        self._report("warn", ctx, publisher, event_type, payload, metadata)


class NotifyController(_Controller):
    def __init__(self, transport, topic, timeout=None):
        super(NotifyController, self).__init__(transport, topic)
        self._throughput = Stats()
        self._latency = Stats()
        self._done = threading.Event()
        self._timeout = timeout
        target = om.Target(exchange='ombt',
                           topic='notifier-%s' % topic,
                           fanout=True)
        self._notifiers = om.RPCClient(transport, target)

    def _print_stats(self):
        print("\n")
        print("Latency (millisecs):    %s" % self._latency)
        print("Throughput (calls/sec): %s" % self._throughput)
        print("  Averaged over %s client(s)" % self._result_count)

    #
    # Tests:
    #
    def run_notification_test(self, count, data, severity, verbose):
        if (NOTIFIER not in self._minions
                or self._minions[NOTIFIER] == 0):
            print("No notifier clients visible")
            return -1

        self._result_count = 0
        self._done.clear()
        self._notifiers.cast({}, 'test_notify', count=count, data=data,
                             severity=severity, verbose=verbose)
        if not self._done.wait(self._timeout):
            print("%s test timed out!" % meth)
        else:
            self._print_stats()

    #
    # RPC Calls
    #

    def client_result(self, ctxt, results):
        self._result_count += 1
        t = results['throughput']
        self._throughput.update(t)
        l = Stats()
        l.__dict__.update(results['latency'])
        self._latency.merge(l)
        logging.debug("  result %i of %i"
                      " - Throughput: %i, Latency:%s", self._result_count,
                      self._minions[NOTIFIER], t, l)
        if self._result_count == self._minions[NOTIFIER]:
            self._done.set()


        
def _parse_args(args, values):
    for i in range(len(args)):
        arg = args[i].lower()
        try:
            key, value = arg.split('=')
        except ValueError:
            print("Error - argument format is key=value")
            print(" - %s is not valid" % str(arg))
            raise SyntaxError("bad argument: %s" % arg)
        if key not in values:
            print("Error - unrecognized argument %s" % key)
            print(" - arguments %s" % [x for x in iter(values)])
            raise SyntaxError("unknown argument: %s" % key)
        values[key] = int(value) if isinstance(values[key], int) else value
    return values


def _do_shutdown(tport, cfg, args):
    controller = _Controller(tport, args.topic)
    controller.shutdown()

def _rpc_call_test(tport, cfg, args):
    controller = RPCController(tport, args.topic, args.timeout)
    args = _parse_args(args.args, {'length': DEFAULT_LEN, 'calls': 1})
    controller.run_call_test(args['calls'], 'X' * args['length'])


def _rpc_cast_test(tport, cfg, args):
    controller = RPCController(tport, args.topic, args.timeout)
    args = _parse_args(args.args, {'length': DEFAULT_LEN, 'calls': 1})
    controller.run_cast_test(args['calls'], 'X' * args['length'])


def _notify_test(tport, cfg, args):
    controller = NotifyController(tport, args.topic, args.timeout)
    args = _parse_args(args.args, {'length': DEFAULT_LEN, 'calls': 1,
                                   'severity': 'debug',
                                   'verbose': None})
    controller.run_notification_test(args['calls'], 'X' * args['length'],
                                     args['severity'], args['verbose'])

def controller(tport, cfg, args):
    TESTS={'rpc-call': _rpc_call_test,
           'rpc-cast': _rpc_cast_test,
           'shutdown': _do_shutdown,
           'notify': _notify_test}
    func = TESTS.get(args.test.lower())
    if func is None:
        print("Error - unrecognized command %s" % args.test)
        print("commands: %s" % [x for x in iter(TESTS)])
        return -1
    return func(tport, cfg, args)


def rpc_standalone(tport, cfg, args):
    server = RPCTestServer(tport,
                           args.topic,
                           args.executor)
    client = RPCTestClient(tport, args.topic)

    controller = RPCController(tport, args.topic, args.timeout)

    if args.do_cast:
        controller.run_cast_test(args.calls,
                                 'X' * args.length,
                                 args.debug)
    else:
        controller.run_call_test(args.calls,
                                 'X' * args.length,
                                 args.debug)
    controller.shutdown()


def notify_standalone(tport, cfg, args):
    server = TestListener(tport,
                          args.topic,
                          args.executor)
    client = TestNotifier(tport, args.topic)

    controller = NotifyController(tport, args.topic, args.timeout)
    controller.run_notification_test(args.calls,
                                     'X' * args.length,
                                     'debug', # todo: fix
                                     args.debug)
    controller.shutdown()


def rpc_server(tport, cfg, args):
    server = RPCTestServer(tport, args.topic, args.executor)
    server.wait()

def rpc_client(tport, cfg, args):
    client = RPCTestClient(tport, args.topic)
    client.wait()


def main():
    parser = argparse.ArgumentParser(
        description='Benchmark tool for oslo.messaging')

    parser.add_argument("--url",
                        default='rabbit://localhost:5672',
                        help="The address of the messaging service")
    parser.add_argument("--oslo-config",
                        help="oslo.messaging configuration file")
    parser.add_argument('--topic', default='test-topic',
                        help='service address to use')
    parser.add_argument('--debug', action='store_true',
                        help='Enable DEBUG logging')
    parser.add_argument("--timeout", type=int, default=None,
                        help='fail test after timeout seconds')

    subparsers = parser.add_subparsers(dest='mode',
                                       description='operational mode')
    # RPC Standalone
    sp = subparsers.add_parser('rpc',
                               description='standalone RPC test')
    sp.add_argument("--calls", type=int, default=1,
                    help="Number of RPC calls to perform")
    sp.add_argument("--length", type=int, default=DEFAULT_LEN,
                    help='length in bytes of payload string')
    sp.add_argument("--cast", dest='do_cast', action='store_true',
                    help='RPC cast instead of RPC call')
    sp.add_argument("--executor", default="threading",
                    help="type of executor the server will use")

    # Notification Standalone
    sp = subparsers.add_parser('notify',
                               description='standalone notify test')
    sp.add_argument("--calls", type=int, default=1,
                    help="Number of RPC calls to perform")
    sp.add_argument("--length", type=int, default=DEFAULT_LEN,
                    help='length in bytes of payload string')
    sp.add_argument("--executor", default="threading",
                    help="type of executor the server will use")

    # Test controller
    sp = subparsers.add_parser('controller',
                               description='Controller mode')
    sp.add_argument("test", help='the test to run')
    sp.add_argument('args', nargs='*', help='test arguments')

    # RPC Server
    sp = subparsers.add_parser('rpc-server',
                               description='RPC Server mode')
    sp.add_argument("--executor", default="threading",
                    help="type of executor the server will use")

    # RPC Client
    sp = subparsers.add_parser('rpc-client',
                               description='RPC Client mode')

    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARN)

    cfg.CONF.transport_url=args.url
    if args.oslo_config:
        cfg.CONF(["--config-file", args.oslo_config])

    tport = om.get_transport(cfg.CONF)

    {'controller': controller,
     'rpc': rpc_standalone,
     'notify': notify_standalone,
     'rpc-server': rpc_server,
     'rpc-client': rpc_client}[args.mode](tport, cfg, args)

    return None


if __name__ == "__main__":
    sys.exit(main())
