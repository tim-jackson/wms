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

from openerp.osv import orm, fields, osv
from openerp import pooler, netsvc, SUPERUSER_ID
from openerp.tools.translate import _
from openerp.tools.misc import DEFAULT_SERVER_DATETIME_FORMAT

from openerp.addons.connector.session import ConnectorSession
from openerp.addons.connector.queue.job import job
from openerp.addons.connector.exception import JobError, NoExternalId, MappingError
from openerp.addons.connector.unit.synchronizer import ExportSynchronizer
from openerp.addons.connector.event import on_record_create
from openerp.addons.connector_wms.event import on_picking_out_available, on_picking_in_available, on_picking_out_cancel, on_picking_in_cancel

from openerp.addons.stock import stock_picking as stock_StockPicking

from .unit.binder import BotsModelBinder
from .unit.backend_adapter import BotsCRUDAdapter
from .backend import bots
from .connector import get_environment, add_checkpoint

import json
import traceback
from datetime import datetime
import re

class StockPickingIn(orm.Model):
    _inherit = 'stock.picking.in'

    # we redefine the min_date field, so these functions have to exist
    def get_min_max_date(self, *args, **kwargs):
        return super(StockPickingIn, self).get_min_max_date(*args, **kwargs)

    def _set_minimum_date(self, *args, **kwargs):
        return super(StockPickingIn, self)._set_minimum_date(*args, **kwargs)

    def _get_stock_move_changes(self, *args, **kwargs):
        # self is a 'stock.move' object, so we have no way of finding out where
        # we are in the inheritance DAG
        self = self.pool.get('stock.picking.in')
        return stock_StockPicking._get_stock_move_changes(self, *args, **kwargs)

    _columns = {
            'bots_customs': fields.boolean('Bonded Goods', help='If this picking is subject to duties.', states={'done':[('readonly', True)], 'cancel':[('readonly',True)], 'assigned':[('readonly',True)]}),
            'move_lines': fields.one2many('stock.move', 'picking_id', 'Internal Moves', readonly=True, states={'draft':[('readonly',False)], 'confirmed':[('readonly',False)]},),
            'partner_id': fields.many2one('res.partner', 'Destination Address ', help="Optional address where goods are to be delivered, specifically used for allotment", readonly=True, states={'draft':[('readonly',False)]},),
            'min_date': fields.function(
                get_min_max_date,
                fnct_inv=_set_minimum_date, multi='min_max_date',
                store={
                    'stock.move': (
                        _get_stock_move_changes,
                        ['date_expected', 'picking_id'], 10,
                    )
                },
                type='datetime', string='Scheduled Time', select=True,
                readonly=True, states={'draft':[('readonly',False)], 'confirmed':[('readonly',False)]},
                help="Scheduled time for the shipment to be processed"
            ),
        }

    _defaults = {
            'bots_customs':  lambda *a: False,
        }

    def bots_test_exported(self, cr, uid, ids, doraise=False, cancel=False, context=None):
        context = context or {}
        if context.get('wms_bots', False):
            return False
        exported = self.pool.get('bots.stock.picking.in').search(cr, SUPERUSER_ID, [('openerp_id', 'in', ids), ('move_lines.state', 'not in', ('done', 'cancel')), ('bots_override', '=', False)], context=context)
        if exported and cancel:
            exported_obj = self.pool.get('bots.stock.picking.in').browse(cr, uid, exported, context=context)
            exported = [x.id for x in exported_obj if not x.bots_id or not x.backend_id.feat_picking_in_cancel]
        if exported and doraise:
            raise osv.except_osv(_('Error!'), _('This picking has been exported to an external WMS and cannot be modified directly in OpenERP.'))
        return exported or False

    def cancel_assign(self, cr, uid, ids, context=None):
        self.bots_test_exported(cr, uid, ids, doraise=True, cancel=True, context=context)
        ctx = context and context.copy() or {}
        ctx.update({'from_picking': True})
        res = super(StockPickingIn, self).cancel_assign(cr, uid, ids, context=ctx)
        session = ConnectorSession(cr, uid, context=None)
        for id in ids:
            on_picking_in_cancel.fire(session, self._name, id)
        return res

    def action_cancel(self, cr, uid, ids, context=None):
        self.bots_test_exported(cr, uid, ids, doraise=True, cancel=True, context=context)
        ctx = context and context.copy() or {}
        ctx.update({'from_picking': True})
        res = super(StockPickingIn, self).action_cancel(cr, uid, ids, context=ctx)
        return res

    def action_done(self, cr, uid, ids, context=None):
        self.bots_test_exported(cr, uid, ids, doraise=True, context=context)
        res = super(StockPickingIn, self).action_done(cr, uid, ids, context=context)
        return res

    def unlink(self, cr, uid, ids, context=None):
        self.bots_test_exported(cr, uid, ids, doraise=True, context=context)
        res = super(StockPickingIn, self).unlink(cr, uid, ids, context=context)
        return res

class StockPickingOut(orm.Model):
    _inherit = 'stock.picking.out'

    # we redefine the min_date field, so these functions have to exist
    def get_min_max_date(self, *args, **kwargs):
        return super(StockPickingOut, self).get_min_max_date(*args, **kwargs)

    def _set_minimum_date(self, *args, **kwargs):
        return super(StockPickingOut, self)._set_minimum_date(*args, **kwargs)

    def _get_stock_move_changes(self, *args, **kwargs):
        # self is a 'stock.move' object, so we have no way of finding out where
        # we are in the inheritance DAG
        self = self.pool.get('stock.picking.out')
        return stock_StockPicking._get_stock_move_changes(self, *args, **kwargs)

    _columns = {
            'bots_customs': fields.boolean('Bonded Goods', help='If this picking is subject to duties.', states={'done':[('readonly', True)], 'cancel':[('readonly',True)], 'assigned':[('readonly',True)]}),
            'move_lines': fields.one2many('stock.move', 'picking_id', 'Internal Moves', readonly=True, states={'draft':[('readonly',False)], 'confirmed':[('readonly',False)]},),
            'partner_id': fields.many2one('res.partner', 'Destination Address ', help="Optional address where goods are to be delivered, specifically used for allotment", readonly=True, states={'draft':[('readonly',False)]},),
            'min_date': fields.function(
                get_min_max_date,
                fnct_inv=_set_minimum_date, multi='min_max_date',
                store={
                    'stock.move': (
                        _get_stock_move_changes,
                        ['date_expected', 'picking_id'], 10,
                    )
                },
                type='datetime', string='Scheduled Time', select=True,
                readonly=True, states={'draft':[('readonly',False)], 'confirmed':[('readonly',False)]},
                help="Scheduled time for the shipment to be processed"
            ),
        }

    _defaults = {
            'bots_customs':  lambda *a: False,
        }

    def bots_test_exported(self, cr, uid, ids, doraise=False, cancel=False, context=None):
        context = context or {}
        if context.get('wms_bots', False):
            return False
        exported = self.pool.get('bots.stock.picking.out').search(cr, SUPERUSER_ID, [('openerp_id', 'in', ids), ('move_lines.state', 'not in', ('done', 'cancel')), ('bots_override', '=', False)], context=context)
        if exported and cancel:
            exported_obj = self.pool.get('bots.stock.picking.out').browse(cr, uid, exported, context=context)
            exported = [x.id for x in exported_obj if not x.bots_id or not x.backend_id.feat_picking_out_cancel]
        if exported and doraise:
            raise osv.except_osv(_('Error!'), _('This picking has been exported to an external WMS and cannot be modified directly in OpenERP.'))
        return exported or False

    def cancel_assign(self, cr, uid, ids, context=None):
        self.bots_test_exported(cr, uid, ids, doraise=True, cancel=True, context=context)
        ctx = context and context.copy() or {}
        ctx.update({'from_picking': True})
        res = super(StockPickingOut, self).cancel_assign(cr, uid, ids, context=ctx)
        session = ConnectorSession(cr, uid, context=None)
        for id in ids:
            on_picking_out_cancel.fire(session, self._name, id)
        return res

    def action_cancel(self, cr, uid, ids, context=None):
        self.bots_test_exported(cr, uid, ids, doraise=True, cancel=True, context=context)
        ctx = context and context.copy() or {}
        ctx.update({'from_picking': True})
        res = super(StockPickingOut, self).action_cancel(cr, uid, ids, context=ctx)
        return res

    def action_done(self, cr, uid, ids, context=None):
        self.bots_test_exported(cr, uid, ids, doraise=True, context=context)
        res = super(StockPickingOut, self).action_done(cr, uid, ids, context=context)
        return res

    def unlink(self, cr, uid, ids, context=None):
        self.bots_test_exported(cr, uid, ids, doraise=True, context=context)
        res = super(StockPickingOut, self).unlink(cr, uid, ids, context=context)
        return res

class StockPicking(orm.Model):
    _inherit = 'stock.picking'

    _columns = {
            'bots_customs': fields.boolean('Bonded Goods', help='If this picking is subject to duties.', states={'done':[('readonly', True)], 'cancel':[('readonly',True)], 'assigned':[('readonly',True)]}),
        }

    _defaults = {
            'bots_customs':  lambda *a: False,
        }

    def bots_test_exported(self, cr, uid, ids, doraise=False, cancel=False, context=None):
        context = context or {}
        if context.get('wms_bots', False):
            return False
        exported = []
        for pick in self.browse(cr, uid, ids, context=context):
            if pick.type == 'in':
                MODEL = 'bots.stock.picking.in'
                PARAM = 'feat_picking_in_cancel'
            elif pick.type == 'out':
                MODEL = 'bots.stock.picking.out'
                PARAM = 'feat_picking_out_cancel'
            else:
                continue
            exported.extend(self.pool.get(MODEL).search(cr, SUPERUSER_ID, [('openerp_id', 'in', ids), ('move_lines.state', 'not in', ('done', 'cancel')), ('bots_override', '=', False)], context=context))
            if exported and cancel:
                exported_obj = self.pool.get(MODEL).browse(cr, uid, exported, context=context)
                exported = [x.id for x in exported_obj if not x.bots_id or not getattr(x.backend_id, PARAM)]
            if exported and doraise:
                raise osv.except_osv(_('Error!'), _('This picking has been exported, or is pending export, to an external WMS and cannot be modified directly in OpenERP.'))
        return exported or False

    def cancel_assign(self, cr, uid, ids, context=None):
        self.bots_test_exported(cr, uid, ids, doraise=True, cancel=True, context=context)
        ctx = context and context.copy() or {}
        ctx.update({'from_picking': True})
        res = super(StockPicking, self).cancel_assign(cr, uid, ids, context=ctx)
        session = ConnectorSession(cr, uid, context=None)
        for pick in self.browse(cr, uid, ids, context=context):
            if pick.type == 'in':
                on_picking_in_cancel.fire(session, self._name, id)
            elif pick.type == 'out':
                on_picking_out_cancel.fire(session, self._name, id)
            else:
                continue
        return res

    def action_cancel(self, cr, uid, ids, context=None):
        self.bots_test_exported(cr, uid, ids, doraise=True, cancel=True, context=context)
        ctx = context and context.copy() or {}
        ctx.update({'from_picking': True})
        res = super(StockPicking, self).action_cancel(cr, uid, ids, context=ctx)
        return res

    def action_done(self, cr, uid, ids, context=None):
        self.bots_test_exported(cr, uid, ids, doraise=True, context=context)
        res = super(StockPicking, self).action_done(cr, uid, ids, context=context)
        return res

    def unlink(self, cr, uid, ids, context=None):
        self.bots_test_exported(cr, uid, ids, doraise=True, context=context)
        res = super(StockPicking, self).unlink(cr, uid, ids, context=context)
        return res

class StockMove(orm.Model):
    _inherit = 'stock.move'

    def _bots_test_exported(self, cr, uid, ids, field_name, arg, context=None):
        res = {}
        for id in ids:
            res[id] = self.bots_test_exported(cr, uid, [id], doraise=False, cancel=False, context=context) and True or False
        return res

    _columns = {
        'pick_state': fields.related(
                'picking_id', 'state',
                type='char',
                readonly=True,
                string='Picking state',
            ),
        'bots_exported': fields.function(_bots_test_exported, type='boolean', string='Exported to 3PL', readonly=True, help="Has this move been exported to 3PL/Bots"),
    }

    def bots_test_exported(self, cr, uid, ids, doraise=False, cancel=False, context=None):
        exported = False
        for move in self.browse(cr, uid, ids, context=context):
            if move.picking_id and move.picking_id.type == 'out':
                exported = self.pool.get('stock.picking.out').bots_test_exported(cr, uid, [move.picking_id.id], doraise=doraise, cancel=cancel, context=context)
            elif move.picking_id and move.picking_id.type == 'in':
                exported = self.pool.get('stock.picking.in').bots_test_exported(cr, uid, [move.picking_id.id], doraise=doraise, cancel=cancel, context=context)
            if exported:
                return exported
        return False

    def cancel_assign(self, cr, uid, ids, context=None):
        context = context or {}
        if not context.get('from_picking') and self.bots_test_exported(cr, uid, ids, doraise=False, cancel=False, context=context):
            raise osv.except_osv(_('Error!'), _('This move has been exported to an external WMS and cannot unassigned directly. Unassign the picking.'))
        res = super(StockMove, self).cancel_assign(cr, uid, ids, context=context)
        return res

    def action_cancel(self, cr, uid, ids, context=None):
        context = context or {}
        if not context.get('from_picking') and self.bots_test_exported(cr, uid, ids, doraise=False, cancel=False, context=context):
            raise osv.except_osv(_('Error!'), _('This move has been exported to an external WMS and cannot be cancelled directly. Cancel the picking.'))
        res = super(StockMove, self).action_cancel(cr, uid, ids, context=context)
        return res

    def action_done(self, cr, uid, ids, context=None):
        self.bots_test_exported(cr, uid, ids, doraise=True, context=context)
        res = super(StockMove, self).action_done(cr, uid, ids, context=context)
        return res

    def action_scrap(self, cr, uid, ids, product_qty, location_id, context=None):
        self.bots_test_exported(cr, uid, ids, doraise=True, context=context)
        res = super(StockMove, self).action_scrap(cr, uid, ids, product_qty, location_id, context=context)
        return res

    def unlink(self, cr, uid, ids, context=None):
        self.bots_test_exported(cr, uid, ids, doraise=True, context=context)
        res = super(StockMove, self).unlink(cr, uid, ids, context=context)
        return res

class BotsStockPickingOut(orm.Model):
    _name = 'bots.stock.picking.out'
    _inherit = 'bots.binding'
    _inherits = {'stock.picking.out': 'openerp_id'}
    _description = 'Bots Stock Picking Out'

    _columns = {
        'openerp_id': fields.many2one('stock.picking.out',
                                      string='Stock Picking Out',
                                      required=True,
                                      ondelete='restrict'),
        'warehouse_id': fields.many2one('bots.warehouse',
                                      string='Bots Warehouse',
                                      required=True,
                                      ondelete='restrict'),
        'bots_override': fields.boolean('Override Bots Restrictions', help='Allow all normal Bots constraints to be ignored, eg when completing or cancelling.'),
        }

    _sql_constraints = [
        ('bots_picking_out_uniq', 'unique(backend_id, openerp_id)',
         'A Bots picking already exists for this picking for the same backend.'),
    ]

    def reexport_order(self, cr, uid, ids, context=None):
        session = ConnectorSession(cr, uid, context=context)
        for id in ids:
            export_picking_available.delay(session, self._name, id)
        return True

    def reexport_cancel(self, cr, uid, ids, context=None):
        session = ConnectorSession(cr, uid, context=context)
        for id in ids:
            export_picking_cancel.delay(session, self._name, id)
        return True

class BotsStockPickingIn(orm.Model):
    _name = 'bots.stock.picking.in'
    _inherit = 'bots.binding'
    _inherits = {'stock.picking.in': 'openerp_id'}
    _description = 'Bots Stock Picking In'

    _columns = {
        'openerp_id': fields.many2one('stock.picking.in',
                                      string='Stock Picking In',
                                      required=True,
                                      ondelete='restrict'),
        'warehouse_id': fields.many2one('bots.warehouse',
                                      string='Bots Warehouse',
                                      required=True,
                                      ondelete='restrict'),
        'bots_override': fields.boolean('Override Bots Restrictions', help='Allow all normal Bots constraints to be ignored, eg when completing or cancelling.'),
        }

    _sql_constraints = [
        ('bots_picking_in_uniq', 'unique(backend_id, openerp_id)',
         'A Bots picking already exists for this picking for the same backend.'),
    ]

    def reexport_order(self, cr, uid, ids, context=None):
        session = ConnectorSession(cr, uid, context=context)
        for id in ids:
            export_picking_available.delay(session, self._name, id)
        return True

    def reexport_cancel(self, cr, uid, ids, context=None):
        session = ConnectorSession(cr, uid, context=context)
        for id in ids:
            export_picking_cancel.delay(session, self._name, id)
        return True

@bots
class BotsStockPickingOutBinder(BotsModelBinder):
    _model_name = [
            'bots.stock.picking.out',
        ]

@bots
class BotsStockPickingInBinder(BotsModelBinder):
    _model_name = [
            'bots.stock.picking.in',
        ]

class StockPickingAdapter(BotsCRUDAdapter):
    _picking_type = None

    def create(self, picking_id):

        def _find_pricelist_cost(cr, uid, pl_id, prod_id, partner, uom, date, context=None):
            pl_obj = self.session.pool.get('product.pricelist')
            price = pl_obj.price_get(cr, uid, [pl_id], prod_id, 1.0, partner, {
                'uom' : uom,
                'date' : date,
                })[pl_id]
            return price

        if self._picking_type == 'in':
            MODEL = 'bots.stock.picking.in'
            TYPE = 'in'
            FILENAME = 'picking_in_%s.json'
            ALLOWED_STATES = ('waiting', 'confirmed', 'assigned',)
        elif self._picking_type == 'out':
            MODEL = 'bots.stock.picking.out'
            TYPE = 'out'
            FILENAME = 'picking_out_%s.json'
            ALLOWED_STATES = ('assigned',)
        else:
            raise NotImplementedError('Unable to adapt stock picking of type %s' % (self._picking_type,))

        product_binder = self.get_binder_for_model('bots.product')
        picking_binder = self.get_binder_for_model(MODEL)
        bots_picking_obj = self.session.pool.get(MODEL)
        picking_obj = self.session.pool.get('stock.picking')
        move_obj = self.session.pool.get('stock.move')
        bots_warehouse_obj = self.session.pool.get('bots.warehouse')
        currency_obj = self.session.pool.get('res.currency')
        wf_service = netsvc.LocalService("workflow")

        picking = bots_picking_obj.browse(self.session.cr, self.session.uid, picking_id)
        default_company_id = picking.warehouse_id.warehouse_id.company_id.id
        if self.session.context and self.session.context.get('company_id'):
             default_company_id = self.session.context.get('company_id')
        ctx = (self.session.context or {}).copy()
        ctx.update({'company_id': default_company_id})
        default_company = self.session.pool.get('res.company').browse(self.session.cr, self.session.uid, default_company_id, context=ctx)

        picking = bots_picking_obj.browse(self.session.cr, self.session.uid, picking_id, context=ctx)
        if self._picking_type == 'out':
            order_number = picking.sale_id and picking.sale_id.name or picking.name
            address = picking.partner_id or picking.sale_id and picking.sale_id.partner_shipping_id
        elif self._picking_type == 'in':
            order_number = picking.purchase_id and picking.purchase_id.name or picking.name
            address = picking.partner_id or picking.purchase_id and (picking.purchase_id.warehouse_id and picking.purchase_id.warehouse_id.partner_id or picking.purchase_id.dest_address_id)
        else:
            order_number = picking.name
            address = picking.partner_id

        if picking.bots_id:
            raise JobError(_('The Bots picking %s already has an external ID. Will not export again.') % (picking.id,))

        if not address:
            raise MappingError(_('Missing address when attempting to export Bots picking %s.') % (picking_id,))

        # Get a unique name for the picking
        BOTS_ID_MAX_LEN = 16
        bots_id = re.sub(r'[\\/_-]', r'', order_number.upper())[:BOTS_ID_MAX_LEN]
        # Test if this ID is unique, if not increment it
        suffix_counter = 0
        existing_id = picking_binder.to_openerp(bots_id)
        orig_bots_id = bots_id
        while existing_id:
            suffix_counter += 1
            bots_id = "%sS%s" % (orig_bots_id[:BOTS_ID_MAX_LEN-1-len(str(suffix_counter))], suffix_counter)
            existing_id = picking_binder.to_openerp(bots_id)

        # Select which moves we will ship
        picking_complete = True
        moves_to_split = []
        order_lines = []
        seq = 1
        for move in picking.move_lines:
            if move.state not in ALLOWED_STATES:
                picking_complete = False
                moves_to_split.append(move.id)
                continue
            product_bots_id = move.product_id and product_binder.to_backend(move.product_id.id)
            if not product_bots_id:
                picking_complete = False
                moves_to_split.append(move.id)
                continue

            discount = 0
            price_unit = move.product_id.standard_price
            currency = default_company.currency_id
            if move.sale_line_id:
                price_unit = move.sale_line_id.price_unit
                currency = move.sale_line_id.order_id.currency_id
                discount = move.sale_line_id.discount
            elif move.purchase_line_id:
                price_unit = move.purchase_line_id.price_unit
                currency = move.purchase_line_id.order_id.currency_id
            elif move.picking_id:
                default_currency = currency
                order = False
                if move.picking_id.sale_id and move.picking_id.sale_id.currency_id:
                    currency = move.picking_id.sale_id.currency_id
                    order = move.picking_id.sale_id
                elif move.picking_id.purchase_id and move.picking_id.purchase_id.currency_id:
                    currency = move.picking_id.purchase_id.currency_id
                    order = move.picking_id.purchase_id

                pricelist_price = False
                if order:
                    pricelist_price = _find_pricelist_cost(self.session.cr, self.session.uid, order.pricelist_id.id, move.product_id.id, move.partner_id.id, move.product_uom.id, order.date_order, context=ctx)

                if pricelist_price:
                    # Currency will already be correct since the field on the order
                    # is related to the field on the same pricelist.
                    price_unit = pricelist_price
                elif currency.id != default_currency.id:
                    # Fallback - no price could be found on the pricelist.
                    # Convery standard_price to the correct currency.
                    price_unit = currency_obj.compute(self.session.cr, self.session.uid, default_currency.id, currency.id, price_unit, round=False, context=ctx)

            price = currency_obj.round(self.session.cr, self.session.uid, currency, (1 - (discount/100.0)) * price_unit)

            order_line = {
                    "id": "%sS%s" % (bots_id, seq),
                    "seq": seq,
                    "product": product_bots_id, 
                    "product_qty": int(move.product_qty),
                    "uom": move.product_uom.name,
                    "product_uos_qty": int(move.product_uos_qty),
                    "uos": move.product_uos.name,
                    "price_unit": price,
                    "price_currency": currency.name,
                }
            if move.product_id.volume:
                order_line['volume_net'] = move.product_id.volume
            if move.product_id.weight:
                order_line['weight'] = move.product_id.weight
            if move.product_id.weight_net:
                order_line['weight_net'] = move.product_id.weight_net
            if move.note:
                order_line['desc'] = move.note and move.note[:64]
            if TYPE == 'in':
                order_line['customs_free_from'] = not picking.bots_customs

            order_lines.append(order_line)
            seq += 1

        if not order_lines:
            raise MappingError(_('Unable to export any order lines on export of Bots picking %s.') % (picking_id,))

        # Split picking depending on order policy
        if not picking_complete:
            if TYPE == 'in':
                raise NotImplementedError(_('Exporting a partial incoming picking is not implemented'))
            sale_policy = picking.sale_id and picking.sale_id.picking_policy or 'direct'
            picking_policy = picking.move_type or sale_policy
            if picking_policy != 'direct':
                raise InvalidDataError(_('Unable to export picking %s. Picking policy does not allow it to be split and is not fully complete or some products are not mapped for export.') % (picking_id,))
            # Split the picking
            new_picking_id = picking_obj.copy(self.session.cr, self.session.uid, picking.openerp_id.id,
                                              {
                                                  'move_type': sale_policy,
                                                  'move_lines': [],
                                              },
                                              context=ctx)
            move_obj.write(self.session.cr, self.session.uid, moves_to_split, {'picking_id': new_picking_id}, context=ctx)
            wf_service.trg_validate(self.session.uid, 'stock.picking', new_picking_id, 'button_confirm', self.session.cr)

        picking_data = {
                'id': bots_id,
                'name': bots_id,
                'order': bots_id,
                'state': 'new',
                'type': TYPE,
                'date': datetime.strptime(picking.min_date, DEFAULT_SERVER_DATETIME_FORMAT).strftime('%Y-%m-%d'),
                'partner':
                    {
                        "id": "P%d" % (picking.partner_id.id),
                        "name": picking.partner_id.name or '',
                        "street1": picking.partner_id.street or '',
                        "street2": picking.partner_id.street2 or '',
                        "city": picking.partner_id.city or '',
                        "zip": picking.partner_id.zip or '',
                        "country": picking.partner_id.country_id and picking.partner_id.country_id.code or '',
                        "state": picking.partner_id.state_id and picking.partner_id.state_id.name or '',
                        "phone": picking.partner_id.phone or '',
                        "fax": picking.partner_id.fax or '',
                        "email": picking.partner_id.email or '',
                        "language": picking.partner_id.lang or '',
                    },
                'client_order_ref': picking.sale_id and picking.sale_id.client_order_ref or '',
                'line': order_lines,
            }
        if picking.note:
            picking_data['desc'] = picking.note and picking.note[:64]
        if picking.partner_id.vat:
            picking_data['partner']['vat'] = picking.partner_id.vat

        data = {
                'picking': {
                        'pickings': [picking_data,],
                        'header': [{
                                'type': TYPE,
                                'state': 'done',
                                'partner_to': picking.backend_id.name_to,
                                'partner_from': picking.backend_id.name_from,
                                'message_id': '0',
                                'date_msg': datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f'),
                            }],
                    },
                }
        data = json.dumps(data, indent=4)

        filename_id = self._get_unique_filename(FILENAME)
        res = self._write(filename_id, data)
        return bots_id

    def delete(self, picking_id):

        if self._picking_type == 'in':
            MODEL = 'bots.stock.picking.in'
            TYPE = 'in'
            FILENAME = 'picking_in_%s.json'
        elif self._picking_type == 'out':
            MODEL = 'bots.stock.picking.out'
            TYPE = 'out'
            FILENAME = 'picking_out_%s.json'
        else:
            raise NotImplementedError('Unable to adapt stock picking of type %s' % (self._picking_type,))

        bots_picking_obj = self.session.pool.get(MODEL)

        picking = bots_picking_obj.browse(self.session.cr, self.session.uid, picking_id)
        if not picking.bots_id:
            raise JobError(_('The Bots picking %s is exported but does not yet have an external ID. Cannot be cancelled.') % (picking.id,))

        picking_data = {
                'id': picking.bots_id,
                'name': picking.bots_id,
                'order': picking.bots_id,
                'state': 'delete',
                'type': TYPE,
            }
        data = {
                'picking': {
                        'pickings': [picking_data,],
                        'header': [{
                                'type': TYPE,
                                'state': 'cancel',
                                'partner_to': picking.backend_id.name_to,
                                'partner_from': picking.backend_id.name_from,
                                'message_id': '0',
                                'date_msg': datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f'),
                            }],
                    },
                }
        data = json.dumps(data, indent=4)

        filename_id = self._get_unique_filename(FILENAME)
        res = self._write(filename_id, data)
        return

@bots
class StockPickingOutAdapter(StockPickingAdapter):
    _model_name = 'bots.stock.picking.out'
    _picking_type = 'out'

@bots
class StockPickingInAdapter(StockPickingAdapter):
    _model_name = 'bots.stock.picking.in'
    _picking_type = 'in'

def picking_available(session, model_name, record_id, picking_type, location_type):
    warehouse_obj = session.pool.get('stock.warehouse')
    bots_warehouse_obj = session.pool.get('bots.warehouse')
    picking = session.browse(model_name, record_id)
    # Check to see if the picking should be exported to the WMS
    # If so create binding, else return
    if not picking.state == 'assigned': # Handle only deliveries which are assigned
        return

    location_ids = []
    if location_type == 'src':
        location = picking.location_id or picking.move_lines and picking.move_lines[0].location_id
        while location and location.id not in location_ids:
            location_ids.append(location.id)
            if location.chained_picking_type != 'in' and location.chained_auto_packing == 'auto':
                location = location.chained_location_id
    else:
        location = picking.location_dest_id or picking.move_lines and picking.move_lines[0].location_dest_id
        while location and location.id not in location_ids:
            location_ids.append(location.id)
            if location.chained_picking_type != 'out' and location.chained_auto_packing == 'auto':
                location = location.chained_location_id

    warehouse_ids = []
    for location_id in location_ids:
        warehouse_ids = warehouse_obj.search(session.cr, session.uid, ['|', ('lot_stock_id', '=', location_id), ('lot_output_id', '=', location_id)])
        if warehouse_ids:
            break

    bots_warehouse_ids = bots_warehouse_obj.search(session.cr, session.uid, [('warehouse_id', 'in', warehouse_ids)])
    bots_warehouse = bots_warehouse_obj.browse(session.cr, session.uid, bots_warehouse_ids)
    for warehouse in bots_warehouse:
        backend_id = warehouse.backend_id
        if (picking_type == 'bots.stock.picking.out' and backend_id.feat_picking_out) or \
            (picking_type == 'bots.stock.picking.in' and backend_id.feat_picking_in):
            session.create(picking_type,
                            {'backend_id': backend_id.id,
                            'openerp_id': picking.id,
                            'warehouse_id': warehouse['id'],})

def picking_cancel(session, model_name, record_id, picking_type):
    warehouse_obj = session.pool.get('stock.warehouse')
    bots_warehouse_obj = session.pool.get('bots.warehouse')
    picking_ids = session.search(picking_type, [('openerp_id', '=', record_id)])
    pickings = session.browse(picking_type, picking_ids)

    late_pickings = []

    for picking in pickings:
        min_date = datetime.strptime(picking.min_date, DEFAULT_SERVER_DATETIME_FORMAT)
        if not picking.bots_override and datetime.now().date() >= min_date.date():
            late_pickings.append(picking.name)
            continue
        export_picking_cancel.delay(session, picking_type, picking.id)

    if late_pickings:
        raise osv.except_osv(_('Error!'), _('Could not cancel the following pickings, they might have already been delivered by the warehouse: %s') % ", ".join(late_pickings))

@bots
class BotsPickingExport(ExportSynchronizer):
    _model_name = ['bots.stock.picking.in',
                   'bots.stock.picking.out']

    def run(self, binding_id):
        """
        Export the picking to Bots
        """
        bots_id = self.backend_adapter.create(binding_id)
        self.binder.bind(bots_id, binding_id)

    def delete(self, binding_id):
        """
        Export the cancelled picking to Bots
        """
        self.backend_adapter.delete(binding_id)
        self.binder.unbind(binding_id)
        pass

@on_record_create(model_names='bots.stock.picking.out')
def delay_export_picking_out_available(session, model_name, record_id, vals):
    export_picking_available.delay(session, model_name, record_id)

@on_record_create(model_names='bots.stock.picking.in')
def delay_export_picking_in_available(session, model_name, record_id, vals):
    export_picking_available.delay(session, model_name, record_id)

@job
def export_picking_available(session, model_name, record_id):
    picking = session.browse(model_name, record_id)
    if picking.state == 'done' and session.search(model_name, [('backorder_id', '=', picking.openerp_id.id)]):
        # We are an auto-created back order completed - ignore this export
        return "Not creating backorder for auto-created done picking backorder %s" % (picking.name,)
    backend_id = picking.backend_id.id
    env = get_environment(session, model_name, backend_id)
    picking_exporter = env.get_connector_unit(BotsPickingExport)
    res = picking_exporter.run(record_id)
    return res

@job
def export_picking_cancel(session, model_name, record_id):
    picking = session.browse(model_name, record_id)
    backend_id = picking.backend_id.id
    env = get_environment(session, model_name, backend_id)
    picking_exporter = env.get_connector_unit(BotsPickingExport)
    picking_exporter.delete(record_id)
    return True

@on_picking_out_available
def picking_out_available(session, model_name, record_id):
    return picking_available(session, model_name, record_id, 'bots.stock.picking.out', location_type='src')

@on_picking_in_available
def picking_in_available(session, model_name, record_id):
    return picking_available(session, model_name, record_id, 'bots.stock.picking.in', location_type='dest')

@on_picking_out_cancel
def picking_out_cancel(session, model_name, record_id):
    return picking_cancel(session, model_name, record_id, 'bots.stock.picking.out')

@on_picking_in_cancel
def picking_in_cancel(session, model_name, record_id):
    return picking_cancel(session, model_name, record_id, 'bots.stock.picking.in')
