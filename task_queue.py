#!/usr/bin/python3
from gevent.server import StreamServer
from gevent.queue import PriorityQueue, Queue
from gevent import monkey, spawn, sleep
monkey.patch_all()

from matcher import overpass, netstring, mail, space_alert, database
from matcher.view import app
import requests.exceptions
import json
import os.path

# Priority queue
# We should switch to a priority queue ordered by number of chunks
# if somebody requests a place with 10 chunks they should go to the back
# of the queue
#
# Abort request
# If a user gives up and closes the page do we should remove their request from
# the queue if nobody else has made the same request.
#
# We can tell the page was closed by checking a websocket heartbeat.

app.config.from_object('config.default')
database.init_app(app)

task_queue = PriorityQueue()

listen_host, port = 'localhost', 6020

# almost there
# should give status update as each chunk is loaded.
# tell client the length of the rate limit pause

def to_client(send_queue, msg_type, msg):
    msg['type'] = msg_type
    send_queue.put(msg)

def wait_for_slot(send_queue):
    print('get status')
    try:
        status = overpass.get_status()
    except overpass.OverpassError as e:
        r = e.args[0]
        body = f'URL: {r.url}\n\nresponse:\n{r.text}'
        mail.send_mail('Overpass API unavailable', body)
        send_queue.put({'type': 'error',
                        'error': "Can't access overpass API"})
        return False
    except requests.exceptions.Timeout:
        body = 'Timeout talking to overpass API'
        mail.send_mail('Overpass API timeout', body)
        send_queue.put({'type': 'error',
                        'error': "Can't access overpass API"})
        return False

    print('status:', status)
    if not status['slots']:
        return True
    secs = status['slots'][0]
    if secs <= 0:
        return True
    send_queue.put({'type': 'status', 'wait': secs})
    sleep(secs)
    return True

def process_queue_loop():
    with app.app_context():
        while True:
            process_queue()

def process_queue():
    area, item = task_queue.get()
    place = item['place']
    send_queue = item['queue']
    for num, chunk in enumerate(item['chunks']):
        oql = chunk.get('oql')
        if not oql:
            continue
        filename = 'overpass/' + chunk['filename']
        msg = {
            'num': num,
            'filename': chunk['filename'],
            'place': place,
        }
        if not os.path.exists(filename):
            space_alert.check_free_space(app.config)
            if not wait_for_slot(send_queue):
                return
            to_client(send_queue, 'run_query', msg)
            print('run query')
            r = overpass.run_query(oql)
            print('query complete')
            with open(filename, 'wb') as out:
                out.write(r.content)
            space_alert.check_free_space(app.config)
        print(msg)
        to_client(send_queue, 'chunk', msg)
    print('item complete')
    send_queue.put(None)

class Request:
    def __init__(self, sock, address):
        self.address = address
        self.sock = sock
        self.send_queue = None

    def send_msg(self, msg, check_ack=True):
        netstring.write(self.sock, json.dumps(msg))
        if check_ack:
            msg = netstring.read(self.sock)
            assert msg == 'ack'

    def reply_and_close(self, msg):
        self.send_msg(msg, check_ack=False)
        self.sock.close()

    def new_place_request(self, msg):
        self.send_queue = Queue()
        try:
            area = float(msg['place']['area'])
        except ValueError:
            area = 0
        task_queue.put((area, {
            'place': msg['place'],
            'address': self.address,
            'chunks': msg['chunks'],
            'queue': self.send_queue,
        }))

        self.send_msg({'type': 'connected'})

    def handle(self):
        print('New connection from %s:%s' % self.address)
        try:
            msg = json.loads(netstring.read(self.sock))
        except json.decoder.JSONDecodeError:
            msg = {'type': 'error', 'error': 'invalid JSON'}
            return self.reply_and_close(msg)

        if msg.get('type') == 'ping':
            return self.reply_and_close({'type': 'pong'})

        self.new_place_request(msg)
        error = False
        try:
            to_send = self.send_queue.get()
            while to_send:
                self.send_msg(to_send)
                if to_send['type'] == 'error':
                    error = True
                to_send = self.send_queue.get()
        except BrokenPipeError:
            print('socket closed')
        else:
            if not error:
                print('request complete')
                self.send_msg({'type': 'done'})

        self.sock.close()

def handle_request(sock, address):
    r = Request(sock, address)
    return r.handle()

def main():
    space_alert.check_free_space(app.config)
    spawn(process_queue_loop)
    print('listening on port {}'.format(port))
    server = StreamServer((listen_host, port), handle_request)
    server.serve_forever()


if __name__ == '__main__':
    main()
