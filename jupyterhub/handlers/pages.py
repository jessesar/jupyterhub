"""Basic html-rendering handlers."""

# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.

from http.client import responses

from jinja2 import TemplateNotFound
from tornado import web, gen
from tornado.httputil import url_concat

from .. import orm
from ..utils import admin_only, url_path_join
from .base import BaseHandler

import json
import datetime
import logging
import time
import os

import dateutil.parser as dp

class RootHandler(BaseHandler):
    """Render the Hub root page.

    If next argument is passed by single-user server,
    redirect to base_url + single-user page.

    If logged in, redirects to:

    - single-user server if running
    - hub home, otherwise

    Otherwise, renders login page.
    """
    def get(self):
        next_url = self.get_argument('next', '')
        if next_url and not next_url.startswith('/'):
            self.log.warning("Disallowing redirect outside JupyterHub: %r", next_url)
            next_url = ''
        if next_url and next_url.startswith(url_path_join(self.base_url, 'user/')):
            # add /hub/ prefix, to ensure we redirect to the right user's server.
            # The next request will be handled by UserSpawnHandler,
            # ultimately redirecting to the logged-in user's server.
            without_prefix = next_url[len(self.base_url):]
            next_url = url_path_join(self.hub.base_url, without_prefix)
            self.log.warning("Redirecting %s to %s. For sharing public links, use /user-redirect/",
                self.request.uri, next_url,
            )
            self.redirect(next_url)
            return
        user = self.get_current_user()
        if user:
            if user.running:
                url = user.url
                self.log.debug("User is running: %s", url)
                self.set_login_cookie(user) # set cookie
            else:
                url = url_path_join(self.hub.base_url, 'home')
                self.log.debug("User is not running: %s", url)
        else:
            url = self.settings['login_url']
        self.redirect(url)


class HomeHandler(BaseHandler):
    """Render the user's home page."""

    @web.authenticated
    @gen.coroutine
    def get(self):
        user = self.get_current_user()
        if user.running:
            # trigger poll_and_notify event in case of a server that died
            yield user.spawner.poll_and_notify()

        # send the user to /spawn if they aren't running or pending a spawn,
        # to establish that this is an explicit spawn request rather
        # than an implicit one, which can be caused by any link to `/user/:name`
        url = user.url if user.running or user.spawner.pending else url_path_join(self.hub.base_url, 'spawn')
        html = self.render_template('home.html',
            user=user,
            url=url,
        )
        self.finish(html)


class SpawnHandler(BaseHandler):
    """Handle spawning of single-user servers via form.

    GET renders the form, POST handles form submission.

    Only enabled when Spawner.options_form is defined.
    """
    def _render_form(self, message=''):
        user = self.get_current_user()
        return self.render_template('spawn.html',
            user=user,
            spawner_options_form=user.spawner.options_form,
            error_message=message,
            url=self.request.uri,
        )

    @web.authenticated
    def get(self):
        """GET renders form for spawning with user-specified options

        or triggers spawn via redirect if there is no form.
        """
        user = self.get_current_user()
        if not self.allow_named_servers and user.running:
            url = user.url
            self.log.debug("User is running: %s", url)
            self.redirect(url)
            return
        if user.spawner.options_form:
            self.finish(self._render_form())
        else:
            # Explicit spawn request: clear _spawn_future
            # which may have been saved to prevent implicit spawns
            # after a failure.
            if user.spawner._spawn_future and user.spawner._spawn_future.done():
                user.spawner._spawn_future = None
            # not running, no form. Trigger spawn by redirecting to /user/:name
            self.redirect(user.url)

    @web.authenticated
    @gen.coroutine
    def post(self):
        """POST spawns with user-specified options"""
        user = self.get_current_user()
        if not self.allow_named_servers and user.running:
            url = user.url
            self.log.warning("User is already running: %s", url)
            self.redirect(url)
            return
        if user.spawner.pending:
            raise web.HTTPError(
                400, "%s is pending %s" % (user.spawner._log_name, user.spawner.pending)
            )
        form_options = {}
        for key, byte_list in self.request.body_arguments.items():
            form_options[key] = [ bs.decode('utf8') for bs in byte_list ]
        for key, byte_list in self.request.files.items():
            form_options["%s_file"%key] = byte_list
        try:
            options = user.spawner.options_from_form(form_options)
            yield self.spawn_single_user(user, options=options)
        except Exception as e:
            self.log.error("Failed to spawn single-user server with form", exc_info=True)
            self.finish(self._render_form(str(e)))
            return
        self.set_login_cookie(user)
        url = user.url

        next_url = self.get_argument('next', '')
        if next_url and not next_url.startswith('/'):
            self.log.warning("Disallowing redirect outside JupyterHub: %r", next_url)
        elif next_url:
            url = next_url

        self.redirect(url)

class AdminHandler(BaseHandler):
    """Render the admin page."""

    @admin_only
    def get(self):
        available = {'name', 'admin', 'running', 'last_activity', 'exam_finished'}
        
        default_sort = ['admin', 'name']
        mapping = {
            'running': orm.Spawner.server_id,
        }
        
        for name in available:
            if name not in mapping:
                mapping[name] = getattr(orm.User, name)
        
        #app_log = logging.getLogger("tornado.application")
        #app_log.info("Mapping: {0}".format(mapping))
        
        default_order = {
            'name': 'asc',
            'last_activity': 'desc',
            'exam_finished': 'desc',
            'admin': 'desc',
            'running': 'desc',
        }

        sorts = self.get_arguments('sort') or default_sort
        orders = self.get_arguments('order')

        for bad in set(sorts).difference(available):
            self.log.warning("ignoring invalid sort: %r", bad)
            sorts.remove(bad)
        for bad in set(orders).difference({'asc', 'desc'}):
            self.log.warning("ignoring invalid order: %r", bad)
            orders.remove(bad)

        # add default sort as secondary
        for s in default_sort:
            if s not in sorts:
                sorts.append(s)
        if len(orders) < len(sorts):
            for col in sorts[len(orders):]:
                orders.append(default_order[col])
        else:
            orders = orders[:len(sorts)]

        # this could be one incomprehensible nested list comprehension
        # get User columns
        cols = [ mapping[c] for c in sorts ]
        # get User.col.desc() order objects
        ordered = [ getattr(c, o)() for c, o in zip(cols, orders) ]

        users = self.db.query(orm.User).outerjoin(orm.Spawner).order_by(*ordered)
        users = [ self._user_from_orm(u) for u in users ]
        running = [ u for u in users if u.running ]

        html = self.render_template('admin.html',
            user=self.get_current_user(),
            admin_access=self.settings.get('admin_access', False),
            users=users,
            running=running,
            sort={s:o for s,o in zip(sorts, orders)},
        )
        self.finish(html)


class TokenPageHandler(BaseHandler):
    """Handler for page requesting new API tokens"""

    @web.authenticated
    def get(self):
        html = self.render_template('token.html')
        self.finish(html)


class ProxyErrorHandler(BaseHandler):
    """Handler for rendering proxy error pages"""

    def get(self, status_code_s):
        status_code = int(status_code_s)
        status_message = responses.get(status_code, 'Unknown HTTP Error')
        # build template namespace

        hub_home = url_path_join(self.hub.base_url, 'home')
        message_html = ''
        if status_code == 503:
            message_html = ' '.join([
                "Your server appears to be down.",
                "Try restarting it <a href='%s'>from the hub</a>" % hub_home
            ])
        ns = dict(
            status_code=status_code,
            status_message=status_message,
            message_html=message_html,
            logo_url=hub_home,
        )

        self.set_header('Content-Type', 'text/html')
        # render the template
        try:
            html = self.render_template('%s.html' % status_code, **ns)
        except TemplateNotFound:
            self.log.debug("No template for %d", status_code)
            html = self.render_template('error.html', **ns)

        self.write(html)
        
class SelectExamHandler(BaseHandler):
    @web.authenticated
    @gen.coroutine
    def get(self):
        user = self.get_current_user()
        
        exam = self.get_eligible_exam()
        
        if exam:
            setattr(user, 'exam', exam['name'])
            
            self.db.commit()
            
            self.redirect(url_path_join(self.hub.server.base_url, 'home'))
        else:
            pass

class EndSessionHandler(BaseHandler):
	"""End the user's session: stop the server, log out and show message."""
	
	@web.authenticated
	@gen.coroutine
	def get(self):
	    user = self.get_current_user()
	    
	    self.set_cookie('ended-session', str(time.time()))
	    
	    html = self.render_template('end-session.html',
	        user=user,
	    )
	    self.finish(html)
	    
class DoneHandler(BaseHandler):
	@gen.coroutine
	def get(self):
	    html = self.render_template('session-done.html')
	    self.finish(html)

class SetUserFinishedHandler(BaseHandler):
	
	@web.authenticated
	@gen.coroutine
	def post(self, name):
		current_user = self.get_current_user()
		
		if name == current_user.name:
			user = self.find_user(name)
			if user is None:
				raise web.HTTPError(404)
			else:
				setattr(user, 'exam_finished', datetime.datetime.utcnow())
				
				self.db.commit()
				self.write('ok')
		else:
			raise web.HTTPError(401)


default_handlers = [
    (r'/?', RootHandler),
    (r'/home', HomeHandler),
    (r'/admin', AdminHandler),
    (r'/spawn', SpawnHandler),
    (r'/token', TokenPageHandler),
    (r'/error/(\d+)', ProxyErrorHandler),
    
    (r'/select-exam', SelectExamHandler),
    (r'/end-session', EndSessionHandler),
    (r'/done', DoneHandler),
    (r'/set-user-finished/([^/]+)', SetUserFinishedHandler)
]
