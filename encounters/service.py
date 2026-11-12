import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs, unquote
from encounters.journal import Journal
from encounters.values import canonical, fields, integer, Conflict, Invalid
from pathways.query import dispatch


def route(journal, method, path, body):
    if method == 'GET' and path == '/status':
        return {'revision': journal.revision, 'service': 'continuity'}
    if method == 'POST' and path == '/entity-lineage':
        fields(body, ('table', 'id'), ('revision',))
        from encounters.differences import explain_entity
        return explain_entity(journal.dataset(body.get('revision')), body['table'], body['id'])
    if method == 'POST' and path == '/catalog':
        fields(body, ('table',), ('after', 'limit', 'revision'))
        from encounters.catalog_query import table_page
        return table_page(journal.dataset(body.get('revision')), body['table'], body.get('after'), body.get('limit', 100))
    if method == 'GET' and path == '/inventory':
        from encounters.catalog_query import journal_summary
        return journal_summary(journal)
    if method == 'POST' and path == '/entity-history':
        fields(body, ('table', 'id'))
        from encounters.catalog_query import entity_history
        return entity_history(journal, body['table'], body['id'])
    if method == 'POST' and path == '/revision-impact':
        fields(body, ('before', 'after'))
        from encounters.catalog_query import revision_impact
        return revision_impact(journal, body['before'], body['after'])
    if method == 'POST' and path == '/mapped-batch':
        fields(body, ('id', 'base_revision', 'csv', 'mapping'))
        from encounters.mapping import ingest
        return ingest(journal, body['id'], body['base_revision'], body['csv'], body['mapping'])
    if method == 'POST' and path == '/batches':
        fields(body, ('id', 'base_revision', 'operations'))
        return journal.ingest(body['id'], body['base_revision'], body['operations'])
    if method == 'POST' and path == '/analyses':
        fields(body, ('id', 'revision', 'options'))
        return journal.analyze(body['id'], body['revision'], body['options'])
    if method == 'POST' and path == '/query':
        fields(body, ('id', 'kind'), ('options',))
        record = journal.analysis(body['id'])
        return dispatch(journal.dataset(record['revision']), record['result'], body['kind'], body.get('options'))
    if method == 'POST' and path == '/releases':
        fields(body, ('label', 'analysis'), ('minimum_people',))
        return journal.release(body['label'], body['analysis'], body.get('minimum_people', 5))
    if method == 'POST' and path == '/audit':
        fields(body, ())
        return journal.audit()
    if method == 'GET' and path.startswith('/releases/'):
        label = unquote(path[len('/releases/'):])
        if '/' in label or not label:
            raise Invalid('invalid release label')
        return journal.publication(label)
    if method == 'GET' and path.startswith('/analyses/'):
        key = unquote(path[len('/analyses/'):])
        if '/' in key or not key:
            raise Invalid('invalid analysis key')
        return journal.analysis(key)
    raise KeyError('unknown route')


def handler(database, secret):
    if not isinstance(secret, str) or len(secret) < 32 or len(secret) > 1000:
        raise Invalid('service secret requires 32 to 1000 characters')
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, format, *args):
            return

        def respond(self, status, value):
            payload = canonical(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True

        def do_GET(self):
            self.handle_request('GET')

        def do_POST(self):
            self.handle_request('POST')

        def handle_request(self, method):
            self.connection.settimeout(10)
            supplied = self.headers.get('Authorization', '')
            if not hmac.compare_digest(supplied.encode(), ('Bearer ' + secret).encode()):
                self.respond(401, {'error': 'authentication_required'})
                return
            parts = urlsplit(self.path)
            if parts.query or parts.fragment or len(self.path) > 2000:
                self.respond(400, {'error': 'unsupported_url'})
                return
            try:
                body = None
                if method == 'POST':
                    if self.headers.get('Transfer-Encoding'):
                        raise Invalid('chunked bodies not supported')
                    if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                        raise Invalid('JSON content type required')
                    size_text = self.headers.get('Content-Length')
                    if size_text is None or not size_text.isascii() or not size_text.isdigit():
                        raise Invalid('content length required')
                    size = integer(int(size_text), 'body size', 1, 5000000)
                    payload = self.rfile.read(size)
                    if len(payload) != size:
                        raise Invalid('incomplete request body')
                    body = json.loads(payload)
                with Journal(database) as journal:
                    result = route(journal, method, parts.path, body)
                self.respond(200, result)
            except Conflict as error:
                self.respond(409, {'error': str(error)})
            except KeyError:
                self.respond(404, {'error': 'not_found'})
            except (ValueError, UnicodeError, TypeError) as error:
                self.respond(400, {'error': str(error)})
            except TimeoutError:
                self.respond(408, {'error': 'request_timeout'})
            except Exception:
                self.respond(500, {'error': 'internal_failure'})
    return Handler


def serve(database, secret, port=8080):
    integer(port, 'port', 0, 65535)
    server = ThreadingHTTPServer(('127.0.0.1', port), handler(database, secret))
    server.daemon_threads = True
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return {'stopped': True}
