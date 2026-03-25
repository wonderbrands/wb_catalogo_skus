from odoo import models, fields, api

class ProductTemplate(models.Model):
    _inherit = 'product.template'

    data_product_id = fields.Char(string="Data Product ID")
    
    
    #filtro Multicaja
    is_multibox = fields.Boolean(
        string="Es Multicaja", 
        compute="_compute_is_multibox", 
        store=True
    )

    @api.depends('bom_ids', 'bom_ids.bom_line_ids')
    def _compute_is_multibox(self):
        for record in self:
            is_multi = False
            #listas de materiales del producto
            for bom in record.bom_ids:
                # Si la lista tiene más de 1 componente, es multicaja/combo
                if len(bom.bom_line_ids) > 1:
                    is_multi = True
                    break
            record.is_multibox = is_multi