from flask import Flask
from flask import g

import werkzeug.local
import logging

from . import config
from . import aaa


LOG = logging.getLogger(__name__)

# create flask app and load config from vmsh.config.api module
app = Flask(__name__)

app.logger.addHandler(logging.StreamHandler())
if config.get('DEBUG', False):
    app.logger.setLevel(logging.DEBUG)
else:
    app.logger.setLevel(logging.INFO)

aaa.LOGIN_MANAGER.init_app(app)


# redis connections
try:
    import redis

    def get_SR():
        SR = getattr(g, '_redis_client', None)
        if SR is None:
            redis_url = config.get('REDIS_URL', 'redis://localhost:6379/0')
            SR = redis.StrictRedis.from_url(redis_url)
            g._redis_client = SR
        return SR

    @app.teardown_appcontext
    def teardown_redis(exception):
        SR = getattr(g, '_redis_client', None)
        if SR is not None:
            g._redis_client = None

    SR = werkzeug.local.LocalProxy(get_SR)

    # load entry points
    from . import feedredis  # noqa
    from . import configapi  # noqa

except ImportError:
    LOG.exception("redis is needed for feed and config entrypoint")


try:
    # amqp connection
    import gevent
    import gevent.event
    import gevent.queue
    import amqp
    import json
    import uuid
    import psutil  # noqa

    class _MMMasterConnection(object):
        def __init__(self):
            self._connection = None
            self._in_channel = None
            self._out_channel = None
            self._in_q = None

            self._g_ioloop = None

            self.active_requests = {}

        def _open_channel(self):
            if self._connection is not None:
                return

            self._connection = amqp.connection.Connection(
                **config.get('FABRIC', {})
            )
            self._out_channel = self._connection.channel()

            self._in_channel = self._connection.channel()
            self._in_q = self._in_channel.queue_declare(exclusive=True)
            self._in_channel.basic_consume(
                callback=self._callback,
                no_ack=True,
                exclusive=True
            )

            self._g_ioloop = gevent.spawn(self._ioloop)

        def _callback(self, msg):
            try:
                body = json.loads(msg.body)
            except ValueError:
                LOG.error("invalid message received")
                return

            id_ = body.get('id', None)
            if id_ is None:
                LOG.error("message with no id received, ignored")
                return
            if id_ not in self.active_requests:
                LOG.error("message with unknown id received, ignored")
                return

            result = body.get('result', None)
            if result is None:
                errormsg = body.get('error',
                                    'Error talking with mgmtbus master')
                self.active_requests[id_]['result'].set_exception(
                    RuntimeError(errormsg)
                )
                return

            self.active_requests[id_]['result'].set(value=result)
            self.active_requests.pop(id_)

        def _ioloop(self):
            while True:
                self._connection.drain_events()

        def _send_cmd(self, method, params={}):
            self._open_channel()

            id_ = str(uuid.uuid1())
            msg = {
                'id': id_,
                'reply_to': self._in_q.queue,
                'method': method,
                'params': params
            }
            self.active_requests[id_] = {
                'result': gevent.event.AsyncResult(),
            }

            self._out_channel.basic_publish(
                amqp.Message(body=json.dumps(msg)),
                routing_key='mbus:master'
            )

            return id_

        def status(self):
            reqid = self._send_cmd('status')
            return self.active_requests[reqid]['result'].get(timeout=10)

        def stop(self):
            if self._g_ioloop is not None:
                self._g_ioloop.kill()

            if self._connection is not None:
                self._in_channel.close()
                self._out_channel.close()
                self._connection.close()

    def get_mmmaster():
        r = getattr(g, 'MMMaster', None)
        if r is None:
            r = _MMMasterConnection()
            g.MMMaster = r
        return r

    @app.teardown_appcontext
    def tearwdown_mmmaster(exception):
        r = getattr(g, 'MMMaster', None)
        if r is not None:
            g.MMMaster.stop()
            g.MMMaster = None

    MMMaster = werkzeug.LocalProxy(get_mmmaster)

    class _MMStateFanout(object):
        def __init__(self):
            self.subscribers = {}
            self.next_subscriber_id = 0

            self._connection = amqp.connection.Connection(
                **config.get('FABRIC', {})
            )
            self._channel = self._connection.channel()
            q = self._channel.queue_declare(exclusive=True)
            self._channel.queue_bind(
                queue=q.queue,
                exchange='mw_chassis_state'
            )
            self._channel.basic_consume(
                callback=self._callback,
                no_ack=True,
                exclusive=True
            )

            self.g_ioloop = gevent.spawn(self._ioloop)

        def _ioloop(self):
            while True:
                self._connection.drain_events()

        def _callback(self, msg):
            try:
                msg = json.loads(msg.body)
            except ValueError:
                LOG.error("invalid message received")
                return

            method = msg.get('method', None)
            if method is None:
                LOG.error("Message without method field")
                return

            if method != 'state':
                LOG.error("Method not allowed: %s", method)
                return

            params = msg.get('params', {})

            for s in self.subscribers.values():
                state = params.get('state', {})
                data = {
                    'type': 'state',
                    'data': state
                }
                s.put("data: %s\n\n" %
                      json.dumps(data))

        def subscribe(self):
            csid = self.next_subscriber_id
            self.next_subscriber_id += 1

            self.subscribers[csid] = gevent.queue.Queue()

            return csid

        def unsubscribe(self, sid):
            self.subscribers.pop(sid, None)

        def get(self, sid):
            if sid not in self.subscribers:
                return None

            return self.subscribers[sid].get()

        def stop(self):
            self.g_ioloop.kill()

            self._channel.close()
            self._connection.close()
            self._connection = None

    def get_mmstatefanout():
        r = getattr(g, '_mmstatefanout', None)
        if r is None:
            r = _MMStateFanout()
            g._mmstatefanout = r
        return r

    @app.teardown_appcontext
    def tearwdown_mmstatefanout(exception):
        r = getattr(g, '_mmstatefanout', None)
        if r is not None:
            g._mmstatefanout.stop()
            g._mmstatefanout = None

    MMStateFanout = werkzeug.LocalProxy(get_mmstatefanout)

    from . import status  # noqa

except ImportError:
    LOG.exception("amqp, psutil and gevent needed for the status entrypoint")

try:
    import rrdtool  # noqa

    from . import metricsapi  # noqa

except ImportError:
    LOG.exception("rrdtool needed for metrics endpoint")