###################
##   TODO LIST   ##
###################
# _ Change users managment to database
# _ Repair the tor proxy
# _ Add feedback to the 'add user' process
# Lors du clic sur Nothing to do together, verifier que tout est selectionne ou que rien n'est selectionne
# appel de la fonction : duplicates.merge_duplicates(col, dup_col, publications_ids...)

import os
import json
import pprint
import urllib
import urllib2
import hashlib
import pystache
import subprocess
from scholarScape.server.rpc import scholarScape as RPCServer
from scholarScape.server.utils import config, scholarize
from datetime import date
from contextlib import nested
from txjsonrpc.web import jsonrpc
from urllib import quote_plus as qp
from pymongo import Connection, errors
from bson import json_util, objectid
from zope.interface import implements, Interface
from twisted.protocols import basic
from twisted.web import resource, server, static
from twisted.web.server import NOT_DONE_YET
from twisted.application import service, internet
from twisted.cred import checkers, credentials, portal
from itertools import permutations
from scholarScape.scholar.scholar import duplicates

class IUser(Interface):
    '''A user account.
    '''

    def getUserName(self):
        '''Returns the name of the user account.
        '''

class User(object):
    implements(IUser)

    def __init__(self, name):
        self.__name = name

    def getUserName(self):
        return self.__name

# Read config file and fill in mongo and scrapyd config files with custom values
# Try to connect to DB, send egg to scrapyd server

# Check if the DB is available
try :
    Connection("mongodb://" + config.config['mongo']['user'] + ":" +
            config.config['mongo']['passwd'] + "@" + config.config['mongo']['host'] + ":" + str(config.config['mongo']['port']) + "/" + config.config['mongo']['database'] )
except errors.AutoReconnect:
    print "Could not connect to mongodb server", config.config['mongo']
    exit()

# Render the pipeline template
print "Rendering pipelines.py with values from config.json..."
with nested(open("scholarScape/scholar/scholar/pipelines-template.py", "r"), open("scholarScape/scholar/scholar/pipelines.py", "w")) as (template, generated):
    generated.write(pystache.render(template.read(), config.config['mongo']))

# Render the scrapy cfg
print "Rendering scrapy.cfg with values from config.json..."
with nested(open("scholarScape/scholar/scrapy-template.cfg", "r"), open("scholarScape/scholar/scrapy.cfg", "w")) as (template, generated):
    generated.write(pystache.render(template.read(), config.config['scrapyd']))

# Deploy the egg
print "Sending scholarScrape's scrapy egg to scrapyd server..."
os.chdir("scholarScape/scholar")
p = subprocess.Popen(['scrapy', 'deploy'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
output, errors = p.communicate()
print output, errors
try :
    output = json.loads(output)
    if output['status'] != "ok" :
        print "There was a problem sending the scrapy egg."
        print output, errors
        exit()
except ValueError:
    print "There was a problem sending the scrapy egg."
    print output, errors
    exit()
print "The egg was successfully sent to scrapyd server", config.config['scrapyd']['host'], "on port", config.config['scrapyd']['port']
os.chdir("../..")

print "Starting the server"

class Home(resource.Resource):
    isLeaf = False

    def __init__(self):
        resource.Resource.__init__(self)

    def getChild(self, name, request) :
        if name == '' :
            return self
        return resource.Resource.getChild(self, name, request)

    def render_GET(self, request):
        request.setHeader('Content-Type', 'text/html; charset=utf-8')
        # Get session user
        session = request.getSession()
        user = session.getComponent(IUser)
        # If the user is not connected, redirection to the login page
        if not user :
            login = ''
            launch = False
            explore = False
            admin = False
            logout = False
            path = os.path.join(config.root_dir, config.web_client_dir, 'login.html')
        else:
            # Send the salt and hashed login as an hidden tag
            login = hashlib.md5(config.config['salt'] + user.getUserName()).hexdigest()
            role = config.users[user.getUserName()]['role']
            if role == 'explorer' :
                launch = False
                explore = True
                admin = False
                logout = True
            elif role == 'launcher' :
                launch = True
                explore = True
                admin = False
                logout = True
            elif role == 'admin' :
                launch = True
                explore = True
                admin = True
                logout = True
            if 'page' in request.args and request.args['page'][0] != 'layout':
                # If the page is login then reset user session
                if request.args['page'][0] == 'login' :
                    session.unsetComponent(IUser)
                    launch = False
                    explore = False
                    admin = False
                    logout = False
                path = os.path.join(config.root_dir, config.web_client_dir, "%s.html" % request.args['page'][0])
                path = path if os.path.exists(path) else os.path.join(config.root_dir, config.web_client_dir, '404.html')
            else:
                path = os.path.join(config.root_dir, config.web_client_dir, 'index.html')
        layout_path = os.path.join(config.root_dir, config.web_client_dir, 'layout.html')
        with nested(open(path, 'r'), open(layout_path, "r")) as (fpage, flayout):
            layout = flayout.read().decode('utf-8')
            page = fpage.read().decode('utf-8')
            content = pystache.render(layout, {'contenu' : page, 'login' : login, 'launch' : launch, 'explore' : explore, 'admin' : admin, 'logout' : logout})
        return content.encode('utf-8')

    def render_POST(self, request) :
        # Collect common args
        page = request.args['page'][0]
        login = request.args['form_login'][0]
        password = request.args['form_password'][0]
        # If the request comes from the login page
        if page == 'login' :
            # If the user exists and the password matches
            if login in config.users and config.users[login]['password'] == password :
                user = User(login)
                session = request.getSession()
                session.setComponent(IUser, user)
                request.args['page'][0] = 'index'
                return self.render_GET(request)
            else :
                request.args['page'][0] = 'login'
                return self.render_GET(request)
        # If the request comes from the admin page
        elif page == 'admin' :
            user_role = request.args['form_user_role'][0]
            collections = [request.args[collection][0] for collection in request.args if collection.startswith('project_')]
            # Check if user doesn't exist
            if login not in config.users :
                # Add user to the json file
                config.users[login] = {'password' : password, 'role' : user_role, 'collections' : collections}
                file = open('users.json', 'w')
                json.dump(config.users, file, sort_keys = True, indent = 4)
                file.close()
            return self.render_GET(request)

def _connect_to_db():
    """ attempt to connect to mongo database based on value in config_file
        return db object
    """
    host   = config.config['mongo']['host']
    port   = config.config['mongo']['port']
    db     = config.config['mongo']['database']
    user   = config.config['mongo']['user']
    passwd = config.config['mongo']['passwd']

    try :
        c = Connection("mongodb://" + user +  ":" + passwd  + "@" + host + ":" + str(port) + "/" + db)
        return c[db]
    except :
        print 'Could not connect to the database'
        exit()

class Downloader(resource.Resource):
    isLeaf = True
    def render_GET(self, request):
        try :
            file_path = request.args['file'][0]
            request.setHeader('Content-Disposition', 'attachment;filename=' + request.args['file'][0].split('/')[-1])
            return open(file_path).read()
        except Exception as e:
            return 'There was an error : ' + str(e)

db = _connect_to_db()

root = Home()
root.putChild('downloader', Downloader())
root.putChild('js', static.File(os.path.join(config.root_dir, config.web_client_dir, 'js')))
root.putChild('css', static.File(os.path.join(config.root_dir, config.web_client_dir, 'css')))
root.putChild('fonts', static.File(os.path.join(config.root_dir, config.web_client_dir, 'fonts')))
root.putChild('images', static.File(os.path.join(config.root_dir, config.web_client_dir, 'images')))
manageJson = RPCServer(db)
root.putChild('json', manageJson)
data = static.File('data')
root.putChild('data', data)

application = service.Application('ScholarScape server. Receives JSON-RPC Requests and also serves the client.')
site = server.Site(root)
srv = internet.TCPServer(config.config['twisted']['port'], site)
srv.setServiceParent(application)
