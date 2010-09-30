#!/usr/bin/python
#
# Pulp Repo management module
#
# Copyright (c) 2010 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public License,
# version 2 (GPLv2). There is NO WARRANTY for this software, express or
# implied, including the implied warranties of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. You should have received a copy of GPLv2
# along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.
#
# Red Hat trademarks are not licensed under GPLv2. No permission is
# granted to use or replicate Red Hat trademarks that are incorporated
# in this software or its documentation.

import os
from gettext import gettext as _

from pulp.client import auth_utils
from pulp.client.connection import UserConnection
from pulp.client.core.base import BaseCore, Action

# base auth action class ------------------------------------------------------

class AuthAction(Action):

    def connections(self):
        return {'authconn': UserConnection}

    def setup_parser(self):
        self.parser.add_option('--username', dest='username',
                               help=_('pulp account username'))
        self.parser.add_option('--password', dest='password',
                               help=_('pulp account password'))

# auth actions ----------------------------------------------------------------

class Login(AuthAction):

    name = 'login'
    plug = 'stores user credentials on this machine'

    def run(self):
        #username = self.get_required_option('username')
        #password = self.get_required_option('password')
        # Retrieve the certificate information from the server
        cert_dict = self.authconn.admin_certificate()
        # Determine the destination and store the cert information there
        if not os.path.exists(auth_utils.admin_cert_dir()):
            os.makedirs(auth_utils.admin_cert_dir())
        # Write the certificate data
        cert_filename, key_filename = auth_utils.admin_cert_paths()
        f = open(cert_filename, 'w')
        f.write(cert_dict['certificate'])
        f.close()
        f = open(key_filename, 'w')
        f.write(cert_dict['private_key'])
        f.close()
        print _('user credentials successfully stored at [%s]') % \
                auth_utils.admin_cert_dir()


class Logout(AuthAction):

    name = 'logout'
    plug = 'removes stored user credentials on this machine'

    def run(self):
        # Determine the destination and store the cert information there
        cert_filename, key_filename = auth_utils.admin_cert_paths()
        # Remove the certificate and private key files
        if os.path.exists(cert_filename):
            os.remove(cert_filename)
        if os.path.exists(key_filename):
            os.remove(key_filename)
        print _('user credentials removed from [%s]') % auth_utils.admin_cert_dir()

# auth command ----------------------------------------------------------------

class Auth(BaseCore):

    name = 'auth'
    _default_actions = ('login', 'logout')

    def __init__(self, actions=_default_actions, action_state={}):
        super(Auth, self).__init__(actions, action_state)
        self.login = Login()
        self.logout = Logout()

    def short_description(self):
        return _('stores authentication credentials for the user on the machine')


command_class = auth = Auth
