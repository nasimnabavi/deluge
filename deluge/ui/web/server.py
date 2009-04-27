#
# deluge/ui/web/webui.py
#
# Copyright (C) 2009 Damien Churchill <damoxc@gmail.com>
#
# Deluge is free software.
#
# You may redistribute it and/or modify it under the terms of the
# GNU General Public License, as published by the Free Software
# Foundation; either version 3 of the License, or (at your option)
# any later version.
#
# deluge is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with deluge.    If not, write to:
#   The Free Software Foundation, Inc.,
#   51 Franklin Street, Fifth Floor
#   Boston, MA    02110-1301, USA.
#

import os
import time
import locale
import shutil
import urllib
import gettext
import hashlib
import logging
import tempfile
import mimetypes
import pkg_resources

from twisted.application import service, internet
from twisted.internet import reactor, error
from twisted.web import http, resource, server, static

from deluge import common, component
from deluge.configmanager import ConfigManager
from deluge.log import setupLogger, LOG as _log
from deluge.ui import common as uicommon
from deluge.ui.tracker_icons import TrackerIcons
from deluge.ui.web.common import Template
from deluge.ui.web.json_api import JSON, WebApi
from deluge.ui.web.pluginmanager import PluginManager
log = logging.getLogger(__name__)

# Initialize gettext
try:
    locale.setlocale(locale.LC_ALL, "")
    if hasattr(locale, "bindtextdomain"):
        locale.bindtextdomain("deluge", pkg_resources.resource_filename("deluge", "i18n"))
    if hasattr(locale, "textdomain"):
        locale.textdomain("deluge")
    gettext.bindtextdomain("deluge", pkg_resources.resource_filename("deluge", "i18n"))
    gettext.textdomain("deluge")
    gettext.install("deluge", pkg_resources.resource_filename("deluge", "i18n"))
except Exception, e:
    log.error("Unable to initialize gettext/locale: %s", e)

_ = gettext.gettext

current_dir = os.path.dirname(__file__)

CONFIG_DEFAULTS = {
    "port": 8112,
    "theme": "slate",
    "pwd_salt": "16f65d5c79b7e93278a28b60fed2431e",
    "pwd_md5": "2c9baa929ca38fb5c9eb5b054474d1ce",
    "base": "",
    "sessions": [],
    "sidebar_show_zero": False,
    "sidebar_show_trackers": False,
    "show_keyword_search": False,
    "show_sidebar": True,
    "https": False
}

def rpath(path):
    """Convert a relative path into an absolute path relative to the location
    of this script.
    """
    return os.path.join(current_dir, path)

class Config(resource.Resource):
    """
    Writes out a javascript file that contains the WebUI configuration
    available as Deluge.Config.
    """
    
    def render(self, request):
        return """Deluge = {
    author: 'Damien Churchill <damoxc@gmail.com>',
    version: '1.2-dev',
    config: %s
}""" % common.json.dumps(component.get("DelugeWeb").config.config)

class GetText(resource.Resource):
    def render(self, request):
        request.setHeader("content-type", "text/javascript; encoding=utf-8")
        template = Template(filename=rpath("gettext.js"))
        return template.render()

class Upload(resource.Resource):
    """
    Twisted Web resource to handle file uploads
    """
    
    def render(self, request):
        """
        Saves all uploaded files to the disk and returns a list of filenames,
        each on a new line.
        """
        
        # Block all other HTTP methods.
        if request.method != "POST":
            request.setResponseCode(http.NOT_ALLOWED)
            return ""
        
        if "file" not in request.args:
            request.setResponseCode(http.OK)
            return ""
        
        tempdir = os.path.join(tempfile.gettempdir(), "delugeweb")
        if not os.path.isdir(tempdir):
            os.mkdir(tempdir)

        filenames = []
        for upload in request.args.get("file"):
            fd, fn = tempfile.mkstemp('.torrent', dir=tempdir)
            os.write(fd, upload)
            os.close(fd)
            filenames.append(fn)
        request.setHeader("content-type", "text/plain")
        request.setResponseCode(http.OK)
        return "\n".join(filenames)

class Render(resource.Resource):

    def getChild(self, path, request):
        request.render_file = path
        return self
    
    def render(self, request):
        if not hasattr(request, "render_file"):
            request.setResponseCode(http.INTERNAL_SERVER_ERROR)
            return ""

        filename = os.path.join("render", request.render_file)
        template = Template(filename=rpath(filename))
        request.setHeader("content-type", "text/html")
        request.setResponseCode(http.OK)
        return template.render()

class Tracker(resource.Resource):
    tracker_icons = TrackerIcons()
    
    def getChild(self, path, request):
        request.tracker_name = path
        return self
    
    def render(self, request):
        headers = {}
        filename = self.tracker_icons.get(request.tracker_name)
        if filename:
            request.setHeader("cache-control",
                              "public, must-revalidate, max-age=86400")
            if filename.endswith(".ico"):
                request.setHeader("content-type", "image/x-icon")
            elif filename.endwith(".png"):
                request.setHeader("content-type", "image/png")
            data = open(filename, "rb")
            request.setResponseCode(http.OK)
            return data.read()
        else:
            request.setResponseCode(http.NOT_FOUND)
            return ""

class Flag(resource.Resource):
    def getChild(self, path, request):
        request.country = path
        return self
    
    def render(self, request):
        headers = {}
        path = ("data", "pixmaps", "flags", request.country.lower() + ".png")
        filename = pkg_resources.resource_filename("deluge",
                                                   os.path.join(*path))
        if os.path.exists(filename):
            request.setHeader("cache-control",
                              "public, must-revalidate, max-age=86400")
            request.setHeader("content-type", "image/png")
            data = open(filename, "rb")
            request.setResponseCode(http.OK)
            return data.read()
        else:
            request.setResponseCode(http.NOT_FOUND)
            return ""

class LookupResource(resource.Resource, component.Component):
    
    def __init__(self, name, *directories):        
        resource.Resource.__init__(self)
        component.Component.__init__(self, name)
        self.__directories = directories
    
    @property
    def directories(self):
        return self.__directories
    
    def getChild(self, path, request):
        request.path = path
        return self
    
    def render(self, request):
        log.debug("Requested path: '%s'", request.path)
        for lookup in self.directories:
            if request.path in os.listdir(lookup):
                path = os.path.join(lookup, request.path)
                log.debug("Serving path: '%s'", path)
                mime_type = mimetypes.guess_type(path)
                request.setHeader("content-type", mime_type[0])
                return open(path, "rb").read()
        request.setResponseCode(http.NOT_FOUND)
        return "<h1>404 - Not Found</h1>"

class TopLevel(resource.Resource):
    addSlash = True
    
    __stylesheets = [
        "/css/ext-all.css",
        "/css/ext-extensions.css",
        "/css/deluge.css"
    ]
    
    __scripts = [
        "/js/ext-base.js",
        "/js/ext-all.js",
        "/js/ext-extensions.js",
        "/config.js",
        "/gettext.js",
        "/js/deluge-yc.js"
    ]
    
    __debug_scripts = [
        "/js/ext-base.js",
        "/js/ext-all-debug.js",
        "/js/ext-extensions-debug.js",
        "/config.js",
        "/gettext.js",
        "/js/Deluge.js",
        "/js/Deluge.Formatters.js",
        "/js/Deluge.Menus.js",
        "/js/Deluge.Events.js",
        "/js/Deluge.Client.js",
        "/js/Deluge.ConnectionManager.js",
        "/js/Deluge.Details.js",
        "/js/Deluge.Details.Status.js",
        "/js/Deluge.Details.Details.js",
        "/js/Deluge.Details.Files.js",
        "/js/Deluge.Details.Peers.js",
        "/js/Deluge.Details.Options.js",
        "/js/Deluge.Keys.js",
        "/js/Deluge.Login.js",
        "/js/Deluge.Preferences.js",
        "/js/Deluge.Preferences.Downloads.js",
        "/js/Deluge.Preferences.Network.js",
        "/js/Deluge.Preferences.Bandwidth.js",
        "/js/Deluge.Preferences.Interface.js",
        "/js/Deluge.Preferences.Other.js",
        "/js/Deluge.Preferences.Daemon.js",
        "/js/Deluge.Preferences.Queue.js",
        "/js/Deluge.Preferences.Proxy.js",
        "/js/Deluge.Preferences.Notification.js",
        "/js/Deluge.Preferences.Plugins.js",
        "/js/Deluge.Sidebar.js",
        "/js/Deluge.Statusbar.js",
        "/js/Deluge.Toolbar.js",
        "/js/Deluge.Torrents.js",
        "/js/Deluge.UI.js"
    ]
    
    def __init__(self):
        resource.Resource.__init__(self)
        self.putChild("config.js", Config())
        self.putChild("css", LookupResource("Css", rpath("css")))
        self.putChild("gettext.js", GetText())
        self.putChild("flag", Flag())
        self.putChild("icons", LookupResource("Icons", rpath("icons")))
        self.putChild("images", LookupResource("Images", rpath("images")))
        self.putChild("js", LookupResource("Javascript", rpath("js")))
        self.putChild("json", JSON())
        self.putChild("upload", Upload())
        self.putChild("render", Render())
        self.putChild("themes", static.File(rpath("themes")))
        self.putChild("tracker", Tracker())
        
        theme = component.get("DelugeWeb").config["theme"]
        self.__stylesheets.append("/css/xtheme-%s.css" % theme)

    @property
    def scripts(self):
        return self.__scripts
    
    @property
    def debug_scripts(self):
        return self.__debug_scripts
    
    @property
    def stylesheets(self):
        return self.__stylesheets
    
    def getChild(self, path, request):
        if path == "":
            return self
        else:
            return resource.Resource.getChild(self, path, request)

    def render(self, request):
        if request.args.get('debug', ['false'])[-1] == 'true':
            scripts = self.debug_scripts[:]
        else:
            scripts = self.scripts[:]
            
        template = Template(filename=rpath("index.html"))
        request.setHeader("content-type", "text/html; charset=utf-8")
        return template.render(scripts=scripts, stylesheets=self.stylesheets)

class DelugeWeb(component.Component):
    
    def __init__(self):
        super(DelugeWeb, self).__init__("DelugeWeb")
        self.config = ConfigManager("web.conf", CONFIG_DEFAULTS)
        
        self.top_level = TopLevel()
        self.site = server.Site(self.top_level)
        self.port = self.config["port"]
        self.web_api = WebApi()
        
        # Since twisted assigns itself all the signals may as well make
        # use of it.
        reactor.addSystemEventTrigger("after", "shutdown", self.shutdown)
        
        # Initalize the plugins
        self.plugins = PluginManager()

    def start(self):
        log.info("%s %s.", _("Starting server in PID"), os.getpid())
        reactor.listenTCP(self.port, self.site)
        log.info("serving on %s:%s view at http://127.0.0.1:%s", "0.0.0.0",
            self.port, self.port)
        reactor.run()

    def shutdown(self):
        log.info("Shutting down webserver")
        log.debug("Saving configuration file")
        self.config.save()

if __name__ == "__builtin__":
    deluge_web = DelugeWeb()
    application = service.Application("DelugeWeb")
    sc = service.IServiceCollection(application)
    i = internet.TCPServer(deluge_web.port, deluge_web.site)
    i.setServiceParent(sc)
elif __name__ == "__main__":
    deluge_web = DelugeWeb()
    deluge_web.start()