import os
import logging

from functools import partial
from os.path import dirname, getsize, getmtime, join as pjoin
from subprocess import Popen, STDOUT, PIPE

from tornado import web, websocket, gen, ioloop
from tornado.escape import json_encode, json_decode
from tornado.process import Subprocess

STREAM = Subprocess.STREAM
log = logging.getLogger('logtail')
io_loop = ioloop.IOLoop.instance()


class Commands:
    def __init__(self, grep='grep', awk='gawk', tail='tail'):
        self.grepexe = grep
        self.awkexe = awk
        self.tailexe = tail

    def awk(self, script, fn, stdout, stderr, **kw):
        cmd = [self.awkexe, '--sandbox', script]
        if fn: cmd.append(fn)
        p = Subprocess(cmd, stdout=stdout, stderr=stderr, **kw)
        log.debug('running awk %s, pid: %s', cmd, p.proc.pid)
        return p

    def grep(self, regex, fn, stdout, stderr, **kw):
        cmd = [self.grepexe, '--line-buffered', '--color=never', '-e', regex]
        if fn: cmd.append(fn)
        p = Subprocess(cmd, stdout=stdout, stderr=stderr, **kw)
        log.debug('running grep %s, pid: %s', cmd, p.proc.pid)
        return p

    def tail(self, n, fn, stdout, stderr, **kw):
        cmd = (self.tailexe, '-n', str(n), '-f', fn)
        p = Subprocess(cmd, stdout=stdout, stderr=stderr, **kw)
        log.debug('running tail %s, pid: %s', cmd, p.proc.pid)
        return p

    def tail_awk(self, n, fn, script, stdout, stderr):
        tail = self.tail(n, fn, stdout=PIPE, stderr=STREAM)
        awk = self.awk(script, None, stdout=STREAM, stderr=STREAM, stdin=tail.stdout)
        return tail, awk

    def tail_grep(self, n, fn, regex, stdout, stderr):
        tail = self.tail(n, fn, stdout=PIPE, stderr=STREAM)
        grep = self.grep(regex, None, stdout=STREAM, stderr=STREAM, stdin=tail.stdout)
        tail.stdout.close()
        return tail, grep


class BaseHandler(web.RequestHandler):
    def __init__(self, *args, **kw):
        super(BaseHandler, self).__init__(*args, **kw)
        self.config = self.application.config
        self.cconfig = self.application.cconfig

class Index(BaseHandler):
    def get(self):
        files = self.config['files']['__ungrouped__']
        root = self.config['relative-root']
        files = Files.statfiles(files)
        cconfig = json_encode(self.cconfig)
        self.render('index.html', files=files, root=root, client_config=cconfig)

class Files(BaseHandler):
    @staticmethod
    def statfiles(files):
        for fn in files:
            if not os.access(fn, os.R_OK):
                continue
            yield fn, getsize(fn), getmtime(fn)

    def get(self):
        files = self.config['files']['__ungrouped__']
        files = Files.statfiles(files)
        res = {'files': list(files)}
        self.set_header('Content-Type', 'application/json')
        self.write(json_encode(res))

class Fetch(BaseHandler):
    def error(self, code, msg):
        self.set_header('Content-Type', 'text/html')
        self.set_status(500)
        self.finish(
            '<html><title>%(code)d: %(message)s</title>'
            '<body><tt>%(code)d: %(message)s</tt></body></html>' % \
                {'code': code, 'message': msg})

    def get(self, path):
        if not self.config['allow-transfers']:
            self.error(500, 'transfers not allowed'); return

        if path not in self.config['files']['__ungrouped__']:
            self.error(404, 'file not found'); return

        # basename = os.path.basename(path)
        # self.set_header('Content-Disposition', 'attachment; filename="%s"' % path+'asdf');
        self.set_header('Content-Type', 'text/plain')
        with open(path) as fh:
            self.write(fh.read())  # todo: stream

class WebsocketCommands(websocket.WebSocketHandler):
    def __init__(self, *args, **kw):
        super(WebsocketCommands, self).__init__(*args, **kw)

        self.config = self.application.config
        self.cmd = Commands()
        self.connected = True
        self.tail = None
        self.grep = None
        self.awk = None

    def stdout_callback(self, fn, stream, data):
        log.debug('stdout: %s\n', data.decode('utf8'))
        if not self.connected:
            return

        msg = {fn: data.decode('utf8').splitlines(True)}
        self.wjson(msg)

    def stderr_callback(self, fn, stream, data):
        log.debug('stderr: %s', data)
        if not self.connected:
            return

        text = data.decode('utf8')

        if text.endswith(': file truncated\n'):
            text = 'truncated'
        else:
            text = text.splitlines()

        msg = {'fn': fn, 'err': text}
        self.wjson(msg)

    def killall(self):
        if self.tail:
            log.debug('killing tail process: %s', self.tail.pid)
            self.tail.stdout.close()
            self.tail.stderr.close()
            self.tail.proc.kill()
            self.tail = None

        if self.awk:
            log.debug('killing awk process: %s', self.awk.pid)
            self.awk.stdout.close()
            self.awk.stderr.close()
            self.awk.proc.kill()
            self.awk = None

        if self.grep:
            log.debug('killing grep process: %s', self.grep.pid)
            self.grep.stdout.close()
            self.grep.stderr.close()
            self.grep.proc.kill()
            self.grep = None

    def on_message(self, message):
        msg = json_decode(message)
        log.debug('received message: %s', msg)

        self.killall()

        if 'tail' in msg:
            fn = msg['tail']
            if fn in self.config['files']['__ungrouped__']:
                n = msg.get('last', 10)
                self.tail = self.cmd.tail(n, fn, STREAM, STREAM)

                outcb = partial(self.stdout_callback, fn, self.tail.stdout)
                errcb = partial(self.stderr_callback, fn, self.tail.stderr)
                self.tail.stdout.read_until_close(outcb, outcb)
                self.tail.stderr.read_until_close(errcb, errcb)

        elif 'grep' in msg:
            fn = msg['grep']
            if fn in self.config['files']['__ungrouped__']:
                n = msg.get('last', 10)
                regex = msg.get('script', '.*')

                # self.tail, self.grep = self.cmd.tail_grep2(n, fn, regex)
                self.tail, self.grep = self.cmd.tail_grep(n, fn, regex, STREAM, STREAM)

                outcb = partial(self.stdout_callback, fn, self.grep.stdout)
                errcb = partial(self.stderr_callback, fn, self.grep.stderr)
                # self.tail.stderr.read_until_close(errcb, errcb)
                self.grep.stdout.read_until_close(outcb, outcb)
                self.grep.stderr.read_until_close(errcb, errcb)

        elif 'awk' in msg:
            fn = msg['awk']
            if fn in self.config['files']['__ungrouped__']:
                n = msg.get('last', 10)
                script = msg.get('script', '{print $0}')

                self.tail, self.awk = self.cmd.tail_awk(n, fn, script, STREAM, STREAM)

                outcb = partial(self.stdout_callback, fn, self.awk.stdout)
                errcb = partial(self.stderr_callback, fn, self.awk.stderr)
                # self.tail.stderr.read_until_close(errcb, errcb)
                self.awk.stdout.read_until_close(outcb, outcb)
                self.awk.stderr.read_until_close(errcb, errcb)

    def on_close(self):
        self.killall()
        self.connected = False
        log.debug('connection closed')

    def wjson(self, data):
        return self.write_message(json_encode(data))

class Application(web.Application):
    here = dirname(__file__)

    def __init__(self, config, cconfig={}):
        routes = [
          (r'/assets/(.*)', web.StaticFileHandler, {'path': pjoin(self.here, '../assets/')}),
          (r'/files', Files),
          (r'/fetch/(.*)', Fetch),
          (r'/', Index),
          (r'/ws', WebsocketCommands),
        ]

        settings = { 
          'static_path':   pjoin(self.here, '../assets'),
          'template_path': pjoin(self.here, '../templates'),
          'debug': config['debug'],
        }

        super(Application, self).__init__(routes, **settings)
        self.config = config 
        self.cconfig = cconfig
