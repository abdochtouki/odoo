# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from datetime import datetime, timedelta

from odoo import api, fields, models, _
from odoo.tools import DEFAULT_SERVER_DATETIME_FORMAT, float_compare
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    _inherit = "sale.order"

    @api.model
    def _default_warehouse_id(self):
        company = self.env.user.company_id.id
        warehouse_ids = self.env['stock.warehouse'].search([('company_id', '=', company)], limit=1)
        return warehouse_ids

    incoterm = fields.Many2one(
        'stock.incoterms', 'Incoterms',
        help="International Commercial Terms are a series of predefined commercial terms used in international transactions.")
    picking_policy = fields.Selection([
        ('direct', 'Deliver each product when available'),
        ('one', 'Deliver all products at once')],
        string='Shipping Policy', required=True, readonly=True, default='direct',
        states={'draft': [('readonly', False)], 'sent': [('readonly', False)]})
    warehouse_id = fields.Many2one(
        'stock.warehouse', string='Warehouse',
        required=True, readonly=True, states={'draft': [('readonly', False)], 'sent': [('readonly', False)]},
        default=_default_warehouse_id)
    picking_ids = fields.Many2many('stock.picking', compute='_compute_picking_ids', string='Picking associated to this sale')
    delivery_count = fields.Integer(string='Delivery Orders', compute='_compute_picking_ids')

    @api.multi
    @api.depends('procurement_group_id')
    def _compute_picking_ids(self):
        for order in self:
            order.picking_ids = self.env['stock.picking'].search([('group_id', '=', order.procurement_group_id.id)]) if order.procurement_group_id else []
            order.delivery_count = len(order.picking_ids)

    @api.onchange('warehouse_id')
    def _onchange_warehouse_id(self):
        if self.warehouse_id.company_id:
            self.company_id = self.warehouse_id.company_id.id

    @api.multi
    def action_view_delivery(self):
        '''
        This function returns an action that display existing delivery orders
        of given sales order ids. It can either be a in a list or in a form
        view, if there is only one delivery order to show.
        '''
        action = self.env.ref('stock.action_picking_tree_all').read()[0]

        pickings = self.mapped('picking_ids')
        if len(pickings) > 1:
            action['domain'] = [('id', 'in', pickings.ids)]
        elif pickings:
            action['views'] = [(self.env.ref('stock.view_picking_form').id, 'form')]
            action['res_id'] = pickings.id
        return action

    @api.multi
    def action_cancel(self):
        self.mapped('order_line').mapped('procurement_ids').cancel()
        return super(SaleOrder, self).action_cancel()

    @api.multi
    def _prepare_invoice(self):
        invoice_vals = super(SaleOrder, self)._prepare_invoice()
        invoice_vals['incoterms_id'] = self.incoterm.id or False
        return invoice_vals

    def _prepare_procurement_group(self):
        res = super(SaleOrder, self)._prepare_procurement_group()
        res.update({'move_type': self.picking_policy, 'partner_id': self.partner_shipping_id.id, 'sale_order_id': self.id})
        return res

    @api.model
    def _get_customer_lead(self, product_tmpl_id):
        super(SaleOrder, self)._get_customer_lead(product_tmpl_id)
        return product_tmpl_id.sale_delay


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    product_packaging = fields.Many2one('product.packaging', string='Package', default=False)
    route_id = fields.Many2one('stock.location.route', string='Route', domain=[('sale_selectable', '=', True)], ondelete='restrict')

    @api.depends('order_id.state')
    def _compute_invoice_status(self):
        super(SaleOrderLine, self)._compute_invoice_status()
        for line in self:
            # We handle the following specific situation: a physical product is partially delivered,
            # but we would like to set its invoice status to 'Fully Invoiced'. The use case is for
            # products sold by weight, where the delivered quantity rarely matches exactly the
            # quantity ordered.
            if line.order_id.state == 'done'\
                    and line.invoice_status == 'no'\
                    and line.product_id.type in ['consu', 'product']\
                    and line.product_id.invoice_policy == 'delivery'\
                    and line.procurement_ids.mapped('move_ids')\
                    and all(move.state in ['done', 'cancel'] for move in line.procurement_ids.mapped('move_ids')):
                line.invoice_status = 'invoiced'

    @api.multi
    @api.depends('product_id')
    def _compute_qty_delivered_updateable(self):
        # prefetch field before filtering
        self.mapped('product_id')
        # on consumable or stockable products, qty_delivered_updateable defaults
        # to False; on other lines use the original computation
        lines = self.filtered(lambda line: line.product_id.type not in ('consu', 'product'))
        lines = lines.with_prefetch(self._prefetch)
        super(SaleOrderLine, lines)._compute_qty_delivered_updateable()

    @api.onchange('product_id')
    def _onchange_product_id_set_customer_lead(self):
        self.customer_lead = self.product_id.sale_delay

    @api.onchange('product_packaging')
    def _onchange_product_packaging(self):
        if self.product_packaging:
            return self._check_package()

    @api.onchange('product_id')
    def _onchange_product_id_uom_check_availability(self):
        if not self.product_uom or (self.product_id.uom_id.category_id.id != self.product_uom.category_id.id):
            self.product_uom = self.product_id.uom_id
        self._onchange_product_id_check_availability()

    @api.onchange('product_uom_qty', 'product_uom', 'route_id')
    def _onchange_product_id_check_availability(self):
        if not self.product_id or not self.product_uom_qty or not self.product_uom:
            self.product_packaging = False
            return {}
        if self.product_id.type == 'product':
            precision = self.env['decimal.precision'].precision_get('Product Unit of Measure')
            product = self.product_id.with_context(warehouse=self.order_id.warehouse_id.id)
            product_qty = self.product_uom._compute_quantity(self.product_uom_qty, self.product_id.uom_id)
            if float_compare(product.virtual_available, product_qty, precision_digits=precision) == -1:
                is_available = self._check_routing()
                if not is_available:
                    message =  _('You plan to sell %s %s but you only have %s %s available in %s warehouse.') % \
                            (self.product_uom_qty, self.product_uom.name, product.virtual_available, product.uom_id.name, self.order_id.warehouse_id.name)
                    # We check if some products are available in other warehouses.
                    if float_compare(product.virtual_available, self.product_id.virtual_available, precision_digits=precision) == -1:
                        message += _('\nThere are %s %s available accross all warehouses.') % \
                                (self.product_id.virtual_available, product.uom_id.name)

                    warning_mess = {
                        'title': _('Not enough inventory!'),
                        'message' : message
                    }
                    return {'warning': warning_mess}
        return {}

    @api.onchange('product_uom_qty')
    def _onchange_product_uom_qty(self):
        if self.state == 'sale' and self.product_id.type in ['product', 'consu'] and self.product_uom_qty < self._origin.product_uom_qty:
            warning_mess = {
                'title': _('Ordered quantity decreased!'),
                'message' : _('You are decreasing the ordered quantity! Do not forget to manually update the delivery order if needed.'),
            }
            return {'warning': warning_mess}
        return {}

    @api.multi
    def _prepare_order_line_procurement(self, group_id=False):
        vals = super(SaleOrderLine, self)._prepare_order_line_procurement(group_id=group_id)
        date_planned = datetime.strptime(self.order_id.confirmation_date, DEFAULT_SERVER_DATETIME_FORMAT)\
            + timedelta(days=self.customer_lead or 0.0) - timedelta(days=self.order_id.company_id.security_lead)
        vals.update({
            'date_planned': date_planned.strftime(DEFAULT_SERVER_DATETIME_FORMAT),
            'location_id': self.order_id.partner_shipping_id.property_stock_customer.id,
            'route_ids': self.route_id and [(4, self.route_id.id)] or [],
            'warehouse_id': self.order_id.warehouse_id and self.order_id.warehouse_id.id or False,
            'partner_dest_id': self.order_id.partner_shipping_id.id,
            'sale_line_id': self.id,
        })
        return vals

    @api.multi
    def _get_delivered_qty(self):
        """Computes the delivered quantity on sales order lines, based on done stock moves related to its procurements
        """
        self.ensure_one()
        super(SaleOrderLine, self)._get_delivered_qty()
        qty = 0.0
        for move in self.procurement_ids.mapped('move_ids').filtered(lambda r: r.state == 'done' and not r.scrapped):
            if move.location_dest_id.usage == "customer":
                if not move.origin_returned_move_id:
                    qty += move.product_uom._compute_quantity(move.product_uom_qty, self.product_uom)
            elif move.location_dest_id.usage != "customer" and move.to_refund:
                qty -= move.product_uom._compute_quantity(move.product_uom_qty, self.product_uom)
        return qty

    @api.multi
    def _check_package(self):
        default_uom = self.product_id.uom_id
        pack = self.product_packaging
        qty = self.product_uom_qty
        q = default_uom._compute_quantity(pack.qty, self.product_uom)
        if qty and q and (qty % q):
            newqty = qty - (qty % q) + q
            return {
                'warning': {
                    'title': _('Warning'),
                    'message': _("This product is packaged by %.2f %s. You should sell %.2f %s.") % (pack.qty, default_uom.name, newqty, self.product_uom.name),
                },
            }
        return {}

    def _check_routing(self):
        """ Verify the route of the product based on the warehouse
            return True if the product availibility in stock does not need to be verified,
            which is the case in MTO, Cross-Dock or Drop-Shipping
        """
        is_available = False
        product_routes = self.route_id or (self.product_id.route_ids + self.product_id.categ_id.total_route_ids)

        # Check MTO
        wh_mto_route = self.order_id.warehouse_id.mto_pull_id.route_id
        if wh_mto_route and wh_mto_route <= product_routes:
            is_available = True
        else:
            mto_route = False
            try:
                mto_route = self.env['stock.warehouse']._get_mto_route()
            except UserError:
                # if route MTO not found in ir_model_data, we treat the product as in MTS
                pass
            if mto_route and mto_route in product_routes:
                is_available = True

        # Check Drop-Shipping
        if not is_available:
            for pull_rule in product_routes.mapped('pull_ids'):
                if pull_rule.picking_type_id.sudo().default_location_src_id.usage == 'supplier' and\
                        pull_rule.picking_type_id.sudo().default_location_dest_id.usage == 'customer':
                    is_available = True
                    break

        return is_available
