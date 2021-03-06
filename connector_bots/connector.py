# -*- coding: utf-8 -*-
##############################################################################
#
#    Copyright 2014 credativ Ltd
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################

from openerp.osv import orm, fields
from openerp.addons.connector.connector import Environment, install_in_connector
from openerp.addons.connector.checkpoint import checkpoint

install_in_connector()

def get_environment(session, model_name, backend_id):
    """ Create an environment to work with.  """
    backend_record = session.browse('bots.backend', backend_id)
    env = Environment(backend_record, session, model_name)
    return env

class BotsBinding(orm.AbstractModel):
    """ Abstract Model for the Bindings."""

    _name = 'bots.binding'
    _inherit = 'external.binding'
    _description = 'Bots Binding (abstract)'

    _columns = {
        'backend_id': fields.many2one(
            'bots.backend',
            'Bots Backend',
            required=True,
            ondelete='restrict'),
        'bots_id': fields.char('ID on Bots'),
    }

def add_checkpoint(session, model_name, record_id, backend_id):
    """ Add a row in the model ``connector.checkpoint`` for a record,
    meaning it has to be reviewed by a user.

    :param session: current session
    :type session: :py:class:`openerp.addons.connector.session.ConnectorSession`
    :param model_name: name of the model of the record to be reviewed
    :type model_name: str
    :param record_id: ID of the record to be reviewed
    :type record_id: int
    :param backend_id: ID of the Bots Backend
    :type backend_id: int
    """
    return checkpoint.add_checkpoint(session, model_name, record_id, 'bots.backend', backend_id)
